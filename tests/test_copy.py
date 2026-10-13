"""Копирование «эмулятор историка → база» и API приложения (нужен PostgreSQL)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

import emulator
from app.db import Database
from app.historian import SQLITE, Historian
from app.main import create_app
from app.sync import Copier

T0 = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def factory(path):
    return lambda config: Historian(config, dialect=SQLITE, connect=emulator.connector(path))


async def counts(db: Database) -> dict[str, tuple[int, int]]:
    """{тег: (строк в tag_value, row_count в tag_sync)}"""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.name, (SELECT count(*) FROM tag_value v WHERE v.tag_id = t.tag_id) AS n, "
            "s.row_count FROM tag t LEFT JOIN tag_sync s USING (tag_id)")
    return {r["name"]: (r["n"], r["row_count"]) for r in rows}


async def test_backfill_then_new_values_without_duplicates(tmp_path, test_settings):
    path = tmp_path / "runtime.sqlite"
    emulator.create(path, T0 - timedelta(hours=3), T0, step_s=10)
    db = Database(test_settings)
    await db.connect(wait_s=5)
    try:
        clock = Clock(T0 + timedelta(seconds=3))
        copier = Copier(db, historian_factory=factory(path), clock=clock)
        copier.config = {"server": str(path), "user": "reader", "include": "Lehmann1_*",
                         "history_hours": 1,
                         "lag_s": 3, "overlap_s": 30, "poll_s": 5}
        await copier.cycle()
        assert copier.tags == 5 and copier.state == "live" and copier.progress == 1.0
        c = await counts(db)
        assert "SysTimeSec" not in c
        assert c["Lehmann1_Temp"] == (360, 360)              # час каждые 10 с: (start, end]

        # Историк дописал минуту — следующий проход забирает только её
        writer = sqlite3.connect(path)
        emulator.add_history(writer, T0 + timedelta(seconds=10), T0 + timedelta(seconds=60))
        writer.commit()
        writer.close()
        clock.now = T0 + timedelta(seconds=63)
        before = copier.rows_copied
        await copier.cycle()
        c = await counts(db)
        assert c["Lehmann1_Temp"] == (366, 366)              # без дублей, счётчик верный
        assert copier.rows_copied - before >= 6

        # Маски сузились: лишние теги перестают копироваться, их данные остаются
        copier.config["include"] = "Lehmann1_Temp"
        await copier._drop_historian()
        copier._ids = {}
        await copier.cycle()
        _, items = await db.list_tags(include_inactive=True)
        active = {t["name"] for t in items if t["active"]}
        assert active == {"Lehmann1_Temp"} and len(items) == 5

        _, items = await db.list_tags(ids=[next(t["tag_id"] for t in items
                                                if t["name"] == "Lehmann1_Temp")])
        temp = items[0]
        assert temp["unit"] == "°C" and temp["rows"] == 366 and temp["quality"] == 0
        series = await db.series(items, T0 - timedelta(minutes=30), T0 + timedelta(seconds=60),
                                 points=60)
        points = series[temp["tag_id"]]["points"]
        assert points[0][0] == (T0 - timedelta(minutes=30)).timestamp()   # значение до окна
        assert 55 <= len(points) <= 63

        await db.reset_data()
        assert all(n == 0 for n, _ in (await counts(db)).values())
    finally:
        await db.close()


async def test_reader_role_can_read_values_but_not_settings(test_settings):
    db = Database(test_settings)
    await db.connect(wait_s=5)
    await db.save_setting("historian", {"password": "secret"})
    await db.close()
    conn = await asyncpg.connect(host=test_settings.db_host, port=test_settings.db_port,
                                 database=test_settings.db_name,
                                 user=test_settings.reader_user,
                                 password=test_settings.reader_password)
    try:
        await conn.fetch("SELECT * FROM tag_latest")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM app_setting")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM tag_value")
    finally:
        await conn.close()


def test_api_flow(tmp_path, test_settings):
    now = datetime.now(timezone.utc)
    path = tmp_path / "runtime.sqlite"
    emulator.create(path, now - timedelta(hours=2), now, step_s=30)
    app = create_app(test_settings, historian_factory=factory(path),
                     check_options={"dialect": SQLITE, "connect": emulator.connector(path)})
    with TestClient(app) as client:
        config = client.get("/api/settings").json()
        assert config["server"] == "" and config["poll_s"] == 5 and config["password_set"] is False
        assert client.get("/healthz").json() == {"ok": True}

        bad = client.put("/api/settings", json={"poll_s": "abc"})
        assert bad.status_code == 400 and "Как часто" in bad.json()["detail"]

        body = {"server": str(path), "include": "Lehmann1_*", "history_hours": 1,
                "auth": "sql", "user": "reader", "password": "secret", "poll_s": 1}
        saved = client.put("/api/settings", json=body).json()
        assert saved["password_set"] is True and "password" not in saved

        report = client.post("/api/test", json={"include": "Lehmann1_*"}).json()
        assert report["ok"] is True and len(report["steps"]) == 3

        # Пустой пароль при сохранении — «не менять»
        client.put("/api/settings", json={**body, "password": ""})
        assert client.get("/api/settings").json()["password_set"] is True

        status = client.post("/api/copy/start", json={}).json()
        assert status["running"] is True
        for _ in range(100):
            state = client.get("/api/status").json()
            if state["copy"]["state"] == "live" and state["db"]["rows"]:
                break
            time.sleep(0.1)
        assert state["copy"]["state"] == "live", state["copy"]
        assert state["db"]["tags_active"] == 5 and state["db"]["rows"] > 100

        tags = client.get("/api/tags", params={"q": "темпер"}).json()
        assert tags["total"] == 1 and tags["items"][0]["name"] == "Lehmann1_Temp"
        temp_id = tags["items"][0]["tag_id"]
        recipe = client.get("/api/tags", params={"q": "Recipe"}).json()["items"][0]

        series = client.get("/api/series", params={
            "ids": f"{temp_id},{recipe['tag_id']}",
            "from": (now - timedelta(minutes=30)).isoformat()}).json()
        by_name = {t["name"]: t for t in series["tags"]}
        assert len(by_name["Lehmann1_Temp"]["points"]) > 30
        assert by_name["Lehmann1_Recipe"]["changes"]

        assert client.post("/api/copy/stop").json()["running"] is False
        assert client.get("/api/settings").json()["enabled"] is False
        client.post("/api/copy/reset")
        assert client.get("/api/status").json()["db"]["rows"] == 0
