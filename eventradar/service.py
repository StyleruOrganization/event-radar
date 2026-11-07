import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from eventradar.ai import DailyBudgetExceeded, Intelligence
from eventradar.config import DEFAULT_PROMPT, Settings
from eventradar.db import Database, message_chunks
from eventradar.domain import Item, fingerprint, overlaps
from eventradar.routes import Routes

logger = logging.getLogger(__name__)


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


class Service:
    def __init__(self, settings: Settings, db: Database, ai: Intelligence, bot: Bot, routes: Routes):
        self.settings, self.db, self.ai, self.bot, self.routes = settings, db, ai, bot, routes
        self.digest_lock = asyncio.Lock()

    async def accessible_item(self, user_id: int, item_id: int) -> dict | None:
        return await self.db.one(
            "SELECT i.*,p.message_id,c.username FROM items i JOIN posts p ON p.id=i.post_id "
            "JOIN channels c ON c.id=p.channel_id WHERE i.id=? AND i.active=1 "
            "AND p.processed_hash=p.content_hash AND ("
            "EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=? AND s.channel_id=c.id) OR "
            "EXISTS(SELECT 1 FROM invitations v WHERE v.item_id=i.id AND (v.sender=? OR v.recipient=?)) OR "
            "EXISTS(SELECT 1 FROM deliveries d WHERE d.item_id=i.id AND d.user_id=?))",
            (item_id, user_id, user_id, user_id, user_id),
        )

    async def card(self, record: dict, profile: dict, reason: str = "") -> str:
        item = Item.model_validate_json(record["payload"])
        parts = [f"#{record['id']} · {item.title}", item.summary]
        if item.starts_at:
            local = item.starts_at.astimezone(ZoneInfo(profile["timezone"]))
            parts.append(f"Когда: {local:%d.%m.%Y %H:%M} ({profile['timezone']})")
            if not item.ends_at:
                parts.append("Время окончания не указано; для проверки занятости принято 2 часа.")
        if item.venue:
            parts.append("Где: " + ", ".join(value for value in (item.city, item.venue) if value))
        if item.registration_url:
            parts.append("Регистрация: " + item.registration_url)
        if item.cancelled:
            parts.append("Событие отменено по данным публикации.")
        if reason:
            parts.append("Почему подходит: " + reason)
        parts.append(f"Источник: https://t.me/{record['username']}/{record['message_id']}")
        route = await self.routes.describe(profile, item)
        if route:
            parts.append(route)
        return "\n\n".join(parts)

    def item_buttons(self, item_id: int) -> InlineKeyboardMarkup:
        return keyboard([
            [("Пригласить друга", f"invite:{item_id}")],
            [("Я посетил(а)", f"attend:{item_id}"), ("Отзывы", f"reviews:{item_id}")],
        ])

    async def digest(self, user_id: int, manual: bool = False) -> None:
        async with self.digest_lock:
            user = await self.db.one("SELECT * FROM users WHERE id=?", (user_id,))
            if not user or user_id not in self.settings.users or not user["enabled"]:
                return
            now = datetime.now(UTC)
            local = now.astimezone(ZoneInfo(user["timezone"]))
            day = local.date().isoformat()
            if not manual and (user["last_report"] == day or local.strftime("%H:%M") < user["report_time"]):
                return
            rules = await self.db.rows("SELECT * FROM blackouts WHERE user_id=?", (user_id,))
            candidates = await self.db.rows(
                "SELECT i.*,p.message_id,p.published_at,c.username FROM items i "
                "JOIN posts p ON p.id=i.post_id JOIN channels c ON c.id=p.channel_id "
                "JOIN subscriptions s ON s.channel_id=c.id WHERE s.user_id=? AND i.active=1 "
                "AND p.processed_hash=p.content_hash ORDER BY p.published_at DESC,i.id DESC", (user_id,),
            )
            sent, deferred = 0, False
            seen = set()
            for record in candidates:
                item = Item.model_validate_json(record["payload"])
                if user["prompt"] == DEFAULT_PROMPT and (
                    item.kind != "event" or not item.starts_at or not item.venue or not item.registration_url
                ):
                    continue
                if item.cancelled or (item.starts_at and item.starts_at <= now) or overlaps(item, rules, user["timezone"]):
                    continue
                if item.kind == "post" and datetime.fromisoformat(record["published_at"]) < now - timedelta(days=7):
                    continue
                event_key = fingerprint(item.title.casefold().strip(), str(item.starts_at), (item.venue or "").casefold())
                if event_key in seen:
                    continue
                seen.add(event_key)
                payload_hash = fingerprint(record["payload"])
                delivered = await self.db.one("SELECT fingerprint FROM deliveries WHERE user_id=? AND item_id=?", (user_id, record["id"]))
                if delivered and delivered["fingerprint"] == payload_hash:
                    continue
                match_hash = fingerprint(record["payload"], user["prompt"], user["city"], user["region"], self.settings.openai_model)
                decision = await self.db.one("SELECT * FROM decisions WHERE user_id=? AND item_id=? AND fingerprint=?", (user_id, record["id"], match_hash))
                if not decision:
                    try:
                        result = await self.ai.match(item.model_dump(mode="json"), user)
                    except DailyBudgetExceeded:
                        deferred = True
                        break
                    except Exception as error:
                        logger.warning("Matching failed (%s)", type(error).__name__)
                        deferred = True
                        break
                    decision = result.model_dump()
                    await self.db.execute(
                        "INSERT INTO decisions(user_id,item_id,fingerprint,relevant,reason) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user_id,item_id) DO UPDATE SET fingerprint=excluded.fingerprint,relevant=excluded.relevant,reason=excluded.reason",
                        (user_id, record["id"], match_hash, int(result.relevant), result.reason),
                    )
                if not decision["relevant"]:
                    continue
                text = await self.card(record, user, decision["reason"])
                chunks = message_chunks(text)
                for index, chunk in enumerate(chunks):
                    await self.bot.send_message(user_id, chunk, reply_markup=self.item_buttons(record["id"]) if index == len(chunks) - 1 else None, link_preview_options=LinkPreviewOptions(is_disabled=True))
                await self.db.execute(
                    "INSERT INTO deliveries(user_id,item_id,fingerprint,sent_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,item_id) DO UPDATE SET fingerprint=excluded.fingerprint,sent_at=excluded.sent_at",
                    (user_id, record["id"], payload_hash, now.isoformat()),
                )
                sent += 1
                await asyncio.sleep(0.05)
                if sent >= self.settings.max_digest_items:
                    break
            pending = await self.db.one(
                "SELECT COUNT(*) AS n FROM posts p JOIN subscriptions s ON s.channel_id=p.channel_id "
                "WHERE s.user_id=? AND (p.processed_hash IS NULL OR p.processed_hash!=p.content_hash)", (user_id,),
            )
            suffix = " Часть публикаций еще обрабатывается." if pending and pending["n"] else ""
            suffix += " AI временно недоступен или исчерпан дневной лимит; оставшееся попадет в следующий отчет." if deferred else ""
            suffix += " Достигнут лимит карточек; остальные попадут в следующий отчет." if sent >= self.settings.max_digest_items else ""
            await self.bot.send_message(user_id, (f"Дайджест: {sent} новых или обновленных публикаций." if sent else "Новых подходящих публикаций пока нет.") + suffix)
            if not manual:
                await self.db.execute("UPDATE users SET last_report=? WHERE id=?", (day, user_id))

    async def deliver_outbox(self) -> None:
        for entry in await self.db.rows("SELECT * FROM outbox WHERE sent=0 AND attempts<10 AND retry_after<=? ORDER BY id LIMIT 30", (time.time(),)):
            recipient = await self.db.one("SELECT community FROM users WHERE id=?", (entry["recipient"],))
            if (
                entry["recipient"] not in self.settings.users
                or not recipient
                or (entry["sender"] is not None and entry["sender"] not in self.settings.users)
                or (entry["dedupe"].startswith("channels:") and not recipient["community"])
            ):
                await self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (entry["id"],))
                continue
            try:
                markup = InlineKeyboardMarkup.model_validate(json.loads(entry["keyboard"])) if entry["keyboard"] else None
                await self.bot.send_message(entry["recipient"], entry["text"], reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
                await self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (entry["id"],))
                await asyncio.sleep(0.05)
            except TelegramForbiddenError:
                await self.db.execute("UPDATE users SET enabled=0 WHERE id=?", (entry["recipient"],))
                await self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (entry["id"],))
            except TelegramRetryAfter as error:
                await self.db.execute("UPDATE outbox SET retry_after=? WHERE id=?", (time.time() + error.retry_after + 1, entry["id"]))
                break
            except TelegramBadRequest:
                logger.error("Outbox message %s rejected", entry["id"])
                await self.db.execute("UPDATE outbox SET attempts=10 WHERE id=?", (entry["id"],))
            except Exception as error:
                logger.warning("Outbox delivery failed (%s)", type(error).__name__)
                await self.db.execute("UPDATE outbox SET attempts=attempts+1,retry_after=? WHERE id=?", (time.time() + min(3600, 30 * 2 ** entry["attempts"]), entry["id"]))
