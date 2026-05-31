import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChosenInlineResult,
    ChatMemberUpdated,
    BusinessConnection,
)
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramUnauthorizedError
from dotenv import load_dotenv

import notion_db as db
from analyzer import analyze_message

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Добавь его в .env")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
MOSCOW = ZoneInfo("Europe/Moscow")

# user_id -> (scope, chat_title): контекст последнего чата пользователя
_inline_scope: dict[int, tuple[str, str]] = {}
# chat_instance (из chosen_inline_result) -> (scope, chat_title)
_chat_instance_scope: dict[str, tuple[str, str]] = {}
# scope = chat_instance (нет числового chat_id) — в чат через API не отправить
_opaque_scopes: set[str] = set()
# user_id -> (scope, title, unix_ts) — последняя личная переписка (business / private)
_recent_private_chat: dict[int, tuple[str, str, float]] = {}
# business_connection_id -> владелец аккаунта user_id
_business_owner: dict[str, int] = {}
# username бота для deep-link
_bot_username: str = ""
# user_id -> последний chat_type из inline_query
_inline_user_chat_type: dict[int, str] = {}


# ════════════════════════════════════════════════════════════════
#  ХЕЛПЕРЫ
# ════════════════════════════════════════════════════════════════

def now_moscow() -> datetime:
    """Текущее время в Москве (naive, для сравнений и планировщика)."""
    return datetime.now(MOSCOW).replace(tzinfo=None)


def get_chat_instance(obj) -> str | None:
    """chat_instance есть у ChosenInlineResult (extra), но не у InlineQuery."""
    value = getattr(obj, "chat_instance", None)
    if value is not None:
        return str(value)
    extra = getattr(obj, "model_extra", None) or {}
    raw = extra.get("chat_instance")
    return str(raw) if raw is not None else None


def remember_private_chat(user_id: int, scope: str, title: str) -> None:
    _recent_private_chat[user_id] = (scope, title, time.time())
    _inline_scope[user_id] = (scope, title)


def is_self_scope(scope: str, user_id: int) -> bool:
    return scope == str(user_id)


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        if "T" in iso:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%d.%m.%Y")
        parts = iso[:10].split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
    except ValueError:
        pass
    return iso[:10]


def fmt_datetime(iso: str | None) -> str:
    """Форматирует дату+время для отображения"""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso[:16]


def parse_reminder_time(text: str) -> tuple[str | None, str]:
    """
    Извлекает время из текста задачи.
    Поддерживает форматы:
    - "завтра в 15:30"
    - "сегодня в 15:30"
    - "в 15:30"
    - "через 2 часа"
    - "через 30 минут"
    """
    now = now_moscow()
    clean_text = text
    reminder_time = None

    is_tomorrow = bool(re.search(r'\bзавтра\b', text, re.IGNORECASE))
    is_today = bool(re.search(r'\bсегодня\b', text, re.IGNORECASE))

    # Формат: в 15:30 или в 15-30
    m = re.search(r'(?:в|время|во)\s+(\d{1,2})[:.-](\d{2})', clean_text, re.IGNORECASE)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        base_date = now.date()
        if is_tomorrow:
            base_date += timedelta(days=1)
        elif is_today:
            pass
        reminder_time = datetime(base_date.year, base_date.month, base_date.day, hour, minute, 0)
        if reminder_time <= now and not is_tomorrow and not is_today:
            reminder_time += timedelta(days=1)
        clean_text = re.sub(r'\bзавтра\b', '', clean_text, flags=re.IGNORECASE).strip()
        clean_text = re.sub(r'\bсегодня\b', '', clean_text, flags=re.IGNORECASE).strip()
        clean_text = re.sub(r'(?:в|время|во)\s+\d{1,2}[:.-]\d{2}', '', clean_text, flags=re.IGNORECASE).strip()
        return reminder_time.isoformat(), clean_text

    # Формат: через X часов
    m = re.search(r'через\s+(\d+)\s+час(?:а|ов)?', clean_text, re.IGNORECASE)
    if m:
        hours = int(m.group(1))
        reminder_time = now + timedelta(hours=hours)
        clean_text = re.sub(r'через\s+\d+\s+час(?:а|ов)?', '', clean_text, flags=re.IGNORECASE).strip()
        return reminder_time.isoformat(), clean_text

    # Формат: через X минут
    m = re.search(r'через\s+(\d+)\s+минут(?:у|ы)?', clean_text, re.IGNORECASE)
    if m:
        minutes = int(m.group(1))
        reminder_time = now + timedelta(minutes=minutes)
        clean_text = re.sub(r'через\s+\d+\s+минут(?:у|ы)?', '', clean_text, flags=re.IGNORECASE).strip()
        return reminder_time.isoformat(), clean_text

    # «завтра» / «сегодня» без времени — не трогаем текст (дедлайн извлечёт analyzer)
    return None, clean_text


