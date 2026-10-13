"""historian2grafana: AVEVA Historian → база TimescaleDB → графики."""

import os
from pathlib import Path

__version__ = "1.0.0"

# Старые серверы SQL Server умеют только TLS 1.0/1.1 или подписывают
# сертификат по SHA-1 — современный OpenSSL с ними не соединяется. Флаг
# HISTORIAN_OLD_TLS=1 разрешает такие соединения. Настройку OpenSSL нужно
# задать до первой загрузки библиотеки, поэтому — здесь, при импорте пакета.
if os.environ.get("HISTORIAN_OLD_TLS", "").strip().lower() in {"1", "true", "yes", "on"}:
    os.environ.setdefault("OPENSSL_CONF",
                          str(Path(__file__).resolve().parent.parent / "docker" / "openssl-old-tls.cnf"))
