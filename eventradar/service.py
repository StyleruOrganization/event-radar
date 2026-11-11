import asyncio
import logging
import json
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from eventradar.ai import DailyBudgetExceeded, Intelligence, SubscriptionUnavailable
from eventradar.chatgpt_auth import AuthRequired
from eventradar.config import DEFAULT_PROMPT, Settings
from eventradar.db import Database, message_chunks
from eventradar.domain import AnalyzedPost, Item, fingerprint
from eventradar.reader import Reader
from eventradar.routes import Routes

logger = logging.getLogger(__name__)


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


class Service:
    def __init__(self, settings: Settings, db: Database, ai: Intelligence, bot: Bot, routes: Routes, reader: Reader):
        self.settings, self.db, self.ai, self.bot, self.routes = settings, db, ai, bot, routes
        self.reader = reader
        self.digest_lock = asyncio.Lock()
        self.last_sent = 0.0
        self.send_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        self.bot_username: str | None = None

    async def accessible_item(self, user_id: int, item_id: int) -> dict | None:
        return await self.db.one(
            "SELECT i.*,p.message_id,c.username FROM items i JOIN posts p ON p.id=i.post_id "
            "JOIN channels c ON c.id=p.channel_id WHERE i.id=? AND i.active=1 "
            "AND p.processed_hash=p.content_hash AND ("
            "EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=? AND s.channel_id=c.id) OR "
            "EXISTS(SELECT 1 FROM deliveries d WHERE d.item_id=i.id AND d.user_id=?))",
            (item_id, user_id, user_id),
        )

    async def card(self, record: dict, profile: dict, reason: str = "") -> str:
        item = Item.model_validate_json(record["payload"])
        parts = [f"📌 {item.title}\n🆔 #{record['id']}", item.summary]
        if item.starts_at:
            local = item.starts_at.astimezone(ZoneInfo(profile["timezone"]))
            parts.append(f"🗓 Когда: {local:%d.%m.%Y %H:%M} ({profile['timezone']})")
        if item.venue:
            parts.append("📍 Где: " + ", ".join(value for value in (item.city, item.venue) if value))
        if item.registration_url:
            parts.append("🎟 Регистрация: " + item.registration_url)
        if item.cancelled:
            parts.append("❌ Событие отменено по данным публикации.")
        if reason:
            parts.append("✨ Почему подходит: " + reason)
        parts.append(f"🔗 Источник: https://t.me/{record['username']}/{record['message_id']}")
        route = await self.routes.describe({**profile, "routing": 0}, item)
        if route:
            parts.append(route)
        return "\n\n".join(parts)

    def item_buttons(self, record: dict) -> InlineKeyboardMarkup:
        item = Item.model_validate_json(record["payload"])
        source = f"https://t.me/{record['username']}/{record['message_id']}"
        share = "https://t.me/share/url?" + urlencode({"url": source, "text": item.title})
        rows = [[InlineKeyboardButton(text="↗️ Поделиться постом", url=share)]]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def send(self, user_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        async with self.send_lock:
            for attempt in range(3):
                await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.last_sent)))
                try:
                    await self.bot.send_message(user_id, text, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
                    self.last_sent = time.monotonic()
                    return
                except TelegramRetryAfter as error:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(error.retry_after + 1)

    async def send_photo(self, chat_id: int, data: bytes, caption: str, markup: InlineKeyboardMarkup | None) -> None:
        async with self.send_lock:
            for attempt in range(3):
                await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.last_sent)))
                try:
                    await self.bot.send_photo(chat_id, BufferedInputFile(data, filename="event.jpg"), caption=caption, reply_markup=markup)
                    self.last_sent = time.monotonic()
                    return
                except TelegramRetryAfter as error:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(error.retry_after + 1)

    async def send_card(self, chat_id: int, record: dict, profile: dict, photo_sources: set[tuple[str, int]], photo_budget: int, *, shared: bool = False) -> bool:
        text = await self.card(record, {**profile, "routing": 0} if shared else profile, record.get("reason", ""))
        markup = self.item_buttons(record)
        source = (record["username"], record["message_id"])
        if photo_budget > 0 and source not in photo_sources:
            photo_sources.add(source)
            data = await self.reader.photo(*source)
            if data:
                chunks = message_chunks(text, limit=1000)
                try:
                    await self.send_photo(chat_id, data, chunks[0], markup if len(chunks) == 1 else None)
                except TelegramBadRequest:
                    logger.warning("Source photo rejected; sending complete text card")
                else:
                    if len(chunks) > 1:
                        remainder = message_chunks("\n\n".join(chunks[1:]))
                        for index, chunk in enumerate(remainder):
                            await self.send(chat_id, chunk, markup if index == len(remainder) - 1 else None)
                    return True
        chunks = message_chunks(text)
        for index, chunk in enumerate(chunks):
            await self.send(chat_id, chunk, markup if index == len(chunks) - 1 else None)
        return False

    async def destination(self) -> dict | None:
        row = await self.db.one("SELECT value FROM metadata WHERE key='report_destination'")
        return json.loads(row["value"]) if row else None

    async def bind_destination(self, chat_id: int) -> dict:
        if chat_id >= 0:
            raise ValueError("Expected a group or channel ID")
        chat = await self.bot.get_chat(chat_id)
        if chat.type not in {"group", "supergroup", "channel"} or chat.username:
            raise ValueError("Only private groups and channels may receive reports")
        owner = await self.bot.get_chat_member(chat_id,self.settings.owner_telegram_id)
        if owner.status not in {"creator", "administrator"}:
            raise ValueError("Owner must administer the destination")
        bot_member = await self.bot.get_chat_member(chat_id,self.bot.id)
        if bot_member.status in {"left", "kicked"} or (bot_member.status == "restricted" and not bot_member.can_send_messages):
            raise ValueError("Bot is not a member")
        if chat.type == "channel" and (bot_member.status != "administrator" or not bot_member.can_post_messages):
            raise ValueError("Bot cannot publish channel posts")
        target = {"id":chat.id,"title":chat.title or str(chat.id)}
        await self.db.execute("INSERT INTO metadata(key,value) VALUES('report_destination',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(target,ensure_ascii=False),))
        return target

    async def bootstrap_snapshot(self, user: dict) -> dict | None:
        """Make already delivered owner cards publishable without rerunning AI."""
        if not user.get("last_report_at"):
            return None
        rows = await self.db.rows(
            "SELECT i.id,i.payload,p.message_id,c.username FROM deliveries d JOIN items i ON i.id=d.item_id "
            "JOIN posts p ON p.id=i.post_id JOIN channels c ON c.id=p.channel_id "
            "WHERE d.user_id=? AND d.sent_at>=? ORDER BY d.sent_at,i.id",
            (user["id"], user["last_report_at"]),
        )
        summary = f"📬 Сохраненные карточки предыдущего отчета\n🎯 Карточек: {len(rows)}"
        await self.db.execute(
            "INSERT OR IGNORE INTO report_snapshots(user_id,period_end,timezone,cards,summary) VALUES(?,?,?,?,?)",
            (user["id"], user["last_report_at"], user["timezone"], json.dumps(rows, ensure_ascii=False), summary),
        )
        return await self.db.one("SELECT * FROM report_snapshots WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],))

    async def publish_latest(self) -> str:
        """Publish an owner's cached report; this path never calls AI."""
        async with self.publish_lock:
            destination = await self.destination()
            if not destination:
                return "Сначала выберите приватную группу или канал через /destination в личном чате."
            chat = await self.bot.get_chat(destination["id"])
            if chat.type not in {"group", "supergroup", "channel"} or chat.username:
                raise ValueError("Report destination is no longer a private group or channel")
            user = await self.db.one("SELECT * FROM users WHERE id=?", (self.settings.owner_telegram_id,))
            if not user:
                return "Сначала настройте личный профиль."
            snapshot = await self.db.one("SELECT * FROM report_snapshots WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],))
            if snapshot is None:
                snapshot = await self.bootstrap_snapshot(user)
            if snapshot is None:
                return "Готового отчета пока нет. Владелец может подготовить его через /digest."
            await self.db.execute("INSERT OR IGNORE INTO report_publications(snapshot_id,chat_id) VALUES(?,?)", (snapshot["id"], destination["id"]))
            state = await self.db.one("SELECT * FROM report_publications WHERE snapshot_id=? AND chat_id=?", (snapshot["id"], destination["id"]))
            if state["summary_sent"]:
                return "Последний отчет уже опубликован в выбранном чате. Новых AI-запросов не было."
            cards = json.loads(snapshot["cards"])
            profile = {**user, "timezone": snapshot["timezone"], "routing": 0}
            sources = {(card["username"], card["message_id"]) for card in cards[:state["next_card"]]}
            photos = state["photos"]
            for index in range(state["next_card"], len(cards)):
                current = await self.destination()
                if not current or current["id"] != destination["id"]:
                    return "Публикация остановлена: группа отключена или изменена."
                photos += int(await self.send_card(destination["id"], cards[index], profile, sources, max(0, self.settings.max_photos_per_report - photos), shared=True))
                await self.db.execute("UPDATE report_publications SET next_card=?,photos=? WHERE snapshot_id=? AND chat_id=?", (index + 1, photos, snapshot["id"], destination["id"]))
            for chunk in message_chunks(snapshot["summary"]):
                await self.send(destination["id"], chunk)
            await self.db.execute("UPDATE report_publications SET summary_sent=1 WHERE snapshot_id=? AND chat_id=?", (snapshot["id"], destination["id"]))
            return "📬 Отчет опубликован в выбранном чате без повторного AI-анализа."

    async def analyze(self, posts: list[dict], user: dict) -> list[dict]:
        if not posts:
            return []
        await self.ai.prepare()
        context = fingerprint("report-analysis-v1", user["prompt"], user["city"], user["region"], self.settings.source_timezone, self.ai.model_key)
        results: dict[int, AnalyzedPost] = {}
        pending: list[dict] = []
        keys = {post["id"]: fingerprint(post["content_hash"], context) for post in posts}
        for post in posts:
            cached = await self.db.one(
                "SELECT payload FROM report_analyses WHERE user_id=? AND post_id=? AND fingerprint=?",
                (user["id"], post["id"], keys[post["id"]]),
            )
            if cached:
                results[post["id"]] = AnalyzedPost.model_validate_json(cached["payload"])
            else:
                pending.append(post)

        async def process(batch: list[dict]) -> None:
            analysis = await self.ai.analyze_posts(batch, user)
            async with self.db.transaction() as connection:
                for publication in analysis.publications:
                    await connection.execute(
                        "INSERT INTO report_analyses(user_id,post_id,fingerprint,payload) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id,post_id) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload",
                        (user["id"], publication.post_id, keys[publication.post_id], publication.model_dump_json()),
                    )
                    results[publication.post_id] = publication

        batch: list[dict] = []
        characters = 0
        for post in pending:
            size = len(post["text"])
            if batch and (len(batch) >= self.settings.ai_batch_size or characters + size > self.settings.ai_batch_max_characters):
                await process(batch)
                batch, characters = [], 0
            batch.append(post)
            characters += size
        if batch:
            await process(batch)

        records: list[dict] = []
        async with self.db.transaction() as connection:
            for post in posts:
                publication = results[post["id"]]
                version = keys[post["id"]]
                await connection.execute("UPDATE items SET active=0 WHERE post_id=?", (post["id"],))
                for ordinal, recommendation in enumerate(publication.recommendations):
                    payload = recommendation.item.model_dump_json()
                    await connection.execute(
                        "INSERT INTO items(post_id,ordinal,version,payload) VALUES(?,?,?,?) "
                        "ON CONFLICT(post_id,version,ordinal) DO UPDATE SET active=1",
                        (post["id"], ordinal, version, payload),
                    )
                    cursor = await connection.execute("SELECT id FROM items WHERE post_id=? AND version=? AND ordinal=?", (post["id"], version, ordinal))
                    item_id = (await cursor.fetchone())[0]
                    records.append({"id": item_id, "payload": payload, "username": post["username"], "message_id": post["message_id"], "reason": recommendation.reason})
                await connection.execute("UPDATE posts SET processed_hash=content_hash WHERE id=?", (post["id"],))
        return records

    async def digest(self, user_id: int, manual: bool = False, *, reset_period: bool = False) -> None:
        async with self.digest_lock:
            user = await self.db.one("SELECT * FROM users WHERE id=?", (user_id,))
            is_owner = user_id == self.settings.owner_telegram_id
            if not user or (not manual and (not is_owner or not user["enabled"])):
                return
            until = datetime.now(UTC).replace(microsecond=0)
            local = until.astimezone(ZoneInfo(user["timezone"]))
            day = local.date().isoformat()
            if not manual and (
                user["last_report"] == day or user["last_report_attempt"] == day or local.strftime("%H:%M") < user["report_time"]
            ):
                return
            if not manual:
                # Failed scheduled reports must not turn into an AI retry loop every minute.
                await self.db.execute("UPDATE users SET last_report_attempt=? WHERE id=?", (day, user_id))
            if reset_period:
                since = until - timedelta(days=7)
            elif user["last_report_at"]:
                since = datetime.fromisoformat(user["last_report_at"])
            else:
                since = until - timedelta(days=self.settings.initial_lookback_days)
            if since >= until:
                await self.send(user_id, "С последнего отчета еще не начался новый период.")
                return
            retry_message = "Повторите /didgest reset позже." if reset_period else (
                "Повторите /digest позже или дождитесь следующего времени отчета."
            )
            try:
                failures = await self.reader.collect(since, until, user_id=user_id)
                if failures:
                    details = "\n".join(failures)
                    for chunk in message_chunks(
                        "Отчет пока не подготовлен: не удалось полностью прочитать каналы. "
                        f"Период не закрыт, AI-анализ не запускался.\n{details}\n{retry_message}"
                    ):
                        await self.send(user_id, chunk)
                    return
                posts = await self.db.rows(
                    "SELECT p.*,c.username FROM posts p JOIN channels c ON c.id=p.channel_id "
                    "JOIN subscriptions s ON s.channel_id=p.channel_id WHERE s.user_id=? "
                    "AND p.published_at>=? AND p.published_at<? ORDER BY p.published_at,p.id",
                    (user_id, since.isoformat(), until.isoformat()),
                )
                records = await self.analyze(posts, user)
                seen: set[str] = set()
                sent, photos = 0, 0
                photo_sources: set[tuple[str, int]] = set()
                snapshot_cards: list[dict] = []
                for record in records:
                    item = Item.model_validate_json(record["payload"])
                    if user["prompt"] == DEFAULT_PROMPT and (item.kind != "event" or not item.starts_at or not item.venue or not item.registration_url):
                        continue
                    if item.cancelled or (item.starts_at and item.starts_at <= until):
                        continue
                    event_key = fingerprint(item.title.casefold().strip(), str(item.starts_at), (item.venue or "").casefold())
                    if event_key in seen:
                        continue
                    seen.add(event_key)
                    snapshot_cards.append(record)
                    payload_hash = fingerprint(record["payload"])
                    if not reset_period:
                        delivered = await self.db.one(
                            "SELECT fingerprint FROM deliveries WHERE user_id=? AND item_id=?",
                            (user_id, record["id"]),
                        )
                        if delivered and delivered["fingerprint"] == payload_hash:
                            continue
                    photos += int(await self.send_card(user_id, record, user, photo_sources, max(0, self.settings.max_photos_per_report - photos)))
                    await self.db.execute(
                        "INSERT INTO deliveries(user_id,item_id,fingerprint,sent_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id,item_id) DO UPDATE SET fingerprint=excluded.fingerprint,sent_at=excluded.sent_at",
                        (user_id, record["id"], payload_hash, datetime.now(UTC).isoformat()),
                    )
                    sent += 1
                start_local = since.astimezone(ZoneInfo(user["timezone"]))
                sent_label = "Карточек отправлено" if reset_period else "Новых отправлено"
                summary = (
                    f"📬 Отчет за {start_local:%d.%m %H:%M} — {local:%d.%m %H:%M} ({user['timezone']})\n"
                    f"📚 Публикаций: {len(posts)}\n🎯 Подходящих карточек: {len(snapshot_cards)}\n"
                    f"📨 {sent_label}: {sent}"
                )
                if not records:
                    summary += " Подходящих публикаций не найдено."
                # Only a fully delivered report advances the time boundary. Messages arriving during
                # analysis belong to the next period, even when delivery takes several minutes.
                await self.send(user_id, summary)
                async with self.db.transaction() as connection:
                    await connection.execute(
                        "INSERT OR IGNORE INTO report_snapshots(user_id,period_end,timezone,cards,summary) VALUES(?,?,?,?,?)",
                        (user_id, until.isoformat(), user["timezone"], json.dumps(snapshot_cards, ensure_ascii=False), summary),
                    )
                    if manual:
                        await connection.execute("UPDATE users SET last_report_at=? WHERE id=?", (until.isoformat(), user_id))
                    else:
                        await connection.execute("UPDATE users SET last_report_at=?,last_report=? WHERE id=?", (until.isoformat(), day, user_id))
                if is_owner and not reset_period and await self.destination():
                    try:
                        await self.publish_latest()
                    except Exception as error:
                        logger.warning("Shared publication failed (%s)", type(error).__name__)
                        await self.send(user_id, "Личный отчет завершен, но общая публикация прервалась. Проверьте доступ бота и повторите /publish; AI повторно не вызывается.")
            except (DailyBudgetExceeded, SubscriptionUnavailable, AuthRequired) as error:
                await self.send(
                    user_id,
                    f"{error}\nОтчет не завершен. Период сохранен. Готовые результаты кешируются. {retry_message}",
                )
            except Exception:
                logger.exception("Report preparation failed", exc_info=False)
                await self.send(
                    user_id,
                    "Не удалось завершить отчет. Период не закрыт. "
                    f"Уже отправленные карточки и обработанные публикации сохранены. {retry_message}",
                )
