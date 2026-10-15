"""historian2grafana без Docker: PostgreSQL, Grafana и приложение из этой папки.

Комплект собирает offline/build-offline.ps1 на компьютере с интернетом. На
компьютере без интернета всё запускается файлами:

    start.bat   →  python launcher.py start   — база, Grafana и приложение
    stop.bat    →  python launcher.py stop    — остановить базу и Grafana
    check.bat   →  python launcher.py check [СЕРВЕР [ПОРТ]] — проверка связи

Порты и пароли — в файле .env рядом (создаётся из .env.example при первом
запуске). Данные — в папке data.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
DATA = ROOT / "data"
PG_BIN = RUNTIME / "pgsql" / "bin"
PG_DATA = DATA / "postgres"
PG_LOG = DATA / "postgres.log"
GRAFANA_HOME = RUNTIME / "grafana"
GRAFANA_DATA = DATA / "grafana"
GRAFANA_LOG = DATA / "grafana.log"
GRAFANA_PID = DATA / "grafana.pid"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

DB_NAME = "historian"
DB_USER = "historian"
READER = "grafana_reader"
DEFAULTS = {
    "APP_PORT": "8000", "GRAFANA_PORT": "3000", "DB_PORT": "55432",
    "DB_PASSWORD": "historian", "GRAFANA_DB_PASSWORD": "grafana_reader",
    "GRAFANA_ADMIN_USER": "admin", "GRAFANA_ADMIN_PASSWORD": "admin",
    "GRAFANA_URL": "", "HISTORIAN_OLD_TLS": "0", "LOG_LEVEL": "INFO",
}


class Stop(Exception):
    """Понятная ошибка запуска: печатаем и выходим."""


# ---------------------------------------------------------------------------
#  Настройки
# ---------------------------------------------------------------------------
def settings() -> dict[str, str]:
    env_file = ROOT / ".env"
    if not env_file.exists() and (ROOT / ".env.example").exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
    values = dict(DEFAULTS)
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    for key in ("APP_PORT", "GRAFANA_PORT", "DB_PORT"):
        if not values[key].isdigit():
            raise Stop(f"в файле .env {key} должен быть числом, а там {values[key]!r}")
    return values


def app_environment(cfg: dict[str, str]) -> None:
    """Переменные окружения для приложения — до его импорта."""
    os.environ.update({
        "DB_HOST": "127.0.0.1", "DB_PORT": cfg["DB_PORT"], "DB_NAME": DB_NAME,
        "DB_USER": DB_USER, "DB_PASSWORD": cfg["DB_PASSWORD"],
        "GRAFANA_DB_USER": READER, "GRAFANA_DB_PASSWORD": cfg["GRAFANA_DB_PASSWORD"],
        "GRAFANA_PORT": cfg["GRAFANA_PORT"], "GRAFANA_URL": cfg["GRAFANA_URL"],
        "WEB_HOST": "0.0.0.0", "WEB_PORT": cfg["APP_PORT"],
        "HISTORIAN_OLD_TLS": cfg["HISTORIAN_OLD_TLS"], "LOG_LEVEL": cfg["LOG_LEVEL"],
        "FREETDSCONF": str(ROOT / "docker" / "freetds.conf"),
    })


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def tail(path: Path, lines: int = 15) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])


# ---------------------------------------------------------------------------
#  PostgreSQL
# ---------------------------------------------------------------------------
def pg_running() -> bool:
    if not (PG_DATA / "PG_VERSION").exists():
        return False
    result = subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", str(PG_DATA), "status"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    return result.returncode == 0


def init_postgres(cfg: dict[str, str]) -> None:
    print("  Первый запуск: создаю базу данных в папке data\\postgres…")
    DATA.mkdir(exist_ok=True)
    pwfile = DATA / "initdb.pw"
    pwfile.write_text(cfg["DB_PASSWORD"], encoding="utf-8")
    try:
        # initdb не оставляет фоновых процессов — его вывод можно читать целиком
        result = subprocess.run(
            [str(PG_BIN / "initdb.exe"), "-D", str(PG_DATA), "-U", DB_USER,
             f"--pwfile={pwfile}", "-E", "UTF8", "--no-locale", "-A", "scram-sha-256"],
            capture_output=True, encoding="oem", errors="replace", creationflags=NO_WINDOW)
    finally:
        pwfile.unlink(missing_ok=True)
    if result.returncode:
        shutil.rmtree(PG_DATA, ignore_errors=True)
        raise Stop("не удалось создать базу данных:\n" + (result.stderr or result.stdout))
    with open(PG_DATA / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write("\n# historian2grafana: база видна только с этого компьютера\n"
                   "listen_addresses = '127.0.0.1'\n")


def start_postgres(cfg: dict[str, str]) -> bool:
    """Запустить PostgreSQL; False — она уже работала."""
    if not (PG_DATA / "PG_VERSION").exists():
        init_postgres(cfg)
    if pg_running():
        print("  PostgreSQL уже работает")
        return False
    port = int(cfg["DB_PORT"])
    if port_busy(port):
        raise Stop(f"порт {port} занят другой программой — поменяйте DB_PORT в файле .env")
    print(f"  Запускаю PostgreSQL (порт {port})…")
    # Вывод pg_ctl не перехватываем: сервер унаследовал бы наш канал, и мы
    # ждали бы его остановки. Всё, что нужно, сервер пишет в postgres.log
    result = subprocess.run(
        [str(PG_BIN / "pg_ctl.exe"), "-D", str(PG_DATA), "-l", str(PG_LOG),
         "-o", f"-p {port}", "-w", "-t", "90", "start"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW)
    if result.returncode:
        raise Stop("PostgreSQL не запустилась. Последние строки data\\postgres.log:\n"
                   + tail(PG_LOG))
    return True


def stop_postgres() -> None:
    if not pg_running():
        return
    print("  Останавливаю PostgreSQL…")
    subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", str(PG_DATA), "-m", "fast", "-w",
                    "stop"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def create_database(cfg: dict[str, str]) -> None:
    import asyncpg

    async def run() -> None:
        last = None
        for _ in range(30):
            try:
                conn = await asyncpg.connect(host="127.0.0.1", port=int(cfg["DB_PORT"]),
                                             user=DB_USER, password=cfg["DB_PASSWORD"],
                                             database="postgres", timeout=5)
                break
            except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
                last = exc
                await asyncio.sleep(1)
        else:
            raise Stop(f"не удалось подключиться к базе: {last}. Если вы меняли DB_PASSWORD "
                       "после первого запуска — верните прежний пароль в .env")
        try:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME):
                await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
        finally:
            await conn.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
#  Grafana
# ---------------------------------------------------------------------------
def start_grafana(cfg: dict[str, str]) -> subprocess.Popen | None:
    port = int(cfg["GRAFANA_PORT"])
    if port_busy(port):
        print(f"  Grafana уже работает (порт {port} занят)")
        return None
    print(f"  Запускаю Grafana (порт {port})…")
    GRAFANA_DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "GF_PATHS_DATA": str(GRAFANA_DATA),
        "GF_PATHS_LOGS": str(GRAFANA_DATA / "log"),
        "GF_PATHS_PLUGINS": str(GRAFANA_DATA / "plugins"),
        "GF_PATHS_PROVISIONING": str(ROOT / "grafana" / "provisioning"),
        "GF_SERVER_HTTP_PORT": str(port),
        "GF_SECURITY_ADMIN_USER": cfg["GRAFANA_ADMIN_USER"],
        "GF_SECURITY_ADMIN_PASSWORD": cfg["GRAFANA_ADMIN_PASSWORD"],
        "GF_AUTH_ANONYMOUS_ENABLED": "true",
        "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        # Без интернета: не скачивать плагины и новости при запуске
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_NEWS_NEWS_FEED_ENABLED": "false",
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH":
            str(ROOT / "grafana" / "dashboards" / "historian.json"),
        # Для файлов в grafana/provisioning
        "DB_ADDRESS": f"127.0.0.1:{cfg['DB_PORT']}",
        "DB_NAME": DB_NAME,
        "GRAFANA_DB_USER": READER,
        "GRAFANA_DB_PASSWORD": cfg["GRAFANA_DB_PASSWORD"],
        "DASHBOARDS_DIR": str(ROOT / "grafana" / "dashboards"),
    })
    log = open(GRAFANA_LOG, "ab")
    # Без своего окна: Grafana работает в фоне и не мешает окну приложения
    proc = subprocess.Popen([str(GRAFANA_HOME / "bin" / "grafana.exe"), "server",
                             "--homepath", str(GRAFANA_HOME)],
                            cwd=str(GRAFANA_HOME), env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW | NEW_GROUP)
    GRAFANA_PID.write_text(str(proc.pid), encoding="ascii")
    return proc


def stop_grafana() -> None:
    try:
        pid = int(GRAFANA_PID.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return
    print("  Останавливаю Grafana…")
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    GRAFANA_PID.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
#  Команды
# ---------------------------------------------------------------------------
def open_browser_when_ready(port: int) -> None:
    url = f"http://localhost:{port}"
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
                break
        except OSError:
            time.sleep(0.5)
    else:
        return
    print(f"\n  Приложение: {url}")
    webbrowser.open(url)


def start(open_browser: bool = True) -> None:
    cfg = settings()
    app_port = int(cfg["APP_PORT"])
    print("historian2grafana — запуск\n")
    if port_busy(app_port):
        raise Stop(f"порт {app_port} уже занят: приложение, вероятно, уже запущено в другом "
                   "окне. Откройте http://localhost:" + str(app_port) + " или поменяйте "
                   "APP_PORT в .env")
    started_pg = start_postgres(cfg)
    create_database(cfg)
    grafana = start_grafana(cfg)
    print(f"\n  Приложение:  http://localhost:{app_port}")
    print(f"  Grafana:     http://localhost:{cfg['GRAFANA_PORT']}")
    print("\n  Не закрывайте это окно, пока работаете. Остановить: Ctrl+C.\n")
    app_environment(cfg)
    if open_browser:
        threading.Thread(target=open_browser_when_ready, args=(app_port,), daemon=True).start()
    try:
        from app.__main__ import main as run_app
        run_app()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nОстанавливаю…")
        if grafana is not None:
            stop_grafana()
        if started_pg:
            stop_postgres()


def stop() -> None:
    print("historian2grafana — остановка\n")
    stop_grafana()
    stop_postgres()
    print("\n  Готово. Окно приложения (если открыто) закройте вручную.")


def check(args: list[str]) -> int:
    cfg = settings()
    app_environment(cfg)
    if not args:
        # Проверка сохранённых настроек читает их из базы
        start_postgres(cfg)
    from app.check import main as run_check

    sys.argv = ["check", *args]
    return run_check()


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    try:
        if command == "start":
            start(open_browser="--no-browser" not in sys.argv)
        elif command == "stop":
            stop()
        elif command == "check":
            return check(sys.argv[2:])
        else:
            print(__doc__)
            return 2
    except Stop as exc:
        print(f"\nОШИБКА: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
