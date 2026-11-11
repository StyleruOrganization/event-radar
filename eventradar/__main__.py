import asyncio
import logging
import os
import time
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from eventradar.ai import Intelligence
from eventradar.config import Settings
from eventradar.db import Database
from eventradar.handlers import create_router
from eventradar.reader import Reader
from eventradar.routes import Routes
from eventradar.service import Service
from eventradar.runtime import instance_lock

logger = logging.getLogger(__name__)


async def main() -> None:
    os.umask(0o077)
    settings = Settings()
    with instance_lock(settings.database_path):
        await run(settings)


async def run(settings: Settings) -> None:
    db = Database(settings.database_path)
    await db.initialize()
    ai = Intelligence(settings, db)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    reader = Reader(settings, db)
    routes = Routes(ai, db)
    service = Service(settings, db, ai, bot, routes, reader)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(service, reader))

    async def heartbeat() -> None:
        while True:
            await db.execute("INSERT INTO metadata(key,value) VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
            await asyncio.sleep(20)

    async def worker() -> None:
        next_reports = 0.0
        while True:
            if time.monotonic() >= next_reports:
                users = await db.rows("SELECT id FROM users WHERE enabled=1 AND id=?", (settings.owner_telegram_id,))
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

    tasks: list[asyncio.Task] = []
    try:
        await reader.connect()
        identity = await bot.get_me()
        service.bot_username = identity.username
        await bot.delete_my_commands()
        await bot.set_my_commands(
            [
                BotCommand(command="didgest", description="Дайджест по моим каналам"),
                BotCommand(command="add", description="Добавить публичный канал"),
                BotCommand(command="remove", description="Удалить канал из моей подборки"),
                BotCommand(command="channels", description="Мои каналы"),
                BotCommand(command="prompt", description="Мои интересы для дайджеста"),
                BotCommand(command="help", description="Доступные команды"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands([
            BotCommand(command="help", description="Все команды"),
            BotCommand(command="channels", description="Мои каналы"),
            BotCommand(command="digest", description="Мой дайджест"),
            BotCommand(command="prompt", description="Мои интересы"),
            BotCommand(command="ai", description="Подключение подписки ChatGPT"),
            BotCommand(command="destination", description="Куда публиковать готовые отчеты"),
            BotCommand(command="publish", description="Опубликовать готовый отчет без AI"),
            BotCommand(command="settings", description="Мои настройки"),
        ], scope=BotCommandScopeChat(chat_id=settings.owner_telegram_id))
        # Stale successful heartbeats must not make a newly starting process healthy.
        await db.execute("DELETE FROM metadata WHERE key='heartbeat'")
        tasks = [asyncio.create_task(coroutine) for coroutine in (
            heartbeat(), worker(),
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
        await ai.client.aclose()


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