def scope_from_message(message: Message) -> str:
    """Для сообщений используем chat_id"""
    return str(message.chat.id)


def is_telegram_chat(scope: str) -> bool:
    """Можно ли отправить сообщение в этот чат через bot.send_message."""
    try:
        int(scope)
        return True
    except ValueError:
        return False


def chat_title(message: Message) -> str:
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return message.chat.title or "Группа"
    if message.chat.type == ChatType.PRIVATE:
        name = message.chat.first_name or "Личка"
        if message.chat.last_name:
            name += f" {message.chat.last_name}"
        if message.chat.username:
            name += f" (@{message.chat.username})"
        return f"Чат с {name}"
    return "Чат"


def sender_name(user) -> str:
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    if user.username:
        name += f" (@{user.username})"
    return name.strip()


async def scope_title(scope: str, fallback: str = "Чат") -> str:
    if not is_telegram_chat(scope):
        return fallback
    try:
        chat = await asyncio.wait_for(bot.get_chat(int(scope)), timeout=3.0)
        if chat.title:
            return chat.title
        if chat.first_name:
            name = chat.first_name
            if chat.last_name:
                name += f" {chat.last_name}"
            return f"Чат с {name}"
    except Exception:
        pass
    return fallback


def tasks_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for i, t in enumerate(tasks, 1):
        buttons.append([InlineKeyboardButton(
            text=f"✅ #{i} выполнено",
            callback_data=f"done_task:{t['id']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_tasks_list(tasks: list[dict], title: str = "📋 <b>Открытые задачи:</b>") -> str:
    if not tasks:
        return "📭 Открытых задач нет"
    lines = [f"{title}\n"]
    for i, t in enumerate(tasks, 1):
        added = fmt_date(t.get("added_at"))
        deadline = f" · ⏰ до {fmt_date(t['deadline'])}" if t.get("deadline") else ""
        reminder = f" · 🔔 напомнить в {fmt_datetime(t.get('reminder_at'))}" if t.get("reminder_at") else ""
        lines.append(
            f"<b>#{i}</b> {t['text']}\n"
            f"    👤 {t['from_user']}{deadline}{reminder}\n"
            f"    💬 {t['chat_title']} · 📅 {added}"
        )
    return "\n\n".join(lines)


def format_archive_list(tasks: list[dict]) -> str:
    if not tasks:
        return "📭 Архив пуст"
    lines = ["🗄 <b>Архив задач:</b>\n"]
    for i, t in enumerate(tasks, 1):
        closed = fmt_date(t.get("closed_at") or t.get("added_at"))
        deadline = f" · ⏰ {fmt_date(t['deadline'])}" if t.get("deadline") else ""
        lines.append(
            f"<b>#{i}</b> {t['text']}\n"
            f"    👤 {t['from_user']}{deadline}\n"
            f"    ✅ закрыто {closed}"
        )
    return "\n\n".join(lines)


def format_notifications_list(notifications: list[dict]) -> str:
    if not notifications:
        return "🔔 Новых уведомлений нет"
    lines = ["🔔 <b>Твои уведомления:</b>\n"]
    for i, n in enumerate(notifications, 1):
        created = fmt_date(n.get("created_at"))
        unread = " 🔵" if not n.get("is_read") else ""
        lines.append(f"<b>#{i}</b>{unread} {n['text']}\n    📅 {created}")
    return "\n\n".join(lines)


def _private_peer_user_id(scope: str, creator_id: int) -> int | None:
    """В личке scope = id собеседника; вернуть его, если это не автор задачи."""
    if not is_telegram_chat(scope):
        return None
    peer_id = int(scope)
    if peer_id > 0 and peer_id != creator_id:
        return peer_id
    return None


async def _bind_chat_instance(chat_instance: str, scope: str, title: str) -> None:
    pair = (scope, title)
    _chat_instance_scope[chat_instance] = pair
    await db.save_chat_instance_scope(chat_instance, scope, title)
    if scope == chat_instance:
        _opaque_scopes.add(scope)


async def predicted_scope_for_user(
    user_id: int,
    chat_type: str | None = None,
) -> tuple[str, str] | None:
    """Лучший известный scope для inline в личке с собеседником."""
    entry = _recent_private_chat.get(user_id)
    if entry and time.time() - entry[2] < 900:
        if chat_type in (None, "private", "sender"):
            return entry[0], entry[1]

    if user_id in _inline_scope:
        scope, title = _inline_scope[user_id]
        if not is_self_scope(scope, user_id):
            return scope, title

    last = await db.get_last_scope(user_id)
    if last and not is_self_scope(last, user_id):
        return last, await scope_title(last, "Личный чат")

    return None


async def resolve_scope_from_chat_instance(
    chat_instance: str,
    user_id: int,
    chat_type: str | None = None,
) -> tuple[str, str]:
    if chat_instance in _chat_instance_scope:
        return _chat_instance_scope[chat_instance]

    stored = await db.get_chat_instance_scope(chat_instance)
    if stored:
        _chat_instance_scope[chat_instance] = stored
        return stored

    predicted = await predicted_scope_for_user(user_id, chat_type)
    if predicted:
        scope, title = predicted
        await _bind_chat_instance(chat_instance, scope, title)
        return scope, title

    title = "Личный чат"
    pair = (chat_instance, title)
    _chat_instance_scope[chat_instance] = pair
    _opaque_scopes.add(chat_instance)
    await db.save_chat_instance_scope(chat_instance, chat_instance, title)
    return pair


async def resolve_inline_scope(
    user_id: int,
    chat_type: str | None = None,
) -> tuple[str, str]:
    predicted = await predicted_scope_for_user(user_id, chat_type)
    if predicted:
        return predicted

    if user_id in _inline_scope:
        scope, title = _inline_scope[user_id]
        if not is_self_scope(scope, user_id):
            return scope, title

    return str(user_id), "Личка с ботом"


async def touch_scope(scope: str, title: str, user_id: int) -> None:
    await db.register_chat_user(scope, title, user_id)


async def save_and_notify(
    text: str,
    scope: str,
    chat_title_str: str,
    user_id: int,
    user_name: str,
    deadline: str | None,
    reminder_at: str | None = None,
) -> int:
    task_id = await db.add_task(text, scope, chat_title_str, user_name, user_id, deadline, reminder_at)
    await touch_scope(scope, chat_title_str, user_id)

    deadline_line = f"\n⏰ Дедлайн: <b>{fmt_date(deadline)}</b>" if deadline else ""
    reminder_line = f"\n🔔 Напоминание: <b>{fmt_datetime(reminder_at)}</b>" if reminder_at else ""
    notify_text = (
        f"📝 <b>Новая задача</b> #{task_id}\n"
        f"👤 {user_name}{deadline_line}{reminder_line}\n"
        f"📝 {text}"
    )

    posted_to_chat = False
    if is_telegram_chat(scope) and scope not in _opaque_scopes:
        try:
            await bot.send_message(int(scope), notify_text)
            posted_to_chat = True
        except Exception as e:
            log.warning(f"Cannot post to chat {scope}: {e}")

    member_ids = list(await db.get_scope_user_ids(scope))
    if user_id not in member_ids:
        member_ids.append(user_id)
    peer_id = _private_peer_user_id(scope, user_id)
    if peer_id and peer_id not in member_ids:
        member_ids.append(peer_id)

    short = f"📝 Новая задача #{task_id}: {text[:80]}"
    if deadline:
        short += f" (до {fmt_date(deadline)})"
    if reminder_at:
        short += f" 🔔 в {fmt_datetime(reminder_at)}"

    peer_notified = False
    for uid in member_ids:
        await db.add_notification(uid, scope, task_id, short)
        if uid != user_id or not posted_to_chat:
            try:
                await bot.send_message(uid, f"🔔 {short}\n💬 {chat_title_str}")
                if peer_id and uid == peer_id:
                    peer_notified = True
            except Exception:
                pass

    if peer_id and not peer_notified and _bot_username:
        token = await db.create_notify_token(scope, chat_title_str)
        link = f"https://t.me/{_bot_username}?start=notify_{token}"
        try:
            await bot.send_message(
                user_id,
                f"⚠️ Собеседнику не удалось отправить уведомление (нужно открыть бота).\n"
                f"Перешлите ему ссылку:\n{link}",
            )
        except Exception:
            pass

    # Если есть напоминание, планируем его
    if reminder_at:
        reminder_dt = datetime.fromisoformat(reminder_at)
        if reminder_dt > now_moscow():
            scheduler.add_job(
                send_reminder,
                "date",
                run_date=reminder_dt,
                args=[task_id, text, scope, chat_title_str],
                id=f"reminder_{task_id}",
                replace_existing=True,
            )
            log.info(f"Scheduled reminder for task #{task_id} at {reminder_at}")
        else:
            log.warning(f"Reminder time {reminder_at} is in the past for task #{task_id}")

    return task_id


async def send_reminder(task_id: int, text: str, scope: str, chat_title_str: str) -> None:
    """Отправляет напоминание о задаче в указанное время"""
    reminder_text = f"🔔 <b>Напоминание о задаче!</b> #{task_id}\n📝 {text}"
    
    # Отправляем в чат
    if is_telegram_chat(scope):
        try:
            await bot.send_message(int(scope), reminder_text)
        except Exception as e:
            log.warning(f"Cannot send reminder to chat {scope}: {e}")
    
    # Отправляем всем участникам чата
    for uid in await db.get_scope_user_ids(scope):
        try:
            await bot.send_message(uid, f"🔔 {reminder_text}\n💬 {chat_title_str}")
        except Exception:
            pass


async def ensure_scope_access(user_id: int, scope: str) -> bool:
    user_scopes = await db.get_user_scopes(user_id)
    return scope in user_scopes


# ════════════════════════════════════════════════════════════════
#  INLINE QUERY
# ════════════════════════════════════════════════════════════════

@dp.inline_query()
async def handle_inline(inline_query: InlineQuery) -> None:
    query = inline_query.query.strip()
    user = inline_query.from_user
    
    chat_type = inline_query.chat_type
    log.info(
        "Inline query uid=%s chat_type=%s q=%r",
        user.id, chat_type, query,
    )
    if chat_type:
        _inline_user_chat_type[user.id] = chat_type

    try:
        if not query:
            results = [
                InlineQueryResultArticle(
                    id="hint_task",
                    title="📝 Добавить задачу",
                    description="Напиши: задача <текст> или просто текст. Можно указать время: завтра в 15:30",
                    input_message_content=InputTextMessageContent(
                        message_text="Используй: @taskFaster_bot задача [текст]",
                        parse_mode=None,
                    ),
                ),
            ]
        else:
            # Извлекаем время напоминания
            reminder_at, clean_query = parse_reminder_time(query)
            result = analyze_message(clean_query)
            text = result.task_text or clean_query
            card_id = _inline_result_id(user.id, query, "auto" if result.is_task else "manual")
            
            predicted = await predicted_scope_for_user(user.id, chat_type)
            notify_token = None
            if predicted:
                notify_token = await db.create_notify_token(predicted[0], predicted[1])

            if result.is_task:
                results = [_task_card(card_id, text, result.deadline, reminder_at, notify_token=notify_token)]
            else:
                results = [_task_card(card_id, query, None, reminder_at, manual=True, notify_token=notify_token)]

        await inline_query.answer(results, cache_time=1, is_personal=False)
        log.info("Inline answered uid=%s results=%d", user.id, len(results))
    except Exception:
        log.exception("Inline query error for %r", query)
        try:
            await inline_query.answer(
                [_task_card(_inline_result_id(user.id, query or "err", "err"), query or "задача", None, None, manual=True)],
                cache_time=1,
                is_personal=False,
            )
        except Exception:
            log.exception("Inline query fallback failed")


def _inline_result_id(user_id: int, query: str, kind: str) -> str:
    raw = f"{user_id}:{kind}:{query}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _task_card(
    result_id: str,
    text: str,
    deadline: str | None,
    reminder_at: str | None = None,
    manual: bool = False,
    notify_token: str | None = None,
) -> InlineQueryResultArticle:
    title = "📝 Сохранить как задачу" if manual else "📝 Задача — нажми чтобы отправить"
    deadline_hint = f" (до {fmt_date(deadline)})" if deadline else ""
    reminder_hint = f" 🔔 в {fmt_datetime(reminder_at)}" if reminder_at else ""
    deadline_line = f"\n⏰ Дедлайн: {fmt_date(deadline)}" if deadline else ""
    reminder_line = f"\n🔔 Напоминание: {fmt_datetime(reminder_at)}" if reminder_at else ""
    msg = f"[Бот]\nДобавил задачу в список!\n📝 {text}{deadline_line}{reminder_line}"
    if notify_token and _bot_username:
        msg += f"\n\n🔔 Собеседник: t.me/{_bot_username}?start=notify_{notify_token}"

    reply_markup = None
    if notify_token and _bot_username:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔔 Подписаться на уведомления",
                url=f"https://t.me/{_bot_username}?start=notify_{notify_token}",
            ),
        ]])

    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=f"{text[:100]}{deadline_hint}{reminder_hint}",
        input_message_content=InputTextMessageContent(
            message_text=msg,
            parse_mode=None,
        ),
        reply_markup=reply_markup,
    )


