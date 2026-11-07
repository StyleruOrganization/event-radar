import asyncio
import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery, ErrorEvent, Message
from telethon.errors import FloodWaitError

from eventradar.config import DEFAULT_PROMPT
from eventradar.db import message_chunks
from eventradar.domain import Item, channel_username, clock, fingerprint
from eventradar.reader import Reader
from eventradar.service import Service, keyboard

HELP = """EventRadar — ваши каналы и события.

/add @channel — подписаться внутри бота
/remove @channel — отписаться
/channels — ваши каналы и состояние чтения
/prompt — показать интересы; /prompt текст — изменить; /prompt reset — сбросить
/digest — новые подходящие публикации (не чаще раза в 5 минут)
/schedule 09:00 Europe/Moscow — время отчета и часовой пояс
/city Москва — город; /region Россия, Москва — регион
/address адрес — точка отправления; /address clear — удалить
/routing on или off — согласие передавать адрес Google для расчета дороги
/block 3 00:00 00:00 9 5 — занято каждую среду с сентября по май
Формат: день 1–7, начало, конец, первый месяц, последний месяц.
Одинаковые часы означают полные сутки; ночные интервалы поддерживаются.
/blocks — интервалы занятости; /unblock ID — удалить
/me — ваш ID для подключения друзей
/friend ID — предложить дружбу; /friends — друзья; /unfriend ID — удалить
/sharechannels — выбрать каналы и предложить их другим участникам
/community on или off — получать предложения каналов (по умолчанию выключено)
/review ID текст — личный отзыв после отметки посещения
/reviewshare ID — поделиться отзывом с друзьями
/history — посещенные события и личные отзывы
/pause — отключить отчеты; /resume — включить
/settings — настройки; /privacy — данные; /delete_me — удалить свои данные

Приглашения, отметки посещения и отзывы доступны по кнопкам карточки.
Друзья должны сначала запустить этого бота и быть допущены владельцем.
Подписка внутри бота не меняет ваши личные подписки Telegram.
"""


class Access(BaseMiddleware):
    def __init__(self, service: Service):
        self.service = service
        self.last_action: dict[int, float] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    async def __call__(self, handler, event, data):
        uid = event.from_user.id if event.from_user else 0
        if uid not in self.service.settings.users:
            return await self.dispatch(handler, event, data)
        lock = self.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            if isinstance(event, CallbackQuery):
                await event.answer("Предыдущая операция еще выполняется.")
            elif isinstance(event, Message) and event.chat.type == "private":
                await event.answer("Предыдущая операция еще выполняется. Дождитесь ее завершения.")
            return
        async with lock:
            return await self.dispatch(handler, event, data)

    async def dispatch(self, handler, event, data):
        user = event.from_user
        message = event if isinstance(event, Message) else event.message
        if not user or not message or message.chat.type != "private":
            return
        if user.id not in self.service.settings.users:
            text = f"Бот доступен по приглашению владельца. Ваш Telegram ID: {user.id}."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return
        now = time.monotonic()
        if now - self.last_action.get(user.id, 0) < 0.7:
            if isinstance(event, CallbackQuery):
                await event.answer("Секунду…")
            return
        self.last_action[user.id] = now
        await self.service.db.execute(
            "INSERT INTO users(id,name,prompt,timezone) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (user.id, user.full_name[:100], DEFAULT_PROMPT, self.service.settings.default_timezone),
        )
        return await handler(event, data)


