import asyncio
import logging

from catbot.app import run_polling, run_webhook
from catbot.config import Settings


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
        if settings.mode == "webhook":
            run_webhook(settings)
        else:
            asyncio.run(run_polling(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