@dp.chosen_inline_result()
async def handle_chosen(chosen: ChosenInlineResult) -> None:
    query = chosen.query.strip()
    if not query:
        return
    user = chosen.from_user
    name = sender_name(user)

    chat_instance = get_chat_instance(chosen)
    chat_type = _inline_user_chat_type.get(user.id)

    if chat_instance:
        scope, chat_title_str = await resolve_scope_from_chat_instance(
            chat_instance, user.id, chat_type,
        )
    else:
        scope, chat_title_str = await resolve_inline_scope(user.id, chat_type)

    remember_private_chat(user.id, scope, chat_title_str)
    if chat_instance:
        await _bind_chat_instance(chat_instance, scope, chat_title_str)
    await touch_scope(scope, chat_title_str, user.id)
    peer_id = _private_peer_user_id(scope, user.id)
    if peer_id:
        await touch_scope(scope, chat_title_str, peer_id)

    # Извлекаем время напоминания и чистим текст
    reminder_at, clean_query = parse_reminder_time(query)
    result = analyze_message(clean_query)
    text = result.task_text or clean_query

    log.info(f"Saving task: user={user.id}, scope={scope}, text={text[:50]}, reminder={reminder_at}")

    try:
        task_id = await save_and_notify(
            text, scope, chat_title_str, user.id, name, result.deadline, reminder_at,
        )
        log.info(f"Task #{task_id} saved via inline (scope={scope}): {text[:50]}")
    except Exception:
        log.exception("Error saving task from inline")


