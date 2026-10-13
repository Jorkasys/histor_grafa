"""Эмулятор AVEVA Historian для тестов: файл SQLite с таблицами того же вида,
что в базе Runtime (Tag, AnalogTag, EngineeringUnit, History)."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.historian import SQLITE, Historian

SCHEMA = """
CREATE TABLE IF NOT EXISTS Tag (TagName TEXT, Description TEXT, TagType INT);
CREATE TABLE IF NOT EXISTS AnalogTag (TagName TEXT, EUKey INT, MinEU REAL, MaxEU REAL);
CREATE TABLE IF NOT EXISTS EngineeringUnit (EUKey INT, Unit TEXT);
CREATE TABLE IF NOT EXISTS History (TagName TEXT, DateTime TEXT, Value REAL, vValue TEXT,
                                    Quality INT);
CREATE INDEX IF NOT EXISTS h_tag_ts ON History (TagName, DateTime);
"""

TAGS = [
    ("Lehmann1_Temp", "CP2 Lehmann 1 - температура массы", 1, "°C", 0, 120),
    ("Lehmann1_Current", "CP2 Lehmann 1 - ток привода", 1, "A", 0, 400),
    ("Lehmann1_FinWay", "CP2 Lehmann 1 - актуальное направление разгрузки танка 3", 1, None,
     None, None),
    ("Lehmann1_Run", "CP2 Lehmann 1 - в работе", 2, None, None, None),
    ("Lehmann1_Recipe", "CP2 Lehmann 1 - рецепт", 3, None, None, None),
    ("SysTimeSec", "Время историка", 1, None, None, None),
]


def stamp(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def create(path: Path | str, start: datetime, end: datetime, step_s: float = 10) -> None:
    """Создать эмулятор с историей от start до end: аналоговые — каждые
    step_s секунд, дискретный и строковый — раз в 5 минут."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    units = {}
    for name, descr, kind, unit, lo, hi in TAGS:
        db.execute("INSERT INTO Tag VALUES (?, ?, ?)", (name, descr, kind))
        if kind == 1:
            if unit and unit not in units:
                units[unit] = len(units) + 1
                db.execute("INSERT INTO EngineeringUnit VALUES (?, ?)", (units[unit], unit))
            db.execute("INSERT INTO AnalogTag VALUES (?, ?, ?, ?)",
                       (name, units.get(unit), lo, hi))
    add_history(db, start, end, step_s)
    db.commit()
    db.close()


def add_history(db: sqlite3.Connection, start: datetime, end: datetime,
                step_s: float = 10) -> int:
    rows = []
    ts = start
    i = 0
    while ts <= end:
        k = ts.timestamp()
        rows.append(("Lehmann1_Temp", stamp(ts), 45 + 5 * math.sin(k / 600), None, 0))
        rows.append(("Lehmann1_Current", stamp(ts), 120 + 30 * math.sin(k / 300), None, 0))
        rows.append(("SysTimeSec", stamp(ts), float(i), None, 0))
        if i % max(int(300 / step_s), 1) == 0:
            n = int(k // 300)
            rows.append(("Lehmann1_FinWay", stamp(ts), float(n % 3 + 1), None, 0))
            rows.append(("Lehmann1_Run", stamp(ts), float(n % 2), None, 0))
            rows.append(("Lehmann1_Recipe", stamp(ts), None, ["DARK70", "MILK32"][n % 2], 0))
        ts += timedelta(seconds=step_s)
        i += 1
    db.executemany("INSERT INTO History VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def connector(path: Path | str):
    """Функция подключения для Historian(..., connect=...)."""
    def connect(_config):
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    return connect


def historian(path: Path | str, config: dict) -> Historian:
    return Historian({"server": str(path), "user": "reader", **config}, dialect=SQLITE,
                     connect=connector(path))
