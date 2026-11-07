import asyncio
import logging
import os
import time
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BotCommand

from eventradar.ai import Intelligence
from eventradar.config import Settings
from eventradar.db import Database
from eventradar.handlers import create_router
from eventradar.reader import Reader
from eventradar.routes import Routes
from eventradar.service import Service

logger = logging.getLogger(__name__)


async def main() -> None:
    os.umask(0o077)
    settings = Settings()
    db = Database(settings.database_path)
    await db.initialize()
    ai = Intelligence(settings, db)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    reader = Reader(settings, db, ai)
    routes = Routes(ai, db)
    service = Service(settings, db, ai, bot, routes)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(service, reader))

    async def heartbeat() -> None:
        while True:
            await db.execute("INSERT INTO metadata(key,value) VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
            await asyncio.sleep(20)

    async def worker() -> None:
        next_collection = 0.0
        next_reports = 0.0
        while True:
            if time.monotonic() >= next_collection:
                await reader.collect()
                await reader.extract()
                next_collection = time.monotonic() + settings.poll_interval_seconds
            if time.monotonic() >= next_reports:
                # Rotating order prevents the same account from always being last at the AI limit.
                users = await db.rows("SELECT id FROM users WHERE enabled=1 ORDER BY id")
                if users:
                    shift = datetime.now(UTC).date().toordinal() % len(users)
                    users = users[shift:] + users[:shift]
                for user in users:
                    try:
                        await service.digest(user["id"])
                    except TelegramForbiddenError:
                        await db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
                    except TelegramRetryAfter as error:
                        await asyncio.sleep(min(error.retry_after + 1, 60))
                    except Exception as error:
                        logger.warning("Scheduled digest failed (%s)", type(error).__name__)
                next_reports = time.monotonic() + 60
            await asyncio.sleep(5)

    async def notifications() -> None:
        while True:
            await service.deliver_outbox()
            await asyncio.sleep(2)

    tasks: list[asyncio.Task] = []
    try:
        await reader.connect()
        await bot.get_me()
        await bot.set_my_commands([
            BotCommand(command="help", description="Все команды"),
            BotCommand(command="channels", description="Мои каналы"),
            BotCommand(command="digest", description="Мой дайджест"),
            BotCommand(command="prompt", description="Мои интересы"),
            BotCommand(command="sharechannels", description="Поделиться каналами"),
            BotCommand(command="friends", description="Мои друзья"),
            BotCommand(command="history", description="Посещенные события"),
            BotCommand(command="settings", description="Мои настройки"),
        ])
        # Stale successful heartbeats must not make a newly starting process healthy.
        await db.execute("DELETE FROM metadata WHERE key='heartbeat'")
        tasks = [asyncio.create_task(coroutine) for coroutine in (
            heartbeat(), worker(), notifications(),
            dispatcher.start_polling(bot, handle_as_tasks=True, close_bot_session=False),
        )]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.execute("DELETE FROM metadata WHERE key='heartbeat'")
        await reader.client.disconnect()
        await bot.session.close()
        await ai.client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Network libraries must never log credential-bearing Telegram URLs or payloads.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logger.critical("Application stopped (%s). Check configuration and service availability.", type(error).__name__)
        raise SystemExit(1) from None