# ════════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ════════════════════════════════════════════════════════════════

def command_args(message: Message) -> str:
    m = re.match(r"^/\w+(?:@\w+)?\s*(.*)", message.text or "", re.DOTALL)
    return (m.group(1) if m else "").strip()


async def create_task_from_text(message: Message, text: str) -> None:
    if not text:
        await message.answer(
            "📝 <b>Как добавить задачу:</b>\n\n"
            "• Inline: <code>@taskFaster_bot ночевка завтра в 19:00</code> — нажми на карточку <b>сверху</b>\n"
            "• Команда: <code>/task ночевка завтра в 19:00</code>\n\n"
            "<b>Форматы времени:</b>\n"
            "• <code>завтра в 15:30</code> — завтра в 15:30\n"
            "• <code>сегодня в 15:30</code> — сегодня в 15:30\n"
            "• <code>в 15:30</code> — сегодня в 15:30 (или завтра если уже прошло)\n"
            "• <code>через 2 часа</code> — через N часов\n"
            "• <code>через 30 минут</code> — через N минут"
        )
        return

    user = message.from_user
    scope = scope_from_message(message)
    title = chat_title(message)
    await touch_scope(scope, title, user.id)

    # Извлекаем время напоминания
    reminder_at, clean_text = parse_reminder_time(text)
    result = analyze_message(clean_text)
    task_text = result.task_text or clean_text
    
    try:
        task_id = await save_and_notify(
            task_text, scope, title, user.id, sender_name(user), result.deadline, reminder_at,
        )
        deadline = f" · ⏰ {fmt_date(result.deadline)}" if result.deadline else ""
        reminder = f" · 🔔 напомнить в {fmt_datetime(reminder_at)}" if reminder_at else ""
        await message.answer(f"✅ Задача #{task_id} сохранена{deadline}{reminder}\n📝 {task_text}")
    except Exception:
        log.exception("Error saving task from message")
        await message.answer("❌ Не удалось сохранить задачу")


