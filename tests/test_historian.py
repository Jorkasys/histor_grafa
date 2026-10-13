"""Запросы к AVEVA Historian, настройки, ошибки и проверка подключения на эмуляторе."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import emulator
from app.historian import (
    SQLITE,
    Historian,
    HistorianError,
    check,
    describe,
    hint,
    mssql_dialect,
    normalize,
    odbc_connection_string,
    openquery_sql,
    pick_driver,
    split_masks,
    tags_sql,
    values_sql,
)

T0 = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ запросы
def test_masks_split_and_like_escaping():
    assert split_masks("Lehmann1_*, CP2_?;\n  Lehmann2_* \n") == ["Lehmann1_*", "CP2_?",
                                                                  "Lehmann2_*"]
    sql = tags_sql(mssql_dialect(), ["Lehmann1_*"], ["*_Alarm_*", "Sys*"], strings=False)
    # Подчёркивание в именах тегов — не подстановочный знак LIKE
    assert "t.TagName LIKE N'Lehmann1\\_%' ESCAPE '\\'" in sql
    assert "NOT (t.TagName LIKE N'%\\_Alarm\\_%' ESCAPE '\\' OR t.TagName LIKE N'Sys%'" in sql
    assert "t.TagType IN (1, 2)" in sql
    assert "FROM [Runtime].dbo.Tag t" in sql and "EngineeringUnit" in sql


def test_values_query_literals_and_window():
    sql = values_sql(mssql_dialect(), ["Lehmann1_FinWay", "Bad'Name"],
                     datetime(2026, 10, 12, 7, 0, 0, 123456),
                     datetime(2026, 10, 12, 7, 0, 5, 500001))
    assert "TagName IN (N'Lehmann1_FinWay', N'Bad''Name')" in sql
    # Время — ISO с «T» (не зависит от DATEFORMAT); окно запроса не уже окна
    assert "DateTime > '2026-10-12T07:00:00.123'" in sql
    assert "DateTime <= '2026-10-12T07:00:05.501'" in sql
    assert "wwRetrievalMode = 'Delta'" in sql and "wwTimeZone = 'UTC'" in sql
    assert "wwVersion = 'Latest'" in sql and "FROM [Runtime].dbo.History" in sql

    cyclic = values_sql(mssql_dialect(), ["A"], T0.replace(tzinfo=None),
                        (T0 + timedelta(minutes=1)).replace(tzinfo=None),
                        retrieval="cyclic", resolution_ms=1000, utc=False)
    assert "wwRetrievalMode = 'Cyclic'" in cyclic and "wwResolution = 1000" in cyclic
    assert "wwTimeZone" not in cyclic


def test_openquery_doubles_inner_quotes():
    inner = values_sql(mssql_dialect(inner=True), ["A'B"], T0.replace(tzinfo=None),
                       (T0 + timedelta(seconds=5)).replace(tzinfo=None), cast_text=False)
    sql = openquery_sql("INSQL", inner)
    assert "FROM OPENQUERY([INSQL], 'SELECT" in sql
    assert "TagName IN (''A''''B'')" in sql
    assert "DateTime > ''2026-10-12 07:00:00.000''" in sql
    assert "FROM History" in sql and "Runtime" not in sql


def test_long_tag_lists_are_split_into_several_queries():
    historian = Historian({"server": "HIS01", "linked_server": "INSQL"})
    names = [f"Lehmann1_Konsha_Temperature_Sensor_{i:04d}" for i in range(400)]
    queries = historian.queries(names, T0, T0 + timedelta(seconds=5))
    assert len(queries) > 1
    assert all(len(q) < 8000 for q in queries)        # предел текста OPENQUERY
    joined = "".join(queries)
    assert all(n in joined for n in names)


def test_time_zone_of_answer():
    historian = Historian({"server": "HIS01", "time_zone": "Europe/Moscow"})
    sql = historian.queries(["A"], T0, T0 + timedelta(seconds=5))[0]
    # Окно переведено в пояс сервера, wwTimeZone не используется
    assert "DateTime > '2026-10-12T10:00:00.000'" in sql and "wwTimeZone" not in sql
    assert historian.to_utc(datetime(2026, 10, 12, 10, 0)) == T0
    assert historian.to_utc("2026-10-12 10:00:00.1234567") == T0 + timedelta(microseconds=123456)
    with pytest.raises(HistorianError):
        Historian({"server": "HIS01", "time_zone": "Mars/Olympus"})


# ---------------------------------------------------------------- настройки
def test_normalize_settings():
    config = normalize({"server": " HIS01 ", "port": "1433", "poll_s": "5", "strings": "false",
                        "password": " p@ss ", "database": ""})
    assert config["server"] == "HIS01" and config["port"] == 1433 and config["poll_s"] == 5
    assert config["strings"] is False and config["password"] == " p@ss "
    assert config["database"] == "Runtime" and config["exclude"] == "Sys*"
    assert normalize({"port": ""})["port"] is None
    with pytest.raises(HistorianError, match="Порт"):
        normalize({"port": "abc"})
    with pytest.raises(HistorianError, match="Как часто"):
        normalize({"poll_s": 0})
    with pytest.raises(HistorianError, match="Вход"):
        normalize({"auth": "kerberos"})


def test_driver_choice_and_connection_string(monkeypatch):
    monkeypatch.setattr("app.historian.sys.platform", "linux")
    monkeypatch.setattr("app.historian.odbc_driver_name", lambda: "ODBC Driver 18 for SQL Server")
    assert pick_driver({"auth": "sql"}) == "odbc"
    assert pick_driver({"auth": "windows", "password": "x"}) == "freetds"
    assert pick_driver({"auth": "sql", "driver": "freetds"}) == "freetds"
    monkeypatch.setattr("app.historian.odbc_driver_name", lambda: None)
    assert pick_driver({"auth": "sql"}) == "freetds"

    text = odbc_connection_string({"server": "HIS01", "port": 1433, "database": "Runtime",
                                   "auth": "sql", "user": "reader", "password": "a}b;c"},
                                  "ODBC Driver 18 for SQL Server")
    assert "SERVER=HIS01,1433" in text and "UID=reader" in text
    assert "PWD={a}}b;c}" in text and "TrustServerCertificate=yes" in text


async def test_windows_login_needs_domain_and_password(monkeypatch):
    monkeypatch.setattr("app.historian.sys.platform", "linux")
    historian = Historian({"server": "HIS01", "auth": "windows", "user": "ivanov",
                           "password": "x"})
    with pytest.raises(HistorianError, match="ДОМЕН"):
        await historian.open()


def test_error_hints():
    class Fake(Exception):
        pass

    err = Fake(20009, b"DB-Lib error message 20009, severity 9:\nUnable to connect: "
                      b"TDS server is unavailable or does not exist (HIS01)\n")
    text = describe(err)
    assert text.startswith("[20009] DB-Lib error") and "\n" not in text
    assert "сервер не отвечает" in hint(text)
    assert "отказал во входе" in hint("Login failed for user 'EU\\ivanov'. (18456)")
    assert "Часовой пояс" in hint("Invalid column name 'wwTimeZone'")
    assert "TLS" in hint("[08001] SSL Provider: error:0A000102")
    assert "Runtime" in hint("Invalid object name 'Runtime.dbo.History'")


# --------------------------------------------------------------- эмулятор
@pytest.fixture
def emu(tmp_path):
    path = tmp_path / "runtime.sqlite"
    emulator.create(path, T0 - timedelta(hours=2), T0, step_s=10)
    return path


async def test_list_tags_and_read(emu):
    async with emulator.historian(emu, {"include": "Lehmann1_*"}) as h:
        tags = await h.list_tags()
        by_name = {t.name: t for t in tags}
        assert "SysTimeSec" not in by_name
        assert by_name["Lehmann1_Temp"].unit == "°C" and by_name["Lehmann1_Temp"].max_value == 120
        assert by_name["Lehmann1_Run"].datatype == "bool"
        assert by_name["Lehmann1_Recipe"].datatype == "string"
        points = await h.read(tags, T0 - timedelta(minutes=10), T0)
    temps = [p for p in points if p.name == "Lehmann1_Temp"]
    assert len(temps) == 60                     # каждые 10 с, окно (start, end]
    assert all(T0 - timedelta(minutes=10) < p.ts <= T0 for p in points)
    recipes = [p for p in points if p.name == "Lehmann1_Recipe"]
    assert recipes and all(p.text in ("DARK70", "MILK32") and p.value is None for p in recipes)


async def test_check_reports_steps(emu):
    base = {"server": str(emu), "user": "reader"}
    steps = await check({**base, "include": "Lehmann1_*"}, dialect=SQLITE,
                        connect=emulator.connector(emu), clock=lambda: T0)
    assert [s.ok for s in steps] == [True, True, True]
    assert "подходит тегов: 5" in steps[1].detail
    assert "строк в сутки" in steps[2].detail

    steps = await check({**base, "include": "Nothing*"}, dialect=SQLITE,
                        connect=emulator.connector(emu), clock=lambda: T0)
    assert steps[-1].ok is False and "маски" in steps[-1].detail

    def refused(_config):
        raise OSError("connection refused")

    steps = await check({"server": "HIS01", "user": "reader"}, dialect=SQLITE, connect=refused)
    assert len(steps) == 1 and not steps[0].ok and "connection refused" in steps[0].detail
