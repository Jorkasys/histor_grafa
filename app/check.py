"""Проверка связи с историком из командной строки.

    python -m app.check                 — проверить сохранённые настройки
    python -m app.check СЕРВЕР [ПОРТ]   — найдётся ли имя и открыт ли порт

В Docker:  docker compose exec app python -m app.check
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time

from .config import Settings
from .db import Database
from .historian import check, normalize


def network(host: str, port: int) -> int:
    print(f"Имя {host}:")
    try:
        addresses = sorted({a[4][0] for a in socket.getaddrinfo(host, port, socket.AF_INET)})
    except socket.gaierror as exc:
        print(f"  ✗ не находится ({exc}). Укажите полное имя с доменом или IP-адрес")
        return 1
    print(f"  ✓ адрес: {', '.join(addresses)}")
    print(f"Порт {port}:")
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as exc:
        print(f"  ✗ закрыт или недоступен ({exc}). Проверьте порт и брандмауэр")
        return 1
    print(f"  ✓ открыт, ответ за {(time.monotonic() - started) * 1000:.0f} мс")
    return 0


async def saved() -> int:
    db = Database(Settings.from_env())
    await db.connect(wait_s=10)
    try:
        config = normalize(await db.load_setting("historian") or {}, strict=False)
    finally:
        await db.close()
    if not config["server"]:
        print("Подключение ещё не настроено: откройте страницу «Подключение» в браузере")
        return 1
    print(f"Сервер {config['server']}, база {config['database']}, вход: "
          f"{'учётная запись Windows' if config['auth'] == 'windows' else 'логин SQL Server'} "
          f"{config['user']}\n")
    steps = await check(config)
    for step in steps:
        print(f"{'✓' if step.ok else '✗'} {step.title}: {step.detail}")
        for item in step.items:
            print(f"    {item}")
    return 0 if all(s.ok for s in steps) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args:
        return network(args[0], int(args[1]) if len(args) > 1 else 1433)
    return asyncio.run(saved())


if __name__ == "__main__":
    sys.exit(main())