@dp.message(Command("task"))
async def cmd_task(message: Message) -> None:
    await create_task_from_text(message, command_args(message))


@dp.business_connection()
async def on_business_connection(connection: BusinessConnection) -> None:
    if connection.is_enabled:
        _business_owner[connection.id] = connection.user.id
        log.info("Business connected: %s user=%s", connection.id, connection.user.id)
    else:
        _business_owner.pop(connection.id, None)


@dp.business_message()
async def on_business_message(message: Message) -> None:
    if not message.chat or message.from_user and message.from_user.is_bot:
        return
    scope = str(message.chat.id)
    title = chat_title(message)
    conn_id = message.business_connection_id or ""
    owner_id = _business_owner.get(conn_id) or (OWNER_ID if OWNER_ID else None)
    customer_id = message.chat.id

    await touch_scope(scope, title, customer_id)
    if owner_id:
        remember_private_chat(owner_id, scope, title)
        await touch_scope(scope, title, owner_id)
    elif message.from_user and not is_self_scope(scope, message.from_user.id):
        remember_private_chat(message.from_user.id, scope, title)


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    args = (command.args or "").strip()

    if args.startswith("notify_"):
        token = args[7:]
        resolved = await db.resolve_notify_token(token)
        if resolved:
            scope, title = resolved
            await touch_scope(scope, title, user_id)
            remember_private_chat(user_id, scope, title)
            await message.answer(
                f"✅ Уведомления для чата <b>{title}</b> включены.\n"
                "Вы будете получать напоминания о задачах из этой переписки."
            )
            return
        await message.answer("❌ Ссылка устарела. Попросите отправить задачу ещё раз.")
        return

    scope = scope_from_message(message)
    await touch_scope(scope, chat_title(message), user_id)
    await message.answer(
        "👋 <b>ProcrastinationManager активен!</b>\n\n"
        "<b>Добавить задачу:</b>\n"
        "  1️⃣ Inline: <code>@taskFaster_bot ночевка завтра в 19:00</code>\n"
        "     → нажми на карточку <b>над полем ввода</b> (не кнопку отправки)\n"
        "  2️⃣ Команда: <code>/task ночевка завтра в 19:00</code>\n\n"
        "<b>Форматы времени:</b>\n"
        "  • <code>завтра в 19:00</code> — завтра в 19:00\n"
        "  • <code>сегодня в 15:30</code> — сегодня в 15:30\n"
        "  • <code>в 15:30</code> — сегодня/завтра в указанное время\n"
        "  • <code>через 2 часа</code> — через N часов\n"
        "  • <code>через 30 минут</code> — через N минут\n\n"
        "Задачи привязаны к <b>этому чату</b> — у каждой переписки свой список.\n\n"
        "<b>Команды:</b>\n"
        "/tasks — открытые задачи\n"
        "/archive — архив\n"
        "/notifications — уведомления\n"
        "/overdue — просроченные"
    )


