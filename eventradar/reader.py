import logging
import time
from datetime import UTC, datetime, timedelta

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, MessageEntityTextUrl, MessageEntityUrl

from eventradar.ai import DailyBudgetExceeded, Intelligence
from eventradar.config import Settings
from eventradar.db import Database
from eventradar.domain import fingerprint

logger = logging.getLogger(__name__)


class Reader:
    def __init__(self, settings: Settings, db: Database, ai: Intelligence):
        self.settings, self.db, self.ai = settings, db, ai
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

    async def collect(self) -> None:
        if time.time() < self.cooldown_until:
            return
        channels = await self.db.rows("SELECT DISTINCT c.* FROM channels c JOIN subscriptions s ON s.channel_id=c.id")
        for channel in channels:
            try:
                entity = await self.client.get_entity(channel["username"])
                if entity.id != channel["id"]:
                    raise ValueError("Channel username now belongs to another channel")
                cursor = channel["cursor"]
                since = datetime.now(UTC) - timedelta(days=self.settings.initial_lookback_days)
                if not cursor:
                    # A boundary cursor avoids importing the entire history on first subscription.
                    async for boundary in self.client.iter_messages(entity, offset_date=since, limit=1):
                        cursor = boundary.id
                async for message in self.client.iter_messages(
                    entity, min_id=cursor, reverse=True, limit=self.settings.max_posts_per_cycle,
                ):
                    await self.save(channel["id"], message)
                    cursor = max(cursor, message.id)
                    await self.db.execute("UPDATE channels SET cursor=? WHERE id=?", (cursor, channel["id"]))
                # Refresh recent edits and detect deleted recent posts without losing the history cursor.
                recent = [message async for message in self.client.iter_messages(entity, limit=30)]
                for message in recent:
                    if message.id <= cursor and message.date >= since:
                        await self.save(channel["id"], message)
                if recent:
                    low, high = min(m.id for m in recent), max(m.id for m in recent)
                    existing = {m.id for m in recent}
                    for post in await self.db.rows(
                        "SELECT id,message_id FROM posts WHERE channel_id=? AND message_id BETWEEN ? AND ?",
                        (channel["id"], low, high),
                    ):
                        if post["message_id"] not in existing:
                            await self.db.execute("UPDATE items SET active=0 WHERE post_id=?", (post["id"],))
                await self.db.execute("UPDATE channels SET error=NULL WHERE id=?", (channel["id"],))
            except FloodWaitError as error:
                self.cooldown_until = time.time() + error.seconds + 5
                await self.db.execute("UPDATE channels SET error=? WHERE id=?", (f"Telegram: пауза {error.seconds} с", channel["id"]))
                break
            except Exception as error:
                logger.warning("Channel %s unavailable (%s)", channel["id"], type(error).__name__)
                await self.db.execute("UPDATE channels SET error=? WHERE id=?", ("Источник временно недоступен", channel["id"]))

    async def extract(self) -> None:
        pending = await self.db.rows(
            "SELECT p.* FROM posts p WHERE (processed_hash IS NULL OR processed_hash!=content_hash) "
            "AND retry_after<=? AND attempts<8 AND EXISTS(SELECT 1 FROM subscriptions s WHERE s.channel_id=p.channel_id) "
            "ORDER BY p.id LIMIT ?", (time.time(), self.settings.max_posts_per_cycle),
        )
        for post in pending:
            try:
                result = await self.ai.extract(post["text"], post["published_at"])
                async with self.db.transaction() as connection:
                    # A new content version gets new immutable card IDs, preserving invitation history.
                    await connection.execute("UPDATE items SET active=0 WHERE post_id=?", (post["id"],))
                    for ordinal, item in enumerate(result.items):
                        await connection.execute(
                            "INSERT INTO items(post_id,ordinal,version,payload) VALUES(?,?,?,?) "
                            "ON CONFLICT(post_id,version,ordinal) DO UPDATE SET active=1",
                            (post["id"], ordinal, post["content_hash"], item.model_dump_json()),
                        )
                    await connection.execute("UPDATE posts SET processed_hash=?,attempts=0 WHERE id=?", (post["content_hash"], post["id"]))
            except DailyBudgetExceeded:
                break
            except Exception as error:
                logger.warning("Extraction %s failed (%s)", post["id"], type(error).__name__)
                await self.db.execute(
                    "UPDATE posts SET attempts=attempts+1,retry_after=? WHERE id=?",
                    (time.time() + min(21600, 60 * 2 ** post["attempts"]), post["id"]),
                )
