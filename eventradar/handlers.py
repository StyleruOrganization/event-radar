import asyncio
import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, ChatMemberUpdated, ErrorEvent, Message
from telethon.errors import FloodWaitError

from eventradar.config import DEFAULT_PROMPT
from eventradar.db import message_chunks
from eventradar.domain import Item, channel_username, clock
from eventradar.reader import Reader
from eventradar.service import Service, keyboard

HELP = """EventRadar — ваши каналы и события.

/add @channel — подписаться внутри бота
/remove @channel — отписаться
/channels — ваши каналы и состояние чтения
/prompt — показать интересы; /prompt текст — изменить; /prompt reset — сбросить
/digest — прочитать и проанализировать публикации с последнего отчета (не чаще раза в 5 минут)
/schedule 09:00 Europe/Moscow — время отчета и часовой пояс
/city Москва — город; /region Россия, Москва — регион
/address адрес — точка отправления; /address clear — удалить
/routing on или off — приблизительная оценка дороги AI с передачей адреса OpenAI
/block 3 00:00 00:00 9 5 — занято каждую среду с сентября по май
Формат: день 1–7, начало, конец, первый месяц, последний месяц.
Одинаковые часы означают полные сутки; ночные интервалы поддерживаются.
/blocks — интервалы занятости; /unblock ID — удалить
/me — ваш Telegram ID
/ai — состояние подписки; /ai retry — снять паузу после повторного входа
/review ID текст — личный отзыв после отметки посещения
/history — посещенные события и личные отзывы
/preview ID — показать сохраненную карточку в новом оформлении без AI
/destination — выбрать приватную группу или канал для публикации
/destination ID — подключить чат по его числовому ID
/destination off — отключить общую публикацию
/publish — отправить готовый отчет без нового AI-анализа
/pause — отключить отчеты; /resume — включить
/settings — настройки; /privacy — данные; /delete_me — удалить свои данные

Управление доступно только владельцу в личном чате. Привязанная группа или канал получает готовые отчеты без доступа участников к настройкам и AI.
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
            text = f"Это личный бот владельца. Ваш Telegram ID: {user.id}."
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
                await message.answer(f"Ваш Telegram ID: {uid}.")
            elif name == "/ai":
                if argument == "retry":
                    await db.execute("DELETE FROM metadata WHERE key='ai_pause'")
                    service.ai.selected_model = None
                await message.answer(await service.ai.status() + "\nУправление: https://chatgpt.com/settings/usage")
            elif name == "/destination":
                if argument == "off":
                    await db.execute("DELETE FROM metadata WHERE key='report_destination'")
                    await message.answer("Общая публикация отключена. Личные отчеты продолжаются.")
                elif argument:
                    try:
                        target = await service.bind_destination(int(argument))
                    except (ValueError, TelegramBadRequest, TelegramForbiddenError):
                        await message.answer("Не удалось подключить чат. Он должен быть приватной группой или каналом, где вы администратор, а бот имеет право публиковать. Для канала дайте боту право публикации сообщений.")
                        return
                    await message.answer(f"✅ Место публикации: {target['title']}. Готовые отчеты будут приходить туда без нового AI-анализа. /publish — отправить предыдущий готовый отчет.")
                else:
                    current = await service.destination()
                    candidates = await db.rows("SELECT chat_id,title,kind FROM publication_candidates WHERE owner_id=? ORDER BY title LIMIT 30", (uid,))
                    rows = [[(row["title"][:55], f"destination:{row['chat_id']}")] for row in candidates]
                    text = (f"Текущее место публикации: {current['title']}\n\n" if current else "Общая публикация пока не подключена.\n\n") + "Создайте приватную группу или канал и добавьте туда бота. Для канала назначьте бота администратором с правом публикации. Затем выберите чат кнопкой здесь. Настройки доступны только в этой личной переписке. Домашний адрес, личная оценка дороги и отзывы в общий отчет не включаются."
                    if not candidates:
                        text += "\n\nНовых чатов пока нет. Если бот уже добавлен, измените его статус администратора или добавьте заново, затем повторите /destination. Для известного ID: /destination -100…"
                    await message.answer(text,reply_markup=keyboard(rows) if rows else None)
            elif name == "/publish":
                await message.answer(await service.publish_latest())
            elif name == "/preview":
                record = await service.accessible_item(uid,int(argument))
                if not record:
                    await message.answer("Карточка недоступна или обновлена.")
                    return
                await service.send_card(uid,record,{**user,"routing":0},set(),service.settings.max_photos_per_report)
            elif name in {"/add", "/remove"}:
                username = channel_username(argument)
                if name == "/remove":
                    await db.execute("DELETE FROM subscriptions WHERE user_id=? AND channel_id IN (SELECT id FROM channels WHERE username=?)", (uid, username))
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
                await message.answer(f"Добавлен @{username}. Сообщения будут прочитаны и проанализированы при подготовке ближайшего отчета или по /digest. Для самого первого отчета берется история за {service.settings.initial_lookback_days} дней.")
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
                    await message.answer("Интересы сохранены. Новый промпт применяется при подготовке следующего отчета к публикациям с последнего успешно отправленного отчета.")
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
            elif name == "/routing":
                if argument not in {"on", "off"}:
                    raise ValueError("Укажите on или off.")
                await db.execute("UPDATE users SET routing=? WHERE id=?", (int(argument == "on"), uid))
                await message.answer("Оценка дороги включена. Адрес и место события передаются OpenAI для приблизительной оценки без карт и расписания." if argument == "on" else "Оценка дороги и передача адреса выключены.")
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
            elif name == "/review":
                item_id, text = argument.split(" ", 1)
                if not 1 <= len(text.strip()) <= 1500:
                    raise ValueError("Отзыв должен содержать от 1 до 1500 символов.")
                attendance = await db.one("SELECT * FROM attendance WHERE user_id=? AND item_id=?", (uid, int(item_id)))
                if not attendance:
                    raise ValueError("Сначала отметьте посещение в карточке события.")
                await db.execute("UPDATE attendance SET review=? WHERE user_id=? AND item_id=?", (text.strip(), uid, int(item_id)))
                await message.answer("Личный отзыв сохранен. Его можно найти в /history.")
            elif name == "/history":
                entries = await db.rows("SELECT a.*,i.payload FROM attendance a JOIN items i ON i.id=a.item_id WHERE a.user_id=? ORDER BY i.id DESC LIMIT 20", (uid,))
                if not entries:
                    await message.answer("Посещенных событий пока нет.")
                for entry in entries:
                    await message.answer(f"#{entry['item_id']} · {Item.model_validate_json(entry['payload']).title}\n\n{entry['review'] or 'Отзыв еще не оставлен.'}")
            elif name in {"/pause", "/resume"}:
                await db.execute("UPDATE users SET enabled=? WHERE id=?", (int(name == "/resume"), uid))
                await message.answer("Отчеты включены." if name == "/resume" else "Отчеты выключены. Включить снова: /resume.")
            elif name == "/settings":
                await message.answer(f"Отчет: {user['report_time']}, {user['timezone']}\nГород: {user['city'] or 'не задан'}\nРегион: {user['region'] or 'не задан'}\nАдрес: {user['address'] or 'не задан'}\nОценка дороги AI с передачей адреса OpenAI: {'да' if user['routing'] else 'нет'}\nОтчеты: {'включены' if user['enabled'] else 'выключены'}")
            elif name == "/privacy":
                await message.answer("На сервере хранятся ваш Telegram ID, имя, интересы, подписки, настройки, адрес и личные отзывы. Тексты публичных каналов общие. OpenAI получает публикации, ваш промпт, город и регион. Домашний адрес передается OpenAI только для приблизительной оценки дороги после /routing on; Telegram ID в AI-запросы не включается. Карты и расписания транспорта не запрашиваются. Отзывы доступны только вам. /delete_me удаляет ваши данные, включая кеш оценок дороги, из рабочей базы; уже отправленные сообщения и серверные резервные копии автоматически не удаляются.")
            elif name == "/delete_me":
                await message.answer("Удалить ваши настройки, подписки, посещения и отзывы из рабочей базы? Подключение ChatGPT отключается отдельно командой chatgpt_auth logout.", reply_markup=keyboard([[("Удалить мои данные", "delete:confirm")]]))
            else:
                await message.answer("Неизвестная команда. /help — все возможности.")
        except (ValueError, ZoneInfoNotFoundError):
            await message.answer("Проверьте аргументы команды и формат в /help.")

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        uid = query.from_user.id
        await query.answer()
        action, separator, value = (query.data or "").partition(":")
        if not separator:
            await query.message.answer("Кнопка устарела. Используйте /help.")
            return
        if action == "destination" and value.lstrip("-").isdecimal():
            try:
                target = await service.bind_destination(int(value))
            except (ValueError, TelegramBadRequest, TelegramForbiddenError):
                await query.message.answer("Не удалось подключить чат. Проверьте, что он приватный, вы его администратор, а бот может публиковать сообщения.")
                return
            await query.message.answer(f"✅ Место публикации: {target['title']}. /publish — отправить готовый отчет без повторного AI-анализа. Новые отчеты будут публиковаться автоматически после подготовки.")
            return
        if action == "delete" and value == "confirm":
            await db.execute("DELETE FROM metadata WHERE key='report_destination'")
            await db.execute("DELETE FROM users WHERE id=?", (uid,))
            await query.message.answer("Личные данные удалены. При новом обращении будет создан пустой профиль. Подключение ChatGPT управляется отдельно.")
            return
        if action not in {"attend", "reviews"} or not value.isdecimal():
            await query.message.answer("Эта функция недоступна в личной версии бота.")
            return
        item_id = int(value)
        record = await service.accessible_item(uid, item_id)
        if not record:
            await query.message.answer("Карточка недоступна или обновлена. Сохраненные отзывы доступны в /history.")
            return
        item = Item.model_validate_json(record["payload"])
        if action == "attend":
            if item.kind != "event" or not item.starts_at or item.starts_at > datetime.now(UTC):
                await query.message.answer("Посещение можно отметить после начала события.")
                return
            await db.execute("INSERT OR IGNORE INTO attendance(user_id,item_id) VALUES(?,?)", (uid, item_id))
            await query.message.answer(f"Посещение отмечено. Оставьте отзыв: /review {item_id} ваш текст")
        else:
            review = await db.one("SELECT review FROM attendance WHERE user_id=? AND item_id=?", (uid, item_id))
            await query.message.answer(review["review"] if review and review["review"] else f"Отзыва пока нет. После посещения: /review {item_id} ваш текст")

    @router.my_chat_member()
    async def discover_destination(event: ChatMemberUpdated) -> None:
        if event.from_user.id != service.settings.owner_telegram_id or event.chat.type not in {"group", "supergroup", "channel"}:
            return
        if event.new_chat_member.status not in {"member", "administrator"}:
            await db.execute("DELETE FROM publication_candidates WHERE owner_id=? AND chat_id=?", (service.settings.owner_telegram_id,event.chat.id))
            return
        if event.chat.username:
            return
        await db.execute(
            "INSERT INTO users(id,name,prompt,timezone) VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (service.settings.owner_telegram_id,event.from_user.full_name[:100],DEFAULT_PROMPT,service.settings.default_timezone),
        )
        await db.execute(
            "INSERT INTO publication_candidates(owner_id,chat_id,title,kind) VALUES(?,?,?,?) "
            "ON CONFLICT(owner_id,chat_id) DO UPDATE SET title=excluded.title,kind=excluded.kind",
            (service.settings.owner_telegram_id,event.chat.id,event.chat.title or str(event.chat.id),event.chat.type),
        )

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