def create_router(service: Service, reader: Reader) -> Router:
    router = Router()
    db = service.db
    access = Access(service)
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)

    async def share_picker(message: Message, user_id: int) -> None:
        channels = await db.rows(
            "SELECT c.*,d.channel_id IS NOT NULL AS selected FROM channels c "
            "JOIN subscriptions s ON s.channel_id=c.id LEFT JOIN share_drafts d ON d.channel_id=c.id AND d.user_id=s.user_id "
            "WHERE s.user_id=? ORDER BY c.title", (user_id,),
        )
        if not channels:
            await message.answer("Сначала добавьте каналы через /add.")
            return
        rows = [[(("✓ " if c["selected"] else "○ ") + c["title"][:45], f"select:{c['id']}")] for c in channels]
        rows.append([("Разослать выбранные", "share:confirm"), ("Сбросить", "share:clear")])
        await message.answer("Выберите только те каналы, которыми хотите поделиться. Получат участники с /community on, еще не подписанные на эти каналы.", reply_markup=keyboard(rows))

    @router.message()
    async def command(message: Message) -> None:
        if not message.text:
            await message.answer("Используйте /help для списка команд.")
            return
        head, _, argument = message.text.partition(" ")
        name, argument = head.split("@", 1)[0].lower(), argument.strip()
        uid = message.from_user.id
        user = await db.one("SELECT * FROM users WHERE id=?", (uid,))
        try:
            if name in {"/start", "/help"}:
                await message.answer(HELP)
            elif name == "/me":
                await message.answer(f"Ваш Telegram ID: {uid}. Друг может отправить /friend {uid}.")
            elif name in {"/add", "/remove"}:
                username = channel_username(argument)
                if name == "/remove":
                    await db.execute("DELETE FROM subscriptions WHERE user_id=? AND channel_id IN (SELECT id FROM channels WHERE username=?)", (uid, username))
                    await db.execute("DELETE FROM share_drafts WHERE user_id=? AND channel_id IN (SELECT id FROM channels WHERE username=?)", (uid, username))
                    await message.answer(f"Подписка на @{username} удалена.")
                    return
                count = await db.one("SELECT COUNT(*) AS n FROM subscriptions WHERE user_id=?", (uid,))
                if count["n"] >= service.settings.max_channels_per_user:
                    await message.answer("Достигнут лимит каналов. Удалите ненужный через /remove.")
                    return
                try:
                    channel_id, title = await reader.resolve(username)
                except FloodWaitError as error:
                    await message.answer(f"Telegram просит подождать {error.seconds} с. Повторите позже.")
                    return
                except Exception:
                    await message.answer("Не удалось открыть канал. Проверьте, что он публичный и ссылка верна.")
                    return
                async with db.transaction() as connection:
                    await connection.execute("INSERT INTO channels(id,username,title) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,title=excluded.title", (channel_id, username, title))
                    await connection.execute("INSERT OR IGNORE INTO subscriptions(user_id,channel_id) VALUES(?,?)", (uid, channel_id))
                await message.answer(f"Добавлен @{username}. Первое чтение — в ближайшие {service.settings.poll_interval_seconds // 60} мин.; история за {service.settings.initial_lookback_days} дней.")
            elif name == "/channels":
                rows = await db.rows("SELECT c.* FROM channels c JOIN subscriptions s ON s.channel_id=c.id WHERE s.user_id=? ORDER BY c.title", (uid,))
                text = "\n".join(f"@{r['username']} — {r['title']}" + (f" · {r['error']}" if r["error"] else "") for r in rows) or "Каналов пока нет. Добавьте /add @channel."
                for chunk in message_chunks(text):
                    await message.answer(chunk)
            elif name == "/prompt":
                if not argument:
                    await message.answer(user["prompt"])
                elif len(argument) > 3000:
                    await message.answer("Промпт должен быть не длиннее 3000 символов.")
                else:
                    await db.execute("UPDATE users SET prompt=? WHERE id=?", (DEFAULT_PROMPT if argument == "reset" else argument, uid))
                    await message.answer("Интересы сохранены. В следующих отчетах публикации будут оцениваться заново; уже доставленные неизмененные карточки не повторяются.")
            elif name == "/schedule":
                report_time, timezone = argument.split()
                clock(report_time)
                ZoneInfo(timezone)
                await db.execute("UPDATE users SET report_time=?,timezone=? WHERE id=?", (report_time, timezone, uid))
                await message.answer(f"Ежедневный отчет в {report_time}, {timezone}. После простоя отчет догоняется в тот же день.")
            elif name in {"/city", "/region", "/address"}:
                field = {"/city": "city", "/region": "region", "/address": "address"}[name]
                if not argument or len(argument) > 300:
                    raise ValueError("Укажите значение до 300 символов или clear для удаления.")
                await db.execute(f"UPDATE users SET {field}=? WHERE id=?", ("" if argument == "clear" else argument, uid))
                await message.answer("Сохранено. Адрес используется только для вашей дороги; друзьям он не отправляется.")
            elif name in {"/routing", "/community"}:
                if argument not in {"on", "off"}:
                    raise ValueError("Укажите on или off.")
                field = "routing" if name == "/routing" else "community"
                if field == "routing" and argument == "on" and not service.routes.key:
                    await message.answer("Владелец еще не настроил GOOGLE_ROUTES_API_KEY. Ссылки на маршрут доступны без него.")
                    return
                await db.execute(f"UPDATE users SET {field}=? WHERE id=?", (int(argument == "on"), uid))
                await message.answer(("Расчет включен: ваш адрес и место события будут передаваться Google Routes для оценки дороги." if argument == "on" else "Расчет с передачей адреса выключен.") if field == "routing" else "Настройка получения предложений каналов сохранена.")
            elif name == "/block":
                weekday, start, end, first, last = argument.split()
                weekday, first, last = int(weekday), int(first), int(last)
                if not 1 <= weekday <= 7 or not 1 <= first <= 12 or not 1 <= last <= 12:
                    raise ValueError("День недели 1–7, месяцы 1–12.")
                clock(start)
                clock(end)
                count = await db.one("SELECT COUNT(*) AS n FROM blackouts WHERE user_id=?", (uid,))
                if count["n"] >= 30:
                    raise ValueError("Лимит 30 интервалов; удалите ненужные.")
                key = await db.execute("INSERT INTO blackouts(user_id,weekday,start_time,end_time,from_month,to_month) VALUES(?,?,?,?,?,?)", (uid, weekday, start, end, first, last))
                await message.answer(f"Занятость #{key} сохранена. Время считается в вашем часовом поясе, месяцы повторяются ежегодно.")
            elif name == "/blocks":
                rules = await db.rows("SELECT * FROM blackouts WHERE user_id=? ORDER BY id", (uid,))
                await message.answer("\n".join(f"#{r['id']}: день {r['weekday']}, {r['start_time']}–{r['end_time']}, месяцы {r['from_month']}–{r['to_month']}" for r in rules) or "Ограничений нет.")
            elif name == "/unblock":
                await db.execute("DELETE FROM blackouts WHERE id=? AND user_id=?", (int(argument), uid))
                await message.answer("Интервал удален, если он существовал.")
            elif name == "/digest":
                if time.time() - user["last_manual"] < 300:
                    await message.answer("Повторный дайджест доступен через 5 минут после предыдущего запроса.")
                    return
                await db.execute("UPDATE users SET last_manual=? WHERE id=?", (time.time(), uid))
                await message.answer("Подбираю публикации. Обработка может занять несколько минут.")
                await service.digest(uid, manual=True)
            elif name == "/friend":
                friend_id = int(argument)
                friend = await db.one("SELECT * FROM users WHERE id=?", (friend_id,))
                if friend_id == uid or friend_id not in service.settings.users or not friend:
                    raise ValueError("Попросите друга запустить бота и прислать свой ID из /me.")
                await db.execute("INSERT OR IGNORE INTO friend_requests(sender,recipient) VALUES(?,?)", (uid, friend_id))
                await db.enqueue(friend_id, f"{user['name']} предлагает добавить друг друга для приглашений на события.", f"friend:{uid}:{friend_id}:{datetime.now(UTC).date()}", keyboard([
                    [("Принять", f"friendyes:{uid}"), ("Отклонить", f"friendno:{uid}")],
                ]).model_dump(), sender=uid)
                await message.answer("Предложение дружбы поставлено в очередь доставки.")
            elif name in {"/friends", "/unfriend"}:
                if name == "/unfriend":
                    other = int(argument)
                    await db.execute("DELETE FROM friendships WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)", (uid, other, other, uid))
                    await db.execute("DELETE FROM friend_requests WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)", (uid, other, other, uid))
                    await message.answer("Дружба удалена в обе стороны.")
                else:
                    friends = await db.rows("SELECT u.id,u.name FROM users u JOIN friendships f ON f.friend_id=u.id WHERE f.user_id=?", (uid,))
                    await message.answer("\n".join(f"{f['name']} · {f['id']}" for f in friends) or "Пока нет друзей. /friend ID — предложить дружбу.")
            elif name == "/sharechannels":
                await share_picker(message, uid)
            elif name == "/review":
                item_id, text = argument.split(" ", 1)
                if not 1 <= len(text.strip()) <= 1500:
                    raise ValueError("Отзыв должен содержать от 1 до 1500 символов.")
                attendance = await db.one("SELECT * FROM attendance WHERE user_id=? AND item_id=?", (uid, int(item_id)))
                if not attendance:
                    raise ValueError("Сначала отметьте посещение в карточке события.")
                await db.execute("UPDATE attendance SET review=?,shared=0 WHERE user_id=? AND item_id=?", (text.strip(), uid, int(item_id)))
                await message.answer("Личный отзыв сохранен. /reviewshare ID — поделиться с друзьями.")
            elif name == "/reviewshare":
                item_id = int(argument)
                review = await db.one("SELECT a.*,i.payload,p.message_id,c.username FROM attendance a JOIN items i ON i.id=a.item_id JOIN posts p ON p.id=i.post_id JOIN channels c ON c.id=p.channel_id WHERE a.user_id=? AND a.item_id=?", (uid, item_id))
                if not review or not review["review"]:
                    raise ValueError("Сначала сохраните отзыв: /review ID текст.")
                await db.execute("UPDATE attendance SET shared=1 WHERE user_id=? AND item_id=?", (uid, item_id))
                item = Item.model_validate_json(review["payload"])
                for friend in await db.rows("SELECT friend_id FROM friendships WHERE user_id=?", (uid,)):
                    await db.enqueue(friend["friend_id"], f"{user['name']} поделился отзывом о «{item.title}»:\n\n{review['review']}\n\nhttps://t.me/{review['username']}/{review['message_id']}", f"review:{uid}:{friend['friend_id']}:{item_id}:{fingerprint(review['review'])}", sender=uid)
                await message.answer("Отзыв открыт друзьям и поставлен в очередь отправки.")
            elif name == "/history":
                entries = await db.rows("SELECT a.*,i.payload FROM attendance a JOIN items i ON i.id=a.item_id WHERE a.user_id=? ORDER BY i.id DESC LIMIT 20", (uid,))
                if not entries:
                    await message.answer("Посещенных событий пока нет.")
                for entry in entries:
                    await message.answer(f"#{entry['item_id']} · {Item.model_validate_json(entry['payload']).title}\n\n{entry['review'] or 'Отзыв еще не оставлен.'}")
            elif name in {"/pause", "/resume"}:
                await db.execute("UPDATE users SET enabled=? WHERE id=?", (int(name == "/resume"), uid))
                await message.answer("Отчеты включены." if name == "/resume" else "Отчеты выключены. Приглашения от друзей остаются доступны.")
            elif name == "/settings":
                await message.answer(f"Отчет: {user['report_time']}, {user['timezone']}\nГород: {user['city'] or 'не задан'}\nРегион: {user['region'] or 'не задан'}\nАдрес: {user['address'] or 'не задан'}\nПередача адреса Google: {'да' if user['routing'] else 'нет'}\nПредложения каналов: {'да' if user['community'] else 'нет'}\nОтчеты: {'включены' if user['enabled'] else 'выключены'}")
            elif name == "/privacy":
                await message.answer("На сервере хранятся ваш Telegram ID, имя, интересы, подписки, настройки, адрес, дружба, приглашения и отзывы. Тексты публичных каналов общие. OpenAI получает публикации, ваш промпт, город и регион; домашний адрес и Telegram ID туда не отправляются. Google получает адрес только после /routing on. Отзывы личные до /reviewshare. /delete_me удаляет ваши данные из рабочей базы; уже отправленные сообщения и серверные резервные копии автоматически не удаляются.")
            elif name == "/delete_me":
                await message.answer("Удалить все ваши настройки, подписки, дружбу, приглашения и отзывы из рабочей базы?", reply_markup=keyboard([[("Удалить мои данные", "delete:confirm")]]))
            else:
                await message.answer("Неизвестная команда. /help — все возможности.")
        except (ValueError, ZoneInfoNotFoundError):
            await message.answer("Проверьте аргументы команды и формат в /help.")

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        uid = query.from_user.id
        user = await db.one("SELECT * FROM users WHERE id=?", (uid,))
        try:
            action, value = (query.data or "").split(":", 1)
            await query.answer()
            if action == "delete" and value == "confirm":
                await db.execute("DELETE FROM users WHERE id=?", (uid,))
                await query.message.answer("Ваши данные удалены. При следующем обращении будет создан пустой профиль.")
                return
            if action in {"friendyes", "friendno"}:
                sender = int(value)
                request = await db.one("SELECT * FROM friend_requests WHERE sender=? AND recipient=?", (sender, uid))
                if not request or sender not in service.settings.users:
                    raise ValueError("Предложение уже обработано или недоступно.")
                async with db.transaction() as connection:
                    if action == "friendyes":
                        await connection.execute("INSERT OR IGNORE INTO friendships(user_id,friend_id) VALUES(?,?),(?,?)", (uid, sender, sender, uid))
                    await connection.execute("DELETE FROM friend_requests WHERE sender=? AND recipient=?", (sender, uid))
                await query.message.answer("Дружба добавлена." if action == "friendyes" else "Предложение отклонено.")
                return
            if action == "select":
                cid = int(value)
                if not await db.one("SELECT 1 FROM subscriptions WHERE user_id=? AND channel_id=?", (uid, cid)):
                    raise ValueError("Вы больше не подписаны на этот канал.")
                selected = await db.one("SELECT 1 FROM share_drafts WHERE user_id=? AND channel_id=?", (uid, cid))
                if selected:
                    await db.execute("DELETE FROM share_drafts WHERE user_id=? AND channel_id=?", (uid, cid))
                else:
                    await db.execute("INSERT INTO share_drafts(user_id,channel_id) VALUES(?,?)", (uid, cid))
                await query.message.edit_reply_markup(reply_markup=None)
                await share_picker(query.message, uid)
                return
            if action == "share":
                if value == "clear":
                    await db.execute("DELETE FROM share_drafts WHERE user_id=?", (uid,))
                    await query.message.answer("Выбор очищен.")
                    return
                if value != "confirm":
                    raise ValueError("Неизвестное действие.")
                channels = await db.rows("SELECT c.* FROM channels c JOIN share_drafts d ON d.channel_id=c.id JOIN subscriptions s ON s.channel_id=c.id AND s.user_id=d.user_id WHERE d.user_id=?", (uid,))
                if not channels:
                    raise ValueError("Выберите хотя бы один канал.")
                recipients = await db.rows("SELECT * FROM users WHERE id!=? AND community=1", (uid,))
                total = 0
                for recipient in recipients:
                    if recipient["id"] not in service.settings.users:
                        continue
                    existing = {r["channel_id"] for r in await db.rows("SELECT channel_id FROM subscriptions WHERE user_id=?", (recipient["id"],))}
                    offered = [c for c in channels if c["id"] not in existing]
                    if not offered:
                        continue
                    for offset in range(0, len(offered), 8):
                        batch = offered[offset:offset + 8]
                        text = f"{user['name']} предлагает каналы:\n\n" + "\n".join(f"{c['title'][:120]} — https://t.me/{c['username']}\nДобавить: /add @{c['username']}" for c in batch)
                        await db.enqueue(recipient["id"], text, f"channels:{uid}:{recipient['id']}:{datetime.now(UTC).date()}:{fingerprint([c['id'] for c in batch])}", sender=uid)
                    total += 1
                await db.execute("DELETE FROM share_drafts WHERE user_id=?", (uid,))
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.answer(f"Предложения поставлены в очередь для {total} участников. Подписки автоматически не меняются.")
                return
            if action in {"yes", "no"}:
                invitation = await db.one("SELECT * FROM invitations WHERE id=? AND recipient=? AND status='pending'", (int(value), uid))
                if not invitation:
                    raise ValueError("Приглашение уже обработано.")
                if action == "yes":
                    current = await service.accessible_item(uid, invitation["item_id"])
                    event = Item.model_validate_json(current["payload"]) if current else None
                    if not event or event.cancelled or not event.starts_at or event.starts_at <= datetime.now(UTC):
                        raise ValueError("Событие обновлено, отменено или уже началось. Уточните детали у друга.")
                status = "accepted" if action == "yes" else "declined"
                await db.execute("UPDATE invitations SET status=? WHERE id=? AND recipient=? AND status='pending'", (status, invitation["id"], uid))
                await db.enqueue(invitation["sender"], f"{user['name']} {'принял(а)' if action == 'yes' else 'отклонил(а)'} приглашение на событие #{invitation['item_id']}.", f"rsvp:{invitation['id']}", sender=uid)
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.answer("Ответ отправлен.")
                return
            # All card actions require access to the specific publication.
            item_id = int(value.split(":", 1)[0])
            record = await service.accessible_item(uid, item_id)
            if not record:
                raise ValueError("Карточка недоступна или обновляется.")
            item = Item.model_validate_json(record["payload"])
            if action == "invite":
                if item.kind != "event" or item.cancelled or not item.starts_at or item.starts_at <= datetime.now(UTC):
                    raise ValueError("Приглашать можно на предстоящие события с известным временем.")
                friends = await db.rows("SELECT u.id,u.name FROM users u JOIN friendships f ON f.friend_id=u.id WHERE f.user_id=?", (uid,))
                rows = [[(f["name"][:50], f"sendinvite:{item_id}:{f['id']}")] for f in friends if f["id"] in service.settings.users]
                await query.message.answer("Кого пригласить? Нажатие отправит приглашение другу." if rows else "Сначала добавьте друга: /friend ID.", reply_markup=keyboard(rows) if rows else None)
            elif action == "sendinvite":
                friend_id = int(value.split(":")[1])
                friendship = await db.one("SELECT 1 FROM friendships WHERE user_id=? AND friend_id=?", (uid, friend_id))
                if not friendship or friend_id not in service.settings.users or item.kind != "event" or not item.starts_at or item.starts_at <= datetime.now(UTC) or item.cancelled:
                    raise ValueError("Приглашение больше недоступно.")
                friend = await db.one("SELECT * FROM users WHERE id=?", (friend_id,))
                await db.execute("INSERT OR IGNORE INTO invitations(sender,recipient,item_id) VALUES(?,?,?)", (uid, friend_id, item_id))
                invitation = await db.one("SELECT * FROM invitations WHERE sender=? AND recipient=? AND item_id=?", (uid, friend_id, item_id))
                text = f"{user['name']} приглашает пойти вместе!\n\n" + await service.card(record, {**friend, "routing": 0})
                await db.enqueue(friend_id, text, f"invite:{invitation['id']}", keyboard([
                    [("Пойду", f"yes:{invitation['id']}"), ("Не смогу", f"no:{invitation['id']}")],
                    [("Пригласить друга", f"invite:{item_id}"), ("Я посетил(а)", f"attend:{item_id}")],
                ]).model_dump(), sender=uid)
                await query.message.answer("Приглашение поставлено в очередь; повторное нажатие не создаст копию.")
            elif action == "attend":
                if item.kind != "event" or not item.starts_at or item.starts_at > datetime.now(UTC):
                    raise ValueError("Посещение можно отметить после начала события.")
                await db.execute("INSERT OR IGNORE INTO attendance(user_id,item_id) VALUES(?,?)", (uid, item_id))
                await query.message.answer(f"Посещение отмечено. Оставьте отзыв: /review {item_id} ваш текст")
            elif action == "reviews":
                reviews = await db.rows("SELECT a.review,u.name FROM attendance a JOIN users u ON u.id=a.user_id WHERE a.item_id=? AND a.review!='' AND (a.user_id=? OR (a.shared=1 AND EXISTS(SELECT 1 FROM friendships f WHERE f.user_id=? AND f.friend_id=a.user_id))) LIMIT 20", (item_id, uid, uid))
                if not reviews:
                    await query.message.answer("Друзья пока не поделились отзывами.")
                for review in reviews:
                    await query.message.answer(f"{review['name']}:\n\n{review['review']}")
            else:
                raise ValueError("Неизвестное действие.")
        except (ValueError, IndexError) as error:
            await query.message.answer(str(error) if str(error) and not isinstance(error, IndexError) else "Кнопка устарела. Используйте /help.")

    @router.errors()
    async def handle_error(event: ErrorEvent) -> bool:
        logging.getLogger(__name__).error("Update failed (%s)", type(event.exception).__name__)
        message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
        if message and not isinstance(event.exception, TelegramForbiddenError):
            try:
                await message.answer("Операция временно недоступна. Попробуйте позже; настройки, сохраненные до ошибки, остаются в силе.")
            except Exception:
                pass
        return True

    return router
