"""Общие заготовки тестов.

Тестам с базой нужен PostgreSQL (лучше с TimescaleDB). Адрес задаётся
переменными TEST_DB_HOST, TEST_DB_PORT, TEST_DB_USER, TEST_DB_PASSWORD
(суперпользователь: тесты создают свою базу h2g_test). Нет базы — эти
тесты пропускаются.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.config import Settings  # noqa: E402

TEST_DB = "h2g_test"


def admin_settings() -> dict:
    return {"host": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("TEST_DB_PORT", "5432")),
            "user": os.environ.get("TEST_DB_USER", "postgres"),
            "password": os.environ.get("TEST_DB_PASSWORD", "postgres")}


@pytest.fixture
async def test_settings() -> Settings:
    """Чистая база h2g_test для каждого теста."""
    admin = admin_settings()
    try:
        conn = await asyncpg.connect(database="postgres", timeout=5, **admin)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"нет PostgreSQL для тестов ({exc})")
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await conn.close()
    return Settings(db_host=admin["host"], db_port=admin["port"], db_name=TEST_DB,
                    db_user=admin["user"], db_password=admin["password"],
                    reader_user="h2g_test_reader", reader_password="reader-pass")
