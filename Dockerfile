# Образ приложения historian2grafana.
#
# База — Debian 12 (bookworm): для неё у Microsoft есть пакет ODBC-драйвера
# SQL Server. Сборка ставит драйвер из репозитория Microsoft и тем самым
# принимает его лицензию (ACCEPT_EULA=Y). Второй драйвер — FreeTDS — входит
# в библиотеку pymssql: через него работает вход учётной записью Windows.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FREETDSCONF=/app/docker/freetds.conf

WORKDIR /app

# unixODBC и Microsoft ODBC Driver 18 for SQL Server
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
       -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY docker/ ./docker/

RUN useradd --system --uid 10001 --create-home historian \
    && chown -R historian:historian /app
USER historian

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app"]
