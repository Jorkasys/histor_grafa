# historian2grafana

Копирует значения тегов из **AVEVA Historian** в собственную базу **TimescaleDB** раз в несколько
секунд и показывает по ней графики — на странице приложения и в **Grafana**. Разворачивается
одной командой в **Docker Desktop** на Windows.

![Страница «Загрузка»](docs/img/loading.png)

## Что умеет

- **Подключение к AVEVA Historian** через его SQL Server (база `Runtime`): логином SQL Server
  или доменной учётной записью Windows (`ДОМЕН\логин` и пароль). Историк только читается.
- **Выбор тегов масками** по таблице `Runtime.dbo.Tag`: `Lehmann1_*, CP2_*`, кроме `Sys*`.
  Новые теги подхватываются сами.
- **Проверка подключения по шагам**: вход, список тегов, чтение значений и оценка объёма данных.
- **Копирование раз в 5 секунд**: при первом запуске — история за заданное число часов, дальше —
  только новые значения. Без дублей; после перерыва пропущенное докопируется само.
- **Графики**: страница «Графики» в приложении и готовый дашборд Grafana «Теги историка».

## Быстрый старт

Нужны Windows 10/11 с [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
и сетевой доступ к серверу историка (порт 1433).

```powershell
git clone <адрес этого репозитория> C:\historian2grafana
cd C:\historian2grafana
Copy-Item .env.example .env
docker compose up -d --build
```

1. Откройте **http://localhost:8000** → «Подключение»: сервер историка, логин и пароль, маски тегов.
2. **Проверить подключение** → **Сохранить и начать копирование**.
3. Графики — на странице «Графики» или в Grafana: **http://localhost:3000**.

Подробная инструкция — от установки Docker Desktop до решения проблем:
[docs/INSTALL.md](docs/INSTALL.md) (то же в Word: `docs/Инструкция.docx`).

## Как устроено

| Контейнер | Что делает | Адрес |
|---|---|---|
| `h2g-app` | Подключение к историку, копирование, страницы в браузере | http://localhost:8000 |
| `h2g-db` | PostgreSQL 16 + TimescaleDB: теги и их значения | — |
| `h2g-grafana` | Grafana с дашбордом «Теги историка» | http://localhost:3000 |

```
AVEVA Historian ──(раз в 5 с, только чтение)──► h2g-app ──► h2g-db ──► графики и Grafana
```

## Для разработчиков

Приложение — Python 3.12 (FastAPI, asyncpg), страницы — HTML и JavaScript без сборки.

```powershell
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Тестам с базой нужен PostgreSQL (лучше с TimescaleDB): адрес задаётся переменными
`TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_USER`, `TEST_DB_PASSWORD`; без него эти тесты
пропускаются. Историк в тестах заменяет эмулятор на SQLite с таблицами того же вида, что в `Runtime`.

## Лицензия

MIT — см. [LICENSE](LICENSE).
