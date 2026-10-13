"""Чтение AVEVA Historian (Wonderware Historian, InSQL) по SQL.

AVEVA Historian отдаёт историю через SQL Server: база ``Runtime``,
справочник тегов ``dbo.Tag`` и расширенная таблица ``dbo.History``. Режим
выборки задаётся «столбцами-параметрами» в условии запроса:
``wwRetrievalMode`` (Delta — каждое записанное изменение, Cyclic — значение
на каждый шаг), ``wwResolution``, ``wwTimeZone``, ``wwVersion``.

Значения подставляются в запрос литералами, а не параметрами: расширенные
таблицы обрабатывает собственный провайдер историка (INSQL), и литералы —
то, что он гарантированно понимает. Это безопасно: строка в T-SQL
экранируется удвоением кавычки, время пишется в формате ISO 8601 с «T»,
который не зависит от языка и DATEFORMAT сервера.

Подключиться можно двумя драйверами:

* **ODBC** — Microsoft ODBC Driver 18 for SQL Server, вход логином SQL Server;
* **FreeTDS** (библиотека pymssql) — вход логином SQL Server или учётной
  записью Windows в виде ``ДОМЕН\\логин`` с паролем (NTLM). Так из
  Linux-контейнера можно войти доменной учётной записью, как в SSMS.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("historian")

APP_NAME = "historian2grafana"
# dbo.Tag.TagType: 1 — аналоговый, 2 — дискретный, 3 — строковый
TAG_TYPES = {1: "float64", 2: "bool", 3: "string"}
# Длина текста запроса: у OPENQUERY предел 8000 символов
MAX_SQL = {"direct": 60000, "openquery": 7000}
GOOD = 0
BAD = 1
_FRACTION = re.compile(r"(\.\d{6})\d+")


class HistorianError(RuntimeError):
    """Понятная человеку ошибка: нет связи, неверный логин и т. п."""


# ---------------------------------------------------------------------------
#  Настройки подключения
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "server": "",
    "port": None,
    "database": "Runtime",
    "auth": "sql",              # sql | windows
    "user": "",
    "password": "",
    "include": "",
    "exclude": "Sys*",
    "strings": True,
    "poll_s": 5,
    "history_hours": 24,
    "lag_s": 3,
    "overlap_s": 30,
    "retrieval": "delta",       # delta | cyclic
    "resolution_ms": 5000,
    "time_zone": "",
    "linked_server": "",
    "driver": "auto",           # auto | odbc | freetds
    "enabled": False,
}
CHOICES = {"auth": ("sql", "windows"), "retrieval": ("delta", "cyclic"),
           "driver": ("auto", "odbc", "freetds")}
LIMITS = {"poll_s": (1, 3600), "history_hours": (0, 24 * 366), "lag_s": (0, 3600),
          "overlap_s": (0, 86400), "resolution_ms": (100, 86_400_000), "port": (1, 65535)}


def normalize(raw: dict[str, Any] | None, *, strict: bool = True) -> dict[str, Any]:
    """Настройки с подставленными значениями по умолчанию и проверкой."""
    raw = dict(raw or {})
    out: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            value = value if isinstance(value, bool) else str(value).lower() in {
                "1", "true", "yes", "on", "да"}
        elif key in LIMITS:
            if value in (None, ""):
                value = None if key == "port" else default
            else:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise HistorianError(f"«{LABELS[key]}»: ожидалось число") from None
                lo, hi = LIMITS[key]
                if strict and not lo <= number <= hi:
                    raise HistorianError(f"«{LABELS[key]}»: допустимо от {lo} до {hi}")
                value = int(number) if number.is_integer() else number
        else:
            value = "" if value is None else str(value).strip()
            if key in CHOICES and value not in CHOICES[key]:
                if strict:
                    raise HistorianError(
                        f"«{LABELS[key]}»: допустимо {', '.join(CHOICES[key])}")
                value = default
        out[key] = value
    # Пароль не обрезаем: пробелы по краям могут быть его частью
    out["password"] = "" if raw.get("password") is None else str(raw.get("password"))
    out["database"] = out["database"] or "Runtime"
    return out


LABELS = {
    "server": "Сервер", "port": "Порт", "database": "База", "auth": "Вход",
    "user": "Логин", "password": "Пароль", "include": "Какие теги копировать",
    "exclude": "Кроме тегов", "strings": "Строковые теги",
    "poll_s": "Как часто забирать новые значения", "history_hours": "Загрузить историю за",
    "lag_s": "Не брать последние", "overlap_s": "Перекрытие", "retrieval": "Режим выборки",
    "resolution_ms": "Шаг для Cyclic", "time_zone": "Часовой пояс ответа",
    "linked_server": "Связанный сервер", "driver": "Драйвер", "enabled": "Копирование",
}


def split_masks(text: str | None) -> list[str]:
    return [m.strip() for m in re.split(r"[,;\n]+", str(text or "")) if m.strip()]


# ---------------------------------------------------------------------------
#  Запросы
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Dialect:
    """Как писать запрос: к SQL Server историка или к его эмулятору в SQLite."""

    prefix: str            # «[Runtime].dbo.» или пусто
    unicode: bool          # строки N'…'
    ww: bool               # столбцы-параметры wwRetrievalMode и др.
    iso_t: bool            # время с «T» (SQL Server) или с пробелом
    digits: int            # знаков долей секунды в литерале времени

    def text(self, value: str) -> str:
        quoted = "'" + str(value).replace("'", "''") + "'"
        return ("N" + quoted) if self.unicode else quoted

    def like(self, mask: str) -> str:
        out = []
        for ch in mask:
            if ch in "\\%_" or (ch == "[" and self.unicode):
                out.append("\\" + ch)
            elif ch == "*":
                out.append("%")
            elif ch == "?":
                out.append("_")
            else:
                out.append(ch)
        return self.text("".join(out))

    def moment(self, value: datetime, up: bool) -> str:
        """Литерал времени без пояса; ``up`` — округлять вверх, чтобы окно
        запроса не оказалось уже окна копирования."""
        step = 10 ** (6 - self.digits)
        micro = value.microsecond
        rounded = (math.ceil(micro / step) if up else micro // step) * step
        value = value.replace(microsecond=0) + timedelta(microseconds=rounded)
        text = value.strftime("%Y-%m-%dT%H:%M:%S" if self.iso_t else "%Y-%m-%d %H:%M:%S")
        if self.digits:
            text += "." + f"{value.microsecond:06d}"[:self.digits]
        return "'" + text + "'"


def mssql_dialect(database: str = "Runtime", inner: bool = False) -> Dialect:
    if inner:
        # Внутри OPENQUERY запрос разбирает сам историк: имена таблиц без
        # базы, время — в привычном ему виде с пробелом
        return Dialect("", False, True, False, 3)
    db = database.strip() or "Runtime"
    return Dialect(f"[{db.replace(']', ']]')}].dbo.", True, True, True, 3)


SQLITE = Dialect("", False, False, False, 6)


def tags_sql(d: Dialect, include: Sequence[str], exclude: Sequence[str], strings: bool,
             rich: bool = True) -> str:
    types = "1, 2, 3" if strings else "1, 2"
    where = [f"t.TagType IN ({types})"]
    if include:
        where.append("(" + " OR ".join(f"t.TagName LIKE {d.like(m)} ESCAPE '\\'"
                                       for m in include) + ")")
    if exclude:
        where.append("NOT (" + " OR ".join(f"t.TagName LIKE {d.like(m)} ESCAPE '\\'"
                                           for m in exclude) + ")")
    if rich:
        head = ("SELECT t.TagName, t.Description, t.TagType, e.Unit, a.MinEU, a.MaxEU\n"
                f"FROM {d.prefix}Tag t\n"
                f"LEFT JOIN {d.prefix}AnalogTag a ON a.TagName = t.TagName\n"
                f"LEFT JOIN {d.prefix}EngineeringUnit e ON e.EUKey = a.EUKey\n")
    else:
        head = ("SELECT t.TagName, t.Description, t.TagType, NULL, NULL, NULL\n"
                f"FROM {d.prefix}Tag t\n")
    return head + "WHERE " + "\n  AND ".join(where) + "\nORDER BY t.TagName"


def values_sql(d: Dialect, names: Sequence[str], start: datetime, end: datetime, *,
               retrieval: str = "delta", resolution_ms: int = 5000, utc: bool = True,
               cast_text: bool = True) -> str:
    """Значения тегов за окно (start, end]; start и end — без пояса, в том
    времени, в котором отвечает историк."""
    text = "CAST(vValue AS nvarchar(512))" if cast_text else "vValue"
    where = [f"TagName IN ({', '.join(d.text(n) for n in names)})",
             f"DateTime > {d.moment(start, up=False)}",
             f"DateTime <= {d.moment(end, up=True)}"]
    if d.ww:
        if retrieval == "cyclic":
            where += ["wwRetrievalMode = 'Cyclic'", f"wwResolution = {int(resolution_ms)}"]
        else:
            where.append("wwRetrievalMode = 'Delta'")
        if utc:
            where.append("wwTimeZone = 'UTC'")
        where.append("wwVersion = 'Latest'")
    return (f"SELECT TagName, DateTime, Value, {text} AS vText, Quality\n"
            f"FROM {d.prefix}History\nWHERE " + "\n  AND ".join(where))


def openquery_sql(server: str, inner: str) -> str:
    """Запрос через связанный сервер: внутренний текст уходит историку как есть."""
    name = "[" + server.strip().replace("]", "]]") + "]"
    quoted = "'" + inner.replace("'", "''") + "'"
    return ("SELECT TagName, DateTime, Value, CAST(vText AS nvarchar(512)) AS vText, Quality\n"
            f"FROM OPENQUERY({name}, {quoted})")


# ---------------------------------------------------------------------------
#  Данные
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    description: str | None = None
    unit: str | None = None
    datatype: str = "float64"        # float64 | bool | string
    min_value: float | None = None
    max_value: float | None = None


@dataclass(slots=True)
class Point:
    name: str
    ts: datetime                     # с часовым поясом (UTC)
    value: float | None
    text: str | None = None
    quality: int = GOOD


@dataclass
class Step:
    """Шаг проверки подключения — для отчёта в интерфейсе."""

    title: str
    ok: bool = True
    detail: str = ""
    items: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "ok": self.ok, "detail": self.detail, "items": self.items}


# ---------------------------------------------------------------------------
#  Подключение
# ---------------------------------------------------------------------------
def pick_driver(config: dict[str, Any]) -> str:
    """Каким драйвером подключаться: odbc или freetds."""
    chosen = config.get("driver") or "auto"
    if chosen != "auto":
        return chosen
    if config.get("auth") == "windows":
        # На самой Windows без пароля — встроенный вход текущего пользователя
        # через ODBC; в контейнере — FreeTDS с ДОМЕН\логин и паролем
        if sys.platform == "win32" and not config.get("password"):
            return "odbc"
        return "freetds"
    return "odbc" if odbc_driver_name() else "freetds"


def odbc_driver_name() -> str | None:
    try:
        import pyodbc
    except ImportError:
        return None
    installed = set(pyodbc.drivers())
    for name in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                 "SQL Server"):
        if name in installed:
            return name
    return None


def odbc_connection_string(config: dict[str, Any], driver: str) -> str:
    server = str(config.get("server") or "").strip()
    if config.get("port"):
        server += f",{int(config['port'])}"
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}",
             f"DATABASE={config.get('database') or 'Runtime'}", f"APP={APP_NAME}"]
    if config.get("auth") == "windows" and not config.get("password"):
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={config.get('user') or ''}",
                  "PWD={" + str(config.get("password") or "").replace("}", "}}") + "}"]
    # Сертификат SQL Server на заводе обычно самоподписанный. Старый
    # драйвер «SQL Server» из Windows шифрования не просит и параметра не знает
    if driver != "SQL Server":
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)


def connect_odbc(config: dict[str, Any]):
    try:
        import pyodbc
    except ImportError as exc:
        raise HistorianError("не установлена библиотека pyodbc") from exc
    driver = odbc_driver_name()
    if not driver:
        raise HistorianError("не найден ODBC-драйвер SQL Server — выберите драйвер FreeTDS")
    conn = pyodbc.connect(odbc_connection_string(config, driver), timeout=15,
                          autocommit=True, readonly=True)
    # Предел на один запрос: зависший запрос не должен держать копирование
    conn.timeout = 300
    return conn


def connect_freetds(config: dict[str, Any]):
    try:
        import pymssql
    except ImportError as exc:
        raise HistorianError("не установлена библиотека pymssql (драйвер FreeTDS)") from exc
    server = str(config.get("server") or "").strip()
    return pymssql.connect(
        server=server, port=str(int(config["port"])) if config.get("port") else "1433",
        user=str(config.get("user") or ""), password=str(config.get("password") or ""),
        database=str(config.get("database") or "Runtime"), login_timeout=15, timeout=300,
        appname=APP_NAME, autocommit=True, tds_version="7.4")


CONNECTORS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "odbc": connect_odbc,
    "freetds": connect_freetds,
}


class Historian:
    """Соединение с историком: список тегов и значения за окно."""

    def __init__(self, config: dict[str, Any], *, dialect: Dialect | None = None,
                 connect: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.config = normalize(config, strict=False)
        zone = self.config["time_zone"]
        try:
            self.tz = ZoneInfo(zone) if zone else timezone.utc
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HistorianError(f"неизвестный часовой пояс {zone!r} "
                                 "(пример: Europe/Moscow; пусто — UTC)") from exc
        self.utc = not zone
        self.include = split_masks(self.config["include"])
        self.exclude = split_masks(self.config["exclude"])
        self.driver = "test" if connect else pick_driver(self.config)
        self._connect = connect or CONNECTORS[self.driver]
        self._dialect = dialect
        self._conn = None
        self._lock = asyncio.Lock()

    @property
    def dialect(self) -> Dialect:
        return self._dialect or mssql_dialect(self.config["database"])

    @property
    def linked(self) -> str:
        return "" if self._dialect else self.config["linked_server"]

    async def open(self) -> None:
        if not self.config["server"]:
            raise HistorianError("укажите сервер историка")
        auth = self.config["auth"]
        if auth == "sql" and not self.config["user"]:
            raise HistorianError("для входа логином SQL Server укажите логин")
        if auth == "windows" and self.driver == "freetds":
            if "\\" not in self.config["user"] or not self.config["password"]:
                raise HistorianError(
                    "для входа учётной записью Windows укажите логин в виде ДОМЕН\\логин "
                    "(например EU\\ivanov) и пароль этой учётной записи")
        try:
            self._conn = await asyncio.to_thread(self._connect, self.config)
        except HistorianError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HistorianError(hint(f"не удалось подключиться к историку: {describe(exc)}",
                                      self.driver)) from exc

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.to_thread(conn.close)
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> "Historian":
        await self.open()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def fetch(self, sql: str) -> list[tuple]:
        if self._conn is None:
            raise HistorianError("нет соединения с историком")

        def run() -> list[tuple]:
            cursor = self._conn.cursor()
            try:
                cursor.execute(sql)
                return [tuple(r) for r in cursor.fetchall()] if cursor.description else []
            finally:
                cursor.close()

        async with self._lock:
            try:
                return await asyncio.to_thread(run)
            except Exception as exc:  # noqa: BLE001
                raise HistorianError(hint(f"ошибка запроса к историку: {describe(exc)}",
                                          self.driver)) from exc

    # ------------------------------------------------------------------ теги
    async def list_tags(self) -> list[Tag]:
        d = self.dialect
        strings = bool(self.config["strings"])
        try:
            rows = await self.fetch(tags_sql(d, self.include, self.exclude, strings))
        except HistorianError as exc:
            # Справочники единиц бывают недоступны — тогда хотя бы имена и типы
            log.warning("теги с единицами не прочитались (%s) — читаю без них", exc)
            rows = await self.fetch(tags_sql(d, self.include, self.exclude, strings,
                                             rich=False))
        tags = []
        for name, description, tag_type, unit, lo, hi in rows:
            datatype = TAG_TYPES.get(_int(tag_type) or 1, "float64")
            tags.append(Tag(str(name), _str(description), _str(unit), datatype,
                            _float(lo), _float(hi)))
        return tags

    # -------------------------------------------------------------- значения
    def queries(self, names: Sequence[str], start: datetime, end: datetime) -> list[str]:
        """Запросы значений за окно (start, end]: столько тегов в запросе,
        сколько помещается в его текст."""
        linked = self.linked
        d = mssql_dialect(inner=True) if linked else self.dialect
        local = [t.astimezone(self.tz).replace(tzinfo=None) for t in (start, end)]
        limit = MAX_SQL["openquery" if linked else "direct"]
        options = {"retrieval": self.config["retrieval"],
                   "resolution_ms": int(self.config["resolution_ms"]), "utc": self.utc}
        out: list[str] = []
        batch: list[str] = []
        size = 0
        for name in names:
            cost = len(name) + 6 + name.count("'") * (2 if linked else 1)
            if batch and size + cost > limit - 600:
                out.append(self._query(d, batch, local, linked, options))
                batch, size = [], 0
            batch.append(name)
            size += cost
        if batch:
            out.append(self._query(d, batch, local, linked, options))
        return out

    @staticmethod
    def _query(d: Dialect, names, local, linked, options) -> str:
        if linked:
            inner = values_sql(d, names, local[0], local[1], cast_text=False, **options)
            return openquery_sql(linked, inner)
        return values_sql(d, names, local[0], local[1], **options)

    async def read(self, tags: Sequence[Tag], start: datetime, end: datetime) -> list[Point]:
        """Все значения тегов в полуинтервале (start, end]."""
        types = {t.name: t.datatype for t in tags}
        out = []
        for sql in self.queries([t.name for t in tags], start, end):
            for name, moment, value, text, quality in await self.fetch(sql):
                ts = self.to_utc(moment)
                if ts is None:
                    continue
                name = str(name)
                if types.get(name) == "string":
                    number, text = None, (None if text is None else str(text))
                else:
                    number, text = _float(value), None
                bad = (number is None and text is None) or _int(quality) == 1
                out.append(Point(name, ts, None if bad else number, None if bad else text,
                                 BAD if bad else GOOD))
        return out

    def to_utc(self, raw: Any) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, str):
            # Старый драйвер «SQL Server» отдаёт datetime2 строкой с 7 знаками
            try:
                raw = datetime.fromisoformat(_FRACTION.sub(r"\1", raw.strip()))
            except ValueError:
                return None
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=self.tz)
        return raw.astimezone(timezone.utc)

    # ------------------------------------------------------------- проверка
    async def server_info(self) -> list[str]:
        """Версии SQL Server и историка, часы сервера — что удалось узнать."""
        if self._dialect is not None:
            return ["эмулятор историка"]
        out = []
        for key, sql in (
                ("server", "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)), "
                           "CAST(SERVERPROPERTY('MachineName') AS nvarchar(128))"),
                ("historian", f"SELECT TOP 1 CAST(Value AS nvarchar(64)) FROM "
                              f"{self.dialect.prefix}SystemParameter "
                              "WHERE Name = 'HistorianVersion'"),
                ("clock", "SELECT DATEDIFF(minute, GETUTCDATE(), GETDATE())")):
            try:
                rows = await self.fetch(sql)
            except HistorianError:
                continue
            if not rows:
                continue
            if key == "server":
                out.append(f"SQL Server {rows[0][0]} на компьютере {rows[0][1]}")
            elif key == "historian":
                out.append(f"AVEVA Historian {rows[0][0]}")
            else:
                minutes = int(rows[0][0])
                sign = "+" if minutes >= 0 else "−"
                out.append(f"часы сервера: UTC{sign}{abs(minutes) // 60}"
                           + (f":{abs(minutes) % 60:02d}" if minutes % 60 else ""))
        return out


async def check(config: dict[str, Any], *, minutes: int = 10, sample: int = 50,
                dialect: Dialect | None = None, connect=None,
                clock: Callable[[], datetime] | None = None) -> list[Step]:
    """Проверка подключения по шагам: связь, теги под маски, чтение значений."""
    steps: list[Step] = []
    try:
        historian = Historian(config, dialect=dialect, connect=connect)
    except HistorianError as exc:
        return [Step("Настройки", False, str(exc))]
    driver = {"odbc": "ODBC Driver for SQL Server", "freetds": "FreeTDS",
              "test": "эмулятор"}[historian.driver]
    started = time.monotonic()
    try:
        await historian.open()
    except HistorianError as exc:
        return [Step(f"Подключение ({driver})", False, str(exc))]
    try:
        info = await historian.server_info()
        steps.append(Step(f"Подключение ({driver})", True,
                          f"за {time.monotonic() - started:.1f} с", info))

        started = time.monotonic()
        try:
            tags = await historian.list_tags()
        except HistorianError as exc:
            steps.append(Step("Список тегов", False, str(exc)))
            return steps
        kinds = {"float64": 0, "bool": 0, "string": 0}
        for t in tags:
            kinds[t.datatype] = kinds.get(t.datatype, 0) + 1
        rule = (", ".join(historian.include) or "все теги") + (
            f", кроме {', '.join(historian.exclude)}" if historian.exclude else "")
        if not tags:
            steps.append(Step("Список тегов", False,
                              f"под условие «{rule}» не подошёл ни один тег — проверьте маски"))
            return steps
        steps.append(Step(
            "Список тегов", True,
            f"под условие «{rule}» подходит тегов: {len(tags)} — аналоговых {kinds['float64']}, "
            f"дискретных {kinds['bool']}, строковых {kinds['string']} "
            f"(прочитано за {time.monotonic() - started:.1f} с)",
            [f"{t.name}" + (f" — {t.description}" if t.description else "")
             + (f", {t.unit}" if t.unit else "") for t in tags[:15]]
            + ([f"… и ещё {len(tags) - 15}"] if len(tags) > 15 else [])))

        now = (clock or (lambda: datetime.now(timezone.utc)))()
        part = tags[:sample]
        started = time.monotonic()
        try:
            points = await historian.read(part, now - timedelta(minutes=minutes), now)
        except HistorianError as exc:
            steps.append(Step("Чтение значений", False, str(exc)))
            return steps
        took = time.monotonic() - started
        per_tag: dict[str, int] = {}
        for p in points:
            per_tag[p.name] = per_tag.get(p.name, 0) + 1
        per_day = len(points) / max(len(part), 1) * len(tags) * (1440 / minutes)
        latest = {}
        for p in points:
            if p.name not in latest or p.ts >= latest[p.name].ts:
                latest[p.name] = p
        items = []
        for name in list(latest)[:10]:
            p = latest[name]
            value = p.text if p.text is not None else (
                "плохое качество" if p.value is None else f"{p.value:g}")
            items.append(f"{name} = {value} (время {p.ts.astimezone().strftime('%d.%m %H:%M:%S')}, "
                         f"значений за {minutes} мин: {per_tag[name]})")
        detail = (f"за последние {minutes} мин по {len(part)} тегам — {len(points)} значений, "
                  f"запрос {took:.1f} с. Ожидаемый объём на все {len(tags)} тегов: "
                  f"около {_human(per_day)} строк в сутки")
        if not points:
            detail += (". Значений нет: теги за это время не менялись или историк их не "
                       "пишет — копирование всё равно можно запускать")
        steps.append(Step("Чтение значений", True, detail, items))
        return steps
    finally:
        await historian.close()


# ---------------------------------------------------------------------------
#  Ошибки по-русски
# ---------------------------------------------------------------------------
def describe(exc: Exception) -> str:
    """Текст ошибки драйвера без технического мусора."""
    args = getattr(exc, "args", ())
    if len(args) == 1 and isinstance(args[0], tuple):
        args = args[0]
    if len(args) == 2 and isinstance(args[1], (bytes, str)):
        # pymssql: (код, b'сообщение')
        text = args[1].decode("utf-8", "replace") if isinstance(args[1], bytes) else args[1]
        return f"[{args[0]}] " + " ".join(text.split())
    return " ".join(str(exc).split()) or exc.__class__.__name__


def hint(message: str, driver: str = "") -> str:
    """Пояснение к частым ошибкам SQL Server."""
    low = message.lower()
    if "login failed" in low or "18456" in low or "18452" in low:
        return message + (" — сервер отказал во входе: неверный логин или пароль, или у "
                          "учётной записи нет права читать базу Runtime. Для учётной записи "
                          "Windows логин пишется как ДОМЕН\\логин")
    if "ssl" in low or "tls" in low or "certificate" in low or "handshake" in low:
        return message + (" — не удалось установить защищённое соединение с SQL Server. "
                          "Попробуйте драйвер FreeTDS (Дополнительно → Драйвер) или включите "
                          "HISTORIAN_OLD_TLS=1 в файле .env и перезапустите приложение")
    if "wwtimezone" in low:
        return message + (" — историк не принял wwTimeZone: укажите часовой пояс сервера "
                          "в поле «Часовой пояс ответа» (например Europe/Moscow)")
    if "invalid object name" in low:
        return message + (" — в базе нет таблиц историка: проверьте имя базы (обычно Runtime) "
                          "и что это сервер AVEVA Historian")
    if "permission" in low and "denied" in low:
        return message + " — у учётной записи нет права читать базу Runtime"
    if ("20002" in low or "adaptive server connection failed" in low
            or "tds server connection failed" in low):
        return message + (" — соединение оборвалось при входе: чаще всего неверный логин или "
                          "пароль (для Windows — ДОМЕН\\логин) либо сервер не принимает такой "
                          "способ входа. Попробуйте другой драйвер (Дополнительно → Драйвер)")
    if ("does not exist" in low or "unavailable" in low or "08001" in low
            or "named pipes" in low or "timeout expired" in low or "not known" in low
            or "host is unknown" in low or "tcp provider" in low or "20009" in low
            or "unknown host" in low or "login timeout" in low):
        return message + (" — сервер не отвечает: проверьте имя или IP-адрес, порт (обычно "
                          "1433) и что брандмауэр пропускает подключения к SQL Server. Из Docker "
                          "короткое имя компьютера может не находиться — укажите полное имя "
                          "с доменом или IP-адрес")
    if "data source name not found" in low or "im002" in low:
        return message + " — не найден ODBC-драйвер SQL Server: выберите драйвер FreeTDS"
    return message


def _human(value: float) -> str:
    if value >= 1e6:
        return f"{value / 1e6:.1f} млн"
    if value >= 1e3:
        return f"{value / 1e3:.0f} тыс."
    return f"{value:.0f}"


def _float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float, Decimal)):
        value = float(raw)
        return value if math.isfinite(value) else None
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _int(raw: Any) -> int | None:
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def _str(raw: Any) -> str | None:
    return None if raw is None or str(raw).strip() == "" else str(raw).strip()
