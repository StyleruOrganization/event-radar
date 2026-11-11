import asyncio
import logging
import time
from datetime import UTC, datetime

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, MessageEntityTextUrl, MessageEntityUrl

from eventradar.config import Settings
from eventradar.db import Database
from eventradar.domain import fingerprint

logger = logging.getLogger(__name__)


class Reader:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        self.client = TelegramClient(
            StringSession(settings.telegram_reader_session.get_secret_value()),
            settings.telegram_api_id, settings.telegram_api_hash.get_secret_value(),
            flood_sleep_threshold=0, receive_updates=False,
        )
        self.cooldown_until = 0.0

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Reader session is not authorized; run python -m eventradar.session")
        me = await self.client.get_me()
        if me.bot:
            raise RuntimeError("Channel reader requires a user account, not a bot account")

    async def resolve(self, username: str) -> tuple[int, str]:
        entity = await self.client.get_entity(username)
        if not isinstance(entity, Channel) or entity.megagroup or not entity.username:
            raise ValueError("Поддерживаются только публичные каналы, не группы и личные чаты.")
        return entity.id, entity.title

    async def photo(self, username: str, message_id: int) -> bytes | None:
        """Read one source illustration, respecting Telegram content protection."""
        if time.time() < self.cooldown_until:
            return None
        maximum = 8 * 1024 * 1024
        try:
            async with asyncio.timeout(30):
                entity = await self.client.get_entity(username)
                if getattr(entity, "noforwards", False):
                    return None
                message = await self.client.get_messages(entity, ids=message_id)
                if not message or not message.photo or getattr(message, "noforwards", False):
                    return None
                size = message.file.size if message.file else None
                if size is None or size > maximum:
                    return None

                def enforce_limit(received: int, _total: int) -> None:
                    if received > maximum:
                        raise ValueError("Source photo is too large")

                data = await self.client.download_media(message, file=bytes, progress_callback=enforce_limit)
                return data if isinstance(data, bytes) and len(data) <= maximum else None
        except FloodWaitError as error:
            self.cooldown_until = time.time() + error.seconds + 5
            return None
        except Exception as error:
            logger.warning("Source illustration unavailable (%s)", type(error).__name__)
            return None

    async def save(self, channel_id: int, message) -> None:
        if not message.message:
            return
        text = message.message
        links: list[str] = []
        encoded = text.encode("utf-16-le")
        for entity in message.entities or []:
            if isinstance(entity, MessageEntityTextUrl):
                links.append(entity.url)
            elif isinstance(entity, MessageEntityUrl):
                links.append(encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le"))
        text = (text + "\nСсылки публикации:\n" + "\n".join(links))[:24000]
        await self.db.execute(
            "INSERT INTO posts(channel_id,message_id,published_at,text,content_hash) VALUES(?,?,?,?,?) "
            "ON CONFLICT(channel_id,message_id) DO UPDATE SET text=excluded.text, "
            "content_hash=excluded.content_hash, attempts=0, retry_after=0 "
            "WHERE posts.content_hash != excluded.content_hash",
            (channel_id, message.id, message.date.astimezone(UTC).isoformat(), text, fingerprint(text)),
        )

    async def collect(self, since: datetime, until: datetime, *, user_id: int) -> list[str]:
        """Load the complete report window. This method never calls AI."""
        if time.time() < self.cooldown_until:
            return ["Telegram просит подождать перед повторным чтением каналов."]
        failures: list[str] = []
        channels = await self.db.rows(
            "SELECT c.* FROM channels c JOIN subscriptions s ON s.channel_id=c.id WHERE s.user_id=? ORDER BY c.id",
            (user_id,),
        )
        for channel in channels:
            try:
                entity = await self.client.get_entity(channel["username"])
                if entity.id != channel["id"]:
                    raise ValueError("Channel username now belongs to another channel")
                # Telethon paginates all messages; no per-cycle cap may truncate the report.
                async for message in self.client.iter_messages(entity, offset_date=until, limit=None):
                    if message.date < since:
                        break
                    if message.date >= until:
                        continue
                    await self.save(channel["id"], message)
                await self.db.execute("UPDATE channels SET error=NULL WHERE id=?", (channel["id"],))
            except FloodWaitError as error:
                self.cooldown_until = time.time() + error.seconds + 5
                detail = f"@{channel['username']}: Telegram просит паузу {error.seconds} с."
                await self.db.execute("UPDATE channels SET error=? WHERE id=?", (detail, channel["id"]))
                failures.append(detail)
                break
            except Exception as error:
                logger.warning("Channel %s unavailable (%s)", channel["id"], type(error).__name__)
                detail = f"@{channel['username']}: источник временно недоступен."
                await self.db.execute("UPDATE channels SET error=? WHERE id=?", (detail, channel["id"]))
                failures.append(detail)
        return failures