@dp.message(Command("tasks", "deals"))
async def cmd_tasks(message: Message) -> None:
    user_id = message.from_user.id
    scope = scope_from_message(message)
    await touch_scope(scope, chat_title(message), user_id)

    if message.chat.type == ChatType.PRIVATE and message.chat.id == user_id:
        scopes = await db.get_user_scopes(user_id)
        if not scopes:
            await message.answer("📭 У тебя пока нет задач. Добавь через inline в чате!")
            return
        tasks = await db.get_tasks_for_scopes(scopes)
        text = format_tasks_list(tasks, "📋 <b>Твои открытые задачи (все чаты):</b>")
    else:
        tasks = await db.get_tasks(scope)
        text = format_tasks_list(tasks)

    kb = tasks_keyboard(tasks) if tasks else None
    await message.answer(text, reply_markup=kb)


@dp.message(Command("archive"))
async def cmd_archive(message: Message) -> None:
    user_id = message.from_user.id
    scope = scope_from_message(message)
    await touch_scope(scope, chat_title(message), user_id)

    if message.chat.type == ChatType.PRIVATE and message.chat.id == user_id:
        scopes = await db.get_user_scopes(user_id)
        tasks = await db.get_archived_for_scopes(scopes)
    else:
        tasks = await db.get_archived_tasks(scope)

    await message.answer(format_archive_list(tasks))


