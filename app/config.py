"""Настройки приложения из переменных окружения (в Docker — из файла .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "historian"
    db_user: str = "historian"
    db_password: str = "historian"
    # Учётная запись Grafana в базе: только чтение данных тегов
    reader_user: str = "grafana_reader"
    reader_password: str = "grafana_reader"
    # Адрес Grafana для ссылок в интерфейсе: пусто — тот же компьютер, порт ниже
    grafana_url: str = ""
    grafana_port: int = 3000
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_host=_env("DB_HOST", cls.db_host),
            db_port=int(_env("DB_PORT", str(cls.db_port))),
            db_name=_env("DB_NAME", cls.db_name),
            db_user=_env("DB_USER", cls.db_user),
            db_password=os.environ.get("DB_PASSWORD", cls.db_password),
            reader_user=_env("GRAFANA_DB_USER", cls.reader_user),
            reader_password=os.environ.get("GRAFANA_DB_PASSWORD", cls.reader_password),
            grafana_url=_env("GRAFANA_URL").rstrip("/"),
            grafana_port=int(_env("GRAFANA_PORT", str(cls.grafana_port))),
            host=_env("WEB_HOST", cls.host),
            port=int(_env("WEB_PORT", str(cls.port))),
            log_level=_env("LOG_LEVEL", cls.log_level).upper(),
        )
