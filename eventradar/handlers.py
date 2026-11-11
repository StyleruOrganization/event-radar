import asyncio
import logging
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, ChatMemberUpdated, ErrorEvent, Message
from telethon.errors import FloodWaitError

from eventradar.config import DEFAULT_PROMPT
from eventradar.db import message_chunks
from eventradar.domain import channel_username, clock
from eventradar.reader import Reader
from eventradar.service import Service, keyboard

DIGEST_COMMANDS = {"/digest", "/didgest"}
PUBLIC_COMMANDS = DIGEST_COMMANDS | {"/start", "/help", "/add", "/remove", "/channels", "/prompt"}

PUBLIC_HELP = """EventRadar — подборка событий из ваших Telegram-каналов.

Вы сами решаете, что попадёт в дайджест. Напишите боту обычными словами, какие события вам интересны. Этот текст называется промптом. У каждого пользователя свой промпт.

1. Добавьте публичный канал. Отправьте /add и через пробел напишите @имя_канала.
2. Напишите /prompt и через пробел опишите, что хотите найти. Например, отправьте такое сообщение.

/prompt Показывай бесплатные выставки и экскурсии в Москве на выходных

3. Отправьте /didgest. Бот будет искать такие события в каналах, которые вы добавили.

Промпт можно менять в любой момент. Например, отправьте /prompt Ищи детские мастер-классы в Москве, и следующий /didgest будет искать мастер-классы. Если свой текст ещё не задан, бот подбирает разные очные мероприятия.

/prompt — посмотреть текущий текст
/prompt reset — вернуть стандартный текст
/channels — посмотреть добавленные каналы
/remove @канал — убрать канал из подборки
/didgest reset — заново получить дайджест за последние 7 дней, включая карточки, которые вы уже получали

Дайджест можно запросить раз в 5 минут. Подписка внутри бота не меняет ваши личные подписки Telegram.
"""

HELP = PUBLIC_HELP + """
/schedule 09:00 Europe/Moscow — время отчета и часовой пояс
/city Москва — город; /region Россия, Москва — регион
/me — ваш Telegram ID
/ai — состояние подписки; /ai retry — снять паузу после повторного входа
/preview ID — показать сохраненную карточку в новом оформлении без AI
/destination — выбрать приватную группу или канал для публикации
/destination ID — подключить чат по его числовому ID
/destination off — отключить общую публикацию
/publish — отправить готовый отчет без нового AI-анализа
/pause — отключить отчеты; /resume — включить
/settings — настройки; /privacy — данные; /delete_me — удалить свои данные

Ручной дайджест, подписки и интересы доступны всем в личном чате. Остальные настройки доступны владельцу. Привязанная группа или канал получает готовые отчеты.
"""


class Access(BaseMiddleware):
    def __init__(self, service: Service):
        self.service = service
        self.last_action: dict[int, float] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    async def __call__(self, handler, event, data):
        user = event.from_user
        message = event if isinstance(event, Message) else event.message
        if not user or not message or message.chat.type != "private":
            return
        if user.id not in self.service.settings.users:
            if isinstance(event, CallbackQuery):
                await event.answer("Настройки доступны только владельцу.", show_alert=True)
                return
            command, mention, username = (event.text or "").partition(" ")[0].lower().partition("@")
            if command not in PUBLIC_COMMANDS or (
                mention and (not username or username != (self.service.bot_username or "").lower())
            ):
                await event.answer(f"Список доступных команд — /help. Ваш Telegram ID: {user.id}.")
                return
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        if lock.locked():
            if isinstance(event, CallbackQuery):
                await event.answer("Предыдущая операция еще выполняется.")
            else:
                await event.answer("Предыдущая операция еще выполняется. Дождитесь ее завершения.")
            return
        async with lock:
            return await self.dispatch(handler, event, data)

    async def dispatch(self, handler, event, data):
        user = event.from_user
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
        is_owner = uid in service.settings.users
        user = await db.one("SELECT * FROM users WHERE id=?", (uid,))
        try:
            if name in {"/start", "/help"}:
                await message.answer(HELP if is_owner else PUBLIC_HELP)
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
                    await connection.execute(
                        "INSERT INTO channels(id,username,title) VALUES(?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET username=excluded.username,title=excluded.title WHERE ?",
                        (channel_id, username, title, is_owner),
                    )
                    await connection.execute("INSERT OR IGNORE INTO subscriptions(user_id,channel_id) VALUES(?,?)", (uid, channel_id))
                    async with connection.execute("SELECT username FROM channels WHERE id=?", (channel_id,)) as cursor:
                        channel = await cursor.fetchone()
                report_command = "при подготовке ближайшего отчета или по /digest" if is_owner else "по /didgest"
                await message.answer(
                    f"Добавлен @{channel['username']}. Сообщения будут прочитаны и проанализированы {report_command}. "
                    f"Для самого первого отчета берется история за {service.settings.initial_lookback_days} дней."
                )
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
            elif name in {"/city", "/region"}:
                field = {"/city": "city", "/region": "region"}[name]
                if not argument or len(argument) > 300:
                    raise ValueError("Укажите значение до 300 символов или clear для удаления.")
                await db.execute(f"UPDATE users SET {field}=? WHERE id=?", ("" if argument == "clear" else argument, uid))
                await message.answer("Сохранено.")
            elif name in DIGEST_COMMANDS:
                if not is_owner and not await db.one(
                    "SELECT 1 FROM subscriptions WHERE user_id=? LIMIT 1", (uid,),
                ):
                    await message.answer("У вас пока нет каналов для дайджеста. Добавьте канал командой /add @channel.")
                    return
                if time.time() - user["last_manual"] < 300:
                    await message.answer("Повторный дайджест доступен через 5 минут после предыдущего запроса.")
                    return
                await db.execute("UPDATE users SET last_manual=? WHERE id=?", (time.time(), uid))
                reset_period = argument == "reset"
                if reset_period:
                    await message.answer("Подбираю публикации за последние 7 дней. Обработка может занять несколько минут.")
                else:
                    await message.answer("Подбираю публикации. Обработка может занять несколько минут.")
                await service.digest(uid, manual=True, reset_period=reset_period)
            elif name in {"/pause", "/resume"}:
                await db.execute("UPDATE users SET enabled=? WHERE id=?", (int(name == "/resume"), uid))
                await message.answer("Отчеты включены." if name == "/resume" else "Отчеты выключены. Включить снова: /resume.")
            elif name == "/settings":
                await message.answer(f"Отчет: {user['report_time']}, {user['timezone']}\nГород: {user['city'] or 'не задан'}\nРегион: {user['region'] or 'не задан'}\nОтчеты: {'включены' if user['enabled'] else 'выключены'}")
            elif name == "/privacy":
                await message.answer("На сервере хранятся ваш Telegram ID, имя, интересы, подписки, настройки, адрес и личные отзывы. Тексты публичных каналов общие. OpenAI получает публикации, ваш промпт, город и регион. Telegram ID в AI-запросы не включается. Адрес и отзывы из старой версии могут оставаться в базе до удаления профиля. /delete_me удаляет ваши данные, включая кеш оценок дороги, из рабочей базы; уже отправленные сообщения и серверные резервные копии автоматически не удаляются.")
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
        await query.message.answer("Кнопка устарела. Используйте /help.")

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