@dp.message(Command("notifications"))
async def cmd_notifications(message: Message) -> None:
    user_id = message.from_user.id
    await touch_scope(scope_from_message(message), chat_title(message), user_id)

    notifications = await db.get_user_notifications(user_id)
    if not notifications:
        await message.answer("🔔 Уведомлений нет")
        return

    await message.answer(format_notifications_list(notifications))
    await db.mark_notifications_read(user_id)


@dp.message(Command("overdue"))
async def cmd_overdue(message: Message) -> None:
    user_id = message.from_user.id
    scope = scope_from_message(message)
    await touch_scope(scope, chat_title(message), user_id)

    if message.chat.type == ChatType.PRIVATE and message.chat.id == user_id:
        scopes = await db.get_user_scopes(user_id)
        tasks = await db.get_overdue_tasks(scopes) if scopes else []
    else:
        tasks = await db.get_overdue_tasks([scope])

    if not tasks:
        await message.answer("✅ Просроченных задач нет!")
        return

    lines = [f"🚨 <b>Просроченные ({len(tasks)}):</b>\n"]
    for t in tasks:
        lines.append(
            f"• {t['text']}\n  👤 {t['from_user']} · ⏰ {fmt_date(t['deadline'])}"
        )
    await message.answer("\n\n".join(lines), reply_markup=tasks_keyboard(tasks))


