"""Запуск: python -m app"""

import logging

import uvicorn

from .config import Settings
from .main import create_app


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
