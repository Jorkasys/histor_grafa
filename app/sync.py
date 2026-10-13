"""Копирование значений из историка в нашу базу.

Как это работает:

* у каждого тега своя отметка «скопировано до» (таблица tag_sync);
* новый тег сначала догружается на ``history_hours`` назад окнами по часу —
  долгую первичную загрузку можно прервать, она продолжится с места остановки;
* дальше раз в ``poll_s`` секунд забирается всё новое. Последние ``lag_s``
  секунд не трогаем: историк мог ещё не дописать их;
* каждое окно записывается как «заменить всё в (start, end]»: удалить и
  вставить одной транзакцией. Поэтому повторное чтение безопасно — дублей
  не бывает, а значения, которые историк дописал с опозданием, подтягиваются
  перекрытием окна (``overlap_s``) и раз в 10 минут — перечитыванием
  последних 15 минут.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from .historian import Historian, HistorianError, Tag

log = logging.getLogger("sync")

CHUNK = timedelta(hours=1)          # окно одного чтения при загрузке истории
BATCH = 100                         # тегов в одном запросе к историку
TAG_REFRESH_S = 600                 # как часто перечитывать список тегов
RESWEEP = (600, 900)                # раз в 10 мин перечитать последние 15 мин
MAX_BACKOFF_S = 300                 # пауза после ошибки растёт до 5 минут


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Copier:
    """Фоновое копирование. Один экземпляр на приложение."""

    def __init__(self, db, *, historian_factory: Callable[[dict], Historian] = Historian,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.db = db
        self.historian_factory = historian_factory
        self.clock = clock
        self.config: dict[str, Any] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=60)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._reset_state()
        self.state = "stopped"

    def _reset_state(self) -> None:
        self.state = "starting"        # stopped | starting | backfill | live | error
        self.tags = 0
        self.rows_copied = 0
        self.started_at: float | None = None
        self.last_sync_at: float | None = None
        self.behind_s: float | None = None
        self.progress: float | None = None
        self.last_error: str | None = None
        self.errors = 0
        self.cycle_s: float | None = None
        self._historian: Historian | None = None
        self._tags: dict[str, Tag] = {}
        self._ids: dict[str, int] = {}
        self._tags_loaded_at = 0.0
        self._resweep_at = time.monotonic()
        self._reported = -1

    # ------------------------------------------------------------ управление
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, config: dict[str, Any]) -> None:
        await self.stop(quiet=True)
        self.config = dict(config)
        self._reset_state()
        self.started_at = time.time()
        self._stop = asyncio.Event()
        self.event("info", f"Копирование запущено: сервер {config.get('server')}, "
                           f"раз в {config.get('poll_s')} с")
        self._task = asyncio.create_task(self.run(), name="copier")

    async def stop(self, quiet: bool = False) -> None:
        was_running = self.running
        if was_running:
            self._stop.set()
            task = self._task
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        await self._drop_historian()
        self.state = "stopped"
        if was_running and not quiet:
            self.event("info", "Копирование остановлено")

    def event(self, level: str, text: str) -> None:
        self.events.appendleft({"ts": time.time(), "level": level, "text": text})
        getattr(log, "warning" if level == "error" else "info")(text)

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state, "running": self.running, "tags": self.tags,
            "rows_copied": self.rows_copied, "started_at": self.started_at,
            "last_sync_at": self.last_sync_at, "behind_s": self.behind_s,
            "progress": self.progress, "last_error": self.last_error,
            "errors": self.errors, "cycle_s": self.cycle_s,
            "poll_s": self.config.get("poll_s"), "events": list(self.events),
        }

    # ------------------------------------------------------------ цикл работы
    async def run(self) -> None:
        backoff = 5.0
        failing = False
        while not self._stop.is_set():
            try:
                started = time.monotonic()
                await self.cycle()
                self.cycle_s = round(time.monotonic() - started, 2)
                backoff = 5.0
                if failing:
                    self.event("ok", "Связь с историком восстановлена")
                failing = False
                self.last_error = None
                delay = float(self.config.get("poll_s") or 5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — драйвер может бросить что угодно
                self.state = "error"
                self.errors += 1
                self.last_error = str(exc) or exc.__class__.__name__
                if not isinstance(exc, HistorianError):
                    log.exception("ошибка копирования")
                self.event("error", f"{self.last_error} — повтор через {backoff:.0f} с")
                failing = True
                await self._drop_historian()
                delay, backoff = backoff, min(backoff * 2, MAX_BACKOFF_S)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        await self._drop_historian()

    async def cycle(self) -> None:
        """Один проход: при необходимости обновить теги и скопировать новое."""
        historian = await self._ensure_historian()
        if not self._ids or time.monotonic() - self._tags_loaded_at > TAG_REFRESH_S:
            await self._refresh_tags(historian)
        names = [n for n in self._ids if n in self._tags]
        self.tags = len(names)
        now = self.clock()
        end = now - timedelta(seconds=float(self.config.get("lag_s") or 0))
        if not names:
            self.state = "live"
            self.last_sync_at = time.time()
            return

        initial = now - timedelta(hours=float(self.config.get("history_hours") or 0))
        overlap = timedelta(seconds=float(self.config.get("overlap_s") or 0))
        if time.monotonic() - self._resweep_at >= RESWEEP[0]:
            overlap = max(overlap, timedelta(seconds=RESWEEP[1]))
            self._resweep_at = time.monotonic()
        marks = await self.db.load_watermarks([self._ids[n] for n in names])

        # Теги с одинаковой отметкой читаются вместе: после первой загрузки
        # это почти всегда все теги одной группой
        groups: dict[datetime | None, list[str]] = {}
        for name in names:
            groups.setdefault(marks.get(self._ids[name]), []).append(name)
        oldest = min((mark or initial) for mark in groups)
        self._progress(oldest, initial, end, final=False)

        for mark, group in sorted(groups.items(), key=lambda kv: kv[0] or initial):
            if self._stop.is_set():
                return
            # Новый тег — с начала глубины истории, остальные — с перекрытием
            start = (mark - overlap) if mark else initial
            await self._copy_range(historian, group, start, end, initial)

        self.last_sync_at = time.time()
        self._progress(end, initial, end)

    async def _copy_range(self, historian: Historian, names: Sequence[str],
                          start: datetime, end: datetime, initial: datetime) -> None:
        pos = start
        while pos < end and not self._stop.is_set():
            stop = min(pos + CHUNK, end)
            for i in range(0, len(names), BATCH):
                await self._copy_window(historian, names[i:i + BATCH], pos, stop)
            pos = stop
            self._progress(pos, initial, end, final=False)

    async def _copy_window(self, historian: Historian, names: Sequence[str],
                           start: datetime, stop: datetime) -> None:
        points = await historian.read([self._tags[n] for n in names], start, stop)
        ids = {n: self._ids[n] for n in names}
        records = []
        for p in points:
            tag_id = ids.get(p.name)
            if tag_id is None or not (start < p.ts <= stop):
                continue  # историк вернул лишнее — не наше окно
            records.append((p.ts, tag_id, p.value, p.text, p.quality))
        await self.db.write_window(list(ids.values()), start, stop, records)
        self.rows_copied += len(records)

    async def _refresh_tags(self, historian: Historian) -> None:
        tags = await historian.list_tags()
        self._ids = await self.db.upsert_tags(tags)
        self._tags = {t.name: t for t in tags}
        self._tags_loaded_at = time.monotonic()
        if not tags:
            self.event("error", "Под маски не подошёл ни один тег историка — проверьте "
                                "маски на странице «Подключение»")
        else:
            self.event("info", f"Тегов под масками: {len(tags)}")

    async def _ensure_historian(self) -> Historian:
        if self._historian is None:
            historian = self.historian_factory(self.config)
            await historian.open()
            self._historian = historian
            self._tags_loaded_at = 0.0
            self.event("ok", "Подключено к историку")
        return self._historian

    async def _drop_historian(self) -> None:
        historian, self._historian = self._historian, None
        if historian is not None:
            await historian.close()

    def _progress(self, position: datetime, initial: datetime, end: datetime,
                  final: bool = True) -> None:
        behind = max((end - position).total_seconds(), 0.0)
        self.behind_s = round(behind, 1)
        span = (end - initial).total_seconds()
        done = (position - initial).total_seconds()
        self.progress = 1.0 if span <= 0 else round(min(max(done / span, 0.0), 1.0), 4)
        caught_up = behind <= max(float(self.config.get("poll_s") or 5) * 3, 60)
        if not caught_up:
            self.state = "backfill"
            step = int(self.progress * 10)
            if step != self._reported:
                self._reported = step
                self.event("info", f"Загрузка истории: {self.progress * 100:.0f}%")
        elif final or self.state != "backfill":
            if self.state in ("backfill", "starting"):
                self.event("ok", "Копирование догнало историк: дальше забираются только "
                                 "новые значения")
            self.state = "live"