@dp.message(F.text & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}))
async def handle_chat_activity(message: Message) -> None:
    """Любая активность в чате — регистрируем участника и контекст."""
    if message.from_user.is_bot:
        return
    scope = scope_from_message(message)
    title = chat_title(message)
    uid = message.from_user.id
    await touch_scope(scope, title, uid)
    _inline_scope[uid] = (scope, title)
    if message.chat.type == ChatType.PRIVATE and message.chat.id != uid:
        remember_private_chat(uid, scope, title)
        await touch_scope(scope, title, message.chat.id)


@dp.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated) -> None:
    if event.new_chat_member.status in ("member", "administrator"):
        scope = str(event.chat.id)
        title = event.chat.title or "Группа"
        if event.from_user:
            await touch_scope(scope, title, event.from_user.id)
            _inline_scope[event.from_user.id] = (scope, title)


# ════════════════════════════════════════════════════════════════
#  CALLBACK КНОПКИ ✅
# ════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("done_task:"))
async def cb_done_task(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return

    user_id = callback.from_user.id
    if not await ensure_scope_access(user_id, task["scope"]):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await db.close_task(task_id)
    await callback.answer("✅ Выполнено!")

    # Удаляем запланированное напоминание, если было
    try:
        scheduler.remove_job(f"reminder_{task_id}")
    except Exception:
        pass

    old_kb = callback.message.reply_markup
    if old_kb:
        new_rows = [
            row for row in old_kb.inline_keyboard
            if not any(btn.callback_data == callback.data for btn in row)
        ]
        new_kb = InlineKeyboardMarkup(inline_keyboard=new_rows) if new_rows else None
        await callback.message.edit_reply_markup(reply_markup=new_kb)


# ════════════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК
# ════════════════════════════════════════════════════════════════

async def morning_digest() -> None:
    try:
        tasks = await db.get_overdue_tasks()
        if not tasks:
            return

        by_scope: dict[str, list[dict]] = {}
        for t in tasks:
            by_scope.setdefault(t["scope"], []).append(t)

        for scope, scope_tasks in by_scope.items():
            lines = [f"☀️ <b>Просроченные задачи ({len(scope_tasks)}):</b>\n"]
            for t in scope_tasks:
                lines.append(
                    f"• {t['text']}\n  👤 {t['from_user']} · ⏰ {fmt_date(t['deadline'])}"
                )
            if is_telegram_chat(scope):
                try:
                    await bot.send_message(
                        int(scope), "\n\n".join(lines),
                        reply_markup=tasks_keyboard(scope_tasks),
                    )
                except Exception as e:
                    log.warning(f"Morning digest to {scope}: {e}")

            for uid in await db.get_scope_user_ids(scope):
                try:
                    await bot.send_message(uid, "\n\n".join(lines))
                except Exception:
                    pass
    except Exception as e:
        log.error(f"Morning digest error: {e}")


# ════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════

async def main() -> None:
    global _bot_username
    log.info("Starting bot...")
    try:
        await db.init_db()
    except Exception:
        log.exception("Database init failed")
        raise
    log.info("Database OK")
    try:
        me = await bot.get_me()
        _bot_username = me.username or ""
        log.info("Bot authorized: @%s", _bot_username)
    except TelegramUnauthorizedError:
        log.error(
            "Неверный BOT_TOKEN. Получи новый у @BotFather → /mybots → Bot Settings → API Token, "
            "запиши в /root/tg-assistant/.env и перезапусти: systemctl restart tg-assistant"
        )
        raise SystemExit(1) from None

    wh = await bot.get_webhook_info()
    if wh.url:
        log.warning("Webhook был установлен (%s) — удаляем для polling", wh.url)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Polling mode, webhook cleared")

    if OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                "🤖 Бот запущен! Используй @taskFaster_bot в любом чате с собеседником.\n\n"
                "Теперь можно указывать время напоминания: @taskFaster_bot позвонить маме завтра в 15:30",
            )
        except Exception:
            pass
    
    scheduler.add_job(morning_digest, "cron", hour=9, minute=0)
    scheduler.start()
    
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message", "inline_query", "chosen_inline_result",
            "callback_query", "my_chat_member",
            "business_connection", "business_message",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())