import asyncio
import hashlib
import html
import logging
import os
import re
import time
from urllib.parse import quote
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
from premium_emoji import PE, e, ib, install_html_send_patch, is_premium_emoji_enabled

load_dotenv()
install_html_send_patch()

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

# «ночевка завтра в 17:00 с <имя>»
_pending_add_user: set[int] = set()
_pending_task_input: dict[int, str] = {}  # personal | shared
_ASSIGNEE_SUFFIX_RE = re.compile(
    r"\s+(?:с|для)\s+([a-zA-Zа-яА-ЯёЁ0-9_.\-]{2,32})\s*$",
    re.IGNORECASE,
)


# ════════════════════════════════════════════════════════════════
#  ХЕЛПЕРЫ
# ════════════════════════════════════════════════════════════════

def now_moscow() -> datetime:
    """Текущее время в Москве (naive, для сравнений и планировщика)."""
    return datetime.now(MOSCOW).replace(tzinfo=None)


def get_chat_instance(obj) -> str | None:
    """chat_instance только у ChosenInlineResult, не у InlineQuery."""
    try:
        value = getattr(obj, "chat_instance", None)
    except AttributeError:
        value = None
    if value is not None:
        return str(value)
    extra = getattr(obj, "model_extra", None) or {}
    if isinstance(extra, dict):
        raw = extra.get("chat_instance")
        if raw is not None:
            return str(raw)
    return None


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


def telegram_share_url(link: str, text: str) -> str:
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"


def alias_join_link(join_token: str) -> str:
    return f"https://t.me/{_bot_username}?start=join_{join_token}"


def alias_share_text(display: str) -> str:
    """Текст для t.me/share/url — без ссылки (она передаётся отдельно в url=)."""
    return f"Присоединись к задачам как «{display}». Открой бота и нажми Start."


async def split_assignee(text: str, owner_id: int) -> tuple[str, dict | None]:
    m = _ASSIGNEE_SUFFIX_RE.search(text)
    if not m:
        return text, None
    clean = text[: m.start()].strip()
    alias = await db.get_user_alias(owner_id, m.group(1))
    return clean, alias


async def prepare_task_from_query(
    query: str,
    owner_id: int,
) -> tuple[str, str | None, str | None, dict | None, dict | None]:
    """Текст задачи, reminder_at, deadline, alias (или None), ошибка assignee."""
    m = _ASSIGNEE_SUFFIX_RE.search(query)
    body, alias = await split_assignee(query, owner_id)
    assignee_error = None
    if m and not alias:
        assignee_error = {
            "name": m.group(1),
            "message": "Участник не найден. Создайте: /addusers имя",
        }
    reminder_at, clean = parse_reminder_time(body)
    result = analyze_message(clean)
    text = result.task_text or clean
    return text, reminder_at, result.deadline, alias, assignee_error


async def send_alias_invite(message: Message, alias: dict) -> None:
    display = alias["display_name"]
    link = alias_join_link(alias["join_token"])
    linked = alias.get("linked_user_id")
    status = f"{e(PE.CHECK)} уже подключён" if linked else f"{e(PE.CLOCK)} ждёт подключения"
    share_text = alias_share_text(display)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Выбрать контакт и отправить",
            url=telegram_share_url(link, share_text),
            icon_custom_emoji_id=PE.SEND,
        ),
    ]])
    await message.answer(
        f"{e(PE.PROFILE)} Участник <b>{display}</b> ({status})\n\n"
        f"Приглашение — присоединиться к задачам как «<b>{display}</b>»:\n"
        f"<a href=\"{link}\">{link}</a>\n\n"
        "Нажмите кнопку ниже — Telegram предложит выбрать чат "
        "и перешлёт это сообщение от вашего имени.",
        reply_markup=kb,
    )


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


def code_bot_mention(suffix: str = "", bot_name: str | None = None) -> str:
    """@бот в <code> без конфликта HTML-сущностей (ENTITY_TEXT_INVALID)."""
    name = html.escape(bot_name or _bot_username or "taskFaster_bot")
    body = f"&#64;{name}{suffix}"
    return f"<code>{body}</code>"


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


def task_display_no(task: dict, viewer_id: int | None) -> int:
    """Личный номер задачи для пользователя (не глобальный id из БД)."""
    if viewer_id is not None:
        if task.get("from_user_id") == viewer_id and task.get("creator_number"):
            return task["creator_number"]
        if task.get("assignee_user_id") == viewer_id and task.get("assignee_number"):
            return task["assignee_number"]
    return task.get("creator_number") or task["id"]


def _btn_back_menu() -> InlineKeyboardButton:
    return ib("Меню", "menu_main", PE.BACK)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ib("Мои задачи", "menu_tasks_personal", PE.FILE),
            ib("С людьми", "menu_tasks_shared", PE.PEOPLE),
        ],
        [
            ib("Своя задача", "menu_add_personal", PE.ADD_TEXT),
            ib("С участником", "menu_add_shared", PE.PERSON_OK),
        ],
        [
            ib("Просроченные", "menu_overdue", PE.TIME_PAST),
            ib("Архив", "menu_archive", PE.BOX),
        ],
        [
            ib("Участники", "menu_users", PE.PROFILE),
            ib("Уведомления", "menu_notifications", PE.BELL),
        ],
        [ib("Как добавить в чате", "menu_help", PE.INFO)],
    ])


def archive_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ib("Мои", "menu_archive_personal", PE.FILE),
            ib("С людьми", "menu_archive_shared", PE.PEOPLE),
        ],
        [_btn_back_menu()],
    ])


def overdue_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ib("Мои", "menu_overdue_personal", PE.FILE),
            ib("С людьми", "menu_overdue_shared", PE.PEOPLE),
        ],
        [_btn_back_menu()],
    ])


def tasks_keyboard(
    tasks: list[dict],
    viewer_id: int | None = None,
    *,
    with_menu: bool = True,
) -> InlineKeyboardMarkup:
    buttons = []
    for t in tasks:
        num = task_display_no(t, viewer_id)
        buttons.append([ib(f"#{num} выполнено", f"done_task:{t['id']}", PE.CHECK)])
    if with_menu:
        buttons.append([_btn_back_menu()])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def archive_keyboard(tasks: list[dict], archive_kind: str) -> InlineKeyboardMarkup:
    """Кнопки удаления для архива (archive_kind: personal | shared)."""
    buttons: list[list[InlineKeyboardButton]] = []
    for i, t in enumerate(tasks, 1):
        buttons.append([
            ib(f"Удалить #{i}", f"del_task:{t['id']}:{archive_kind}", PE.TRASH),
        ])
    buttons.append([ib("К архиву", "menu_archive", PE.BACK)])
    buttons.append([_btn_back_menu()])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def archive_delete_confirm_keyboard(task_id: int, archive_kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ib("Удалить", f"del_task_ok:{task_id}:{archive_kind}", PE.CHECK),
            ib("Отмена", f"del_task_no:{archive_kind}", PE.CROSS),
        ],
    ])


def format_tasks_list(
    tasks: list[dict],
    title: str | None = None,
    viewer_id: int | None = None,
) -> str:
    if not tasks:
        return f"{e(PE.BOX)} Открытых задач нет"
    head = title or f"{e(PE.FILE)} <b>Открытые задачи:</b>"
    lines = [f"{head}\n"]
    for t in tasks:
        num = task_display_no(t, viewer_id)
        added = fmt_date(t.get("added_at"))
        deadline = f" · {e(PE.CLOCK)} до {fmt_date(t['deadline'])}" if t.get("deadline") else ""
        reminder = (
            f" · {e(PE.BELL)} напомнить в {fmt_datetime(t.get('reminder_at'))}"
            if t.get("reminder_at") else ""
        )
        assignee = f" · {e(PE.TAG)} {t['assignee_label']}" if t.get("assignee_label") else ""
        lines.append(
            f"<b>#{num}</b> {t['text']}\n"
            f"    {e(PE.PROFILE)} {t['from_user']}{assignee}{deadline}{reminder}\n"
            f"    {e(PE.WRITE)} {t['chat_title']} · {e(PE.CALENDAR)} {added}"
        )
    return "\n\n".join(lines)


def format_archive_list(tasks: list[dict], viewer_id: int | None = None) -> str:
    """В архиве своя нумерация #1, #2… — не смешивается с открытыми задачами."""
    if not tasks:
        return f"{e(PE.BOX)} Архив пуст"
    lines = [f"{e(PE.BOX)} <b>Архив задач:</b>\n"]
    for i, t in enumerate(tasks, 1):
        closed = fmt_date(t.get("closed_at") or t.get("added_at"))
        deadline = f" · {e(PE.CLOCK)} {fmt_date(t['deadline'])}" if t.get("deadline") else ""
        lines.append(
            f"<b>#{i}</b> {t['text']}\n"
            f"    {e(PE.PROFILE)} {t['from_user']}{deadline}\n"
            f"    {e(PE.CHECK)} закрыто {closed}"
        )
    return "\n\n".join(lines)


def users_list_keyboard(aliases: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for a in aliases:
        label = a["display_name"]
        if len(label) > 22:
            label = label[:21] + "…"
        rows.append([
            ib(label, f"user_invite:{a['id']}", PE.SEND),
            ib("Удалить", f"user_del:{a['id']}", PE.TRASH),
        ])
    rows.append([
        ib("Добавить", "users_add", PE.ADD_TEXT),
        _btn_back_menu(),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def users_delete_confirm_keyboard(alias_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            ib("Удалить", f"user_del_ok:{alias_id}", PE.CHECK),
            ib("Отмена", "menu_users", PE.CROSS),
        ],
    ])


def main_menu_text() -> str:
    smile = e(PE.SMILE)
    return (
        f"{smile} <b>TaskManager</b>\n\n"
        "Выберите действие кнопкой ниже.\n\n"
        f"• <b>Мои задачи</b> — без участников\n"
        f"• <b>С людьми</b> — с указанием участника (<code>с имя</code>)\n"
        f"• В переписке: {code_bot_mention(' текст завтра в 17:00')}"
    )


def dm_chat_label(
    recipient_id: int,
    chat_title_str: str,
    assignee_user_id: int | None,
    assignee_label: str | None,
) -> str:
    """Подпись чата в личном уведомлении (для исполнителя — как его записал автор)."""
    if assignee_user_id and recipient_id == assignee_user_id:
        if assignee_label:
            return f"Личка с {assignee_label}"
        return "Личка с пользователем"
    return chat_title_str


async def show_archive_list(
    message: Message,
    user_id: int,
    archive_kind: str,
    *,
    edit: bool = False,
) -> None:
    if archive_kind == "personal":
        tasks = await db.get_archived_personal_for_user(user_id)
        empty = f"{e(PE.BOX)} Архив своих задач пуст"
    else:
        tasks = await db.get_archived_shared_for_user(user_id)
        empty = f"{e(PE.BOX)} Архив задач с участниками пуст"
    if not tasks:
        text = empty
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ib("К архиву", "menu_archive", PE.BACK)],
            [_btn_back_menu()],
        ])
    else:
        text = format_archive_list(tasks, viewer_id=user_id)
        kb = archive_keyboard(tasks, archive_kind)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


async def show_main_menu(message: Message, edit: bool = False) -> None:
    kb = main_menu_keyboard()
    text = main_menu_text()
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


async def show_task_list(
    message: Message,
    tasks: list[dict],
    title: str,
    viewer_id: int,
    *,
    edit: bool = False,
    empty_hint: str | None = None,
) -> None:
    if empty_hint is None:
        empty_hint = f"{e(PE.BOX)} Задач нет"
    if not tasks:
        text = f"{empty_hint}\n\nСоздайте через меню или inline в чате."
        kb = InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]])
    else:
        text = format_tasks_list(tasks, title, viewer_id=viewer_id)
        kb = tasks_keyboard(tasks, viewer_id=viewer_id)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


async def send_users_list(message: Message, edit: bool = False) -> None:
    owner_id = message.from_user.id
    aliases = await db.list_user_aliases(owner_id)
    if not aliases:
        text = (
            f"{e(PE.PEOPLE)} <b>Участники</b>\n\n"
            "Добавьте человека — потом в задаче укажите <code>с имя</code>.\n"
            "Пример в чате:\n"
            f"{code_bot_mention(' встреча завтра в 17:00 с имя')}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            ib("Добавить", "users_add", PE.ADD_TEXT),
            _btn_back_menu(),
        ]])
    else:
        lines = [f"{e(PE.PEOPLE)} <b>Ваши участники:</b>\n"]
        for a in aliases:
            st = f"{e(PE.CHECK)} подключён" if a.get("linked_user_id") else f"{e(PE.CLOCK)} ждёт"
            lines.append(f"• <b>{a['display_name']}</b> — {st}")
        lines.append(f"\n{e(PE.SEND)} пригласить · {e(PE.TRASH)} удалить")
        text = "\n".join(lines)
        kb = users_list_keyboard(aliases)
    if edit and message.reply_markup:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


def format_notifications_list(notifications: list[dict]) -> str:
    if not notifications:
        return f"{e(PE.BELL)} Новых уведомлений нет"
    lines = [f"{e(PE.BELL)} <b>Твои уведомления:</b>\n"]
    for i, n in enumerate(notifications, 1):
        created = fmt_date(n.get("created_at"))
        unread = f" {e(PE.EYE)}" if not n.get("is_read") else ""
        lines.append(f"<b>#{i}</b>{unread} {n['text']}\n    {e(PE.CALENDAR)} {created}")
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


def dm_notify_recipients(
    creator_id: int,
    assignee_user_id: int | None,
    posted_to_chat: bool,
) -> list[int]:
    """Кому слать личное уведомление: только автор (если не ушло в чат) и исполнитель."""
    recipients: list[int] = []
    if not posted_to_chat:
        recipients.append(creator_id)
    if assignee_user_id and assignee_user_id not in recipients:
        recipients.append(assignee_user_id)
    return recipients


async def ensure_task_access(user_id: int, task: dict) -> bool:
    if task["from_user_id"] == user_id:
        return True
    if task.get("assignee_user_id") == user_id:
        return True
    return task["scope"] in await db.get_user_scopes(user_id)


async def save_and_notify(
    text: str,
    scope: str,
    chat_title_str: str,
    user_id: int,
    user_name: str,
    deadline: str | None,
    reminder_at: str | None = None,
    assignee_user_id: int | None = None,
    assignee_label: str | None = None,
    alias: dict | None = None,
) -> int:
    created = await db.add_task(
        text, scope, chat_title_str, user_name, user_id, deadline, reminder_at,
        assignee_user_id, assignee_label,
    )
    task_id = created["id"]
    creator_no = created["creator_number"]
    assignee_no = created.get("assignee_number")
    await touch_scope(scope, chat_title_str, user_id)

    if assignee_user_id:
        await touch_scope(scope, chat_title_str, assignee_user_id)

    deadline_line = f"\n⏰ Дедлайн: <b>{fmt_date(deadline)}</b>" if deadline else ""
    reminder_line = f"\n🔔 Напоминание: <b>{fmt_datetime(reminder_at)}</b>" if reminder_at else ""
    assignee_line = f"\n🎯 Исполнитель: <b>{assignee_label}</b>" if assignee_label else ""
    notify_text = (
        f"📝 <b>Новая задача</b> #{creator_no}\n"
        f"👤 {user_name}{assignee_line}{deadline_line}{reminder_line}\n"
        f"📝 {text}"
    )

    posted_to_chat = False
    if is_telegram_chat(scope) and scope not in _opaque_scopes:
        try:
            await bot.send_message(int(scope), notify_text)
            posted_to_chat = True
        except Exception as e:
            log.warning(f"Cannot post to chat {scope}: {e}")

    notify_uids = dm_notify_recipients(user_id, assignee_user_id, posted_to_chat)
    assignee_notified = False
    for uid in notify_uids:
        num = creator_no if uid == user_id else (assignee_no or creator_no)
        short = f"📝 Новая задача #{num}: {text[:80]}"
        if assignee_label:
            short += f" → {assignee_label}"
        if deadline:
            short += f" (до {fmt_date(deadline)})"
        if reminder_at:
            short += f" 🔔 в {fmt_datetime(reminder_at)}"
        await db.add_notification(uid, scope, task_id, short)
        chat_label = dm_chat_label(uid, chat_title_str, assignee_user_id, assignee_label)
        try:
            await bot.send_message(uid, f"🔔 {short}\n💬 {chat_label}")
            if assignee_user_id and uid == assignee_user_id:
                assignee_notified = True
        except Exception:
            pass

    if assignee_label and not assignee_notified and alias and _bot_username:
        link = alias_join_link(alias["join_token"])
        try:
            await bot.send_message(
                user_id,
                f"⚠️ <b>{assignee_label}</b> ещё не подключился к боту.\n"
                f"Перешлите приглашение:\n<a href=\"{link}\">{link}</a>",
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
    task = await db.get_task(task_id)
    assignee_line = ""
    if task and task.get("assignee_label"):
        assignee_line = f"\n🎯 {task['assignee_label']}"

    if is_telegram_chat(scope) and scope not in _opaque_scopes:
        num = task_display_no(task, task["from_user_id"]) if task else task_id
        chat_text = f"🔔 <b>Напоминание о задаче!</b> #{num}{assignee_line}\n📝 {text}"
        try:
            await bot.send_message(int(scope), chat_text)
        except Exception as e:
            log.warning(f"Cannot send reminder to chat {scope}: {e}")

    if task:
        creator_id = task["from_user_id"]
        assignee_id = task.get("assignee_user_id")
        for uid in dm_notify_recipients(creator_id, assignee_id, posted_to_chat=False):
            num = task_display_no(task, uid)
            reminder_text = (
                f"🔔 <b>Напоминание о задаче!</b> #{num}{assignee_line}\n📝 {text}"
            )
            chat_label = dm_chat_label(
                uid,
                chat_title_str,
                assignee_id,
                task.get("assignee_label"),
            )
            try:
                await bot.send_message(uid, f"🔔 {reminder_text}\n💬 {chat_label}")
            except Exception:
                pass


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
                    description="Например: задача завтра в 17:00 с имя",
                    input_message_content=InputTextMessageContent(
                        message_text="Используй: @taskFaster_bot задача [текст]",
                        parse_mode=None,
                    ),
                ),
            ]
        else:
            text, reminder_at, deadline, alias, assignee_err = await prepare_task_from_query(
                query, user.id,
            )
            is_task = bool(text.strip())
            card_id = _inline_result_id(user.id, query, "auto" if is_task else "manual")

            predicted = await predicted_scope_for_user(user.id, chat_type)
            notify_token = None
            if predicted and not alias:
                notify_token = await db.create_notify_token(predicted[0], predicted[1])

            if is_task:
                results = [_task_card(
                    card_id, text, deadline, reminder_at,
                    alias=alias, assignee_error=assignee_err, notify_token=notify_token,
                )]
            else:
                results = [_task_card(
                    card_id, query, None, reminder_at, manual=True,
                    alias=alias, assignee_error=assignee_err,
                )]

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
    alias: dict | None = None,
    assignee_error: dict | None = None,
) -> InlineQueryResultArticle:
    title = "📝 Сохранить как задачу" if manual else "📝 Задача — нажми чтобы отправить"
    deadline_hint = f" (до {fmt_date(deadline)})" if deadline else ""
    reminder_hint = f" 🔔 в {fmt_datetime(reminder_at)}" if reminder_at else ""
    assignee_hint = f" · 👤 {alias['display_name']}" if alias else ""
    if assignee_error:
        assignee_hint = f" · ⚠️ нет «{assignee_error['name']}»"
    deadline_line = f"\n⏰ Дедлайн: {fmt_date(deadline)}" if deadline else ""
    reminder_line = f"\n🔔 Напоминание: {fmt_datetime(reminder_at)}" if reminder_at else ""
    assignee_line = f"\n🎯 {alias['display_name']}" if alias else ""
    msg = f"[Бот]\nДобавил задачу в список!\n📝 {text}{assignee_line}{deadline_line}{reminder_line}"

    buttons: list[list[InlineKeyboardButton]] = []
    if alias and _bot_username and not alias.get("linked_user_id"):
        link = alias_join_link(alias["join_token"])
        msg += f"\n\n👤 Подключить «{alias['display_name']}»: {link}"
        share_text = alias_share_text(alias["display_name"])
        buttons.append([InlineKeyboardButton(
            text="Подключить участника",
            url=telegram_share_url(link, share_text),
            icon_custom_emoji_id=PE.LINK,
        )])
    elif notify_token and _bot_username:
        msg += f"\n\n🔔 Собеседник: t.me/{_bot_username}?start=notify_{notify_token}"
        buttons.append([InlineKeyboardButton(
            text="Подписаться на уведомления",
            url=f"https://t.me/{_bot_username}?start=notify_{notify_token}",
            icon_custom_emoji_id=PE.BELL,
        )])

    reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=f"{text[:80]}{assignee_hint}{deadline_hint}{reminder_hint}",
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

    text, reminder_at, deadline, alias, assignee_err = await prepare_task_from_query(query, user.id)
    if assignee_err:
        try:
            await bot.send_message(
                user.id,
                f"⚠️ Участник «{assignee_err['name']}» не найден.\n"
                "Добавьте в «👤 Участники» в меню бота.",
            )
        except Exception:
            pass

    assignee_uid = alias.get("linked_user_id") if alias else None
    assignee_label = alias["display_name"] if alias else None

    log.info(
        "Saving task: user=%s scope=%s text=%s reminder=%s assignee=%s",
        user.id, scope, text[:50], reminder_at, assignee_label,
    )

    try:
        task_id = await save_and_notify(
            text, scope, chat_title_str, user.id, name, deadline, reminder_at,
            assignee_user_id=assignee_uid,
            assignee_label=assignee_label,
            alias=alias,
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


def help_text() -> str:
    return (
        f"{e(PE.INFO)} <b>Как добавить задачу</b>\n\n"
        "<b>В чате с человеком или в группе:</b>\n"
        f"{code_bot_mention(' текст завтра в 17:00')}\n"
        "→ нажмите на карточку <b>над полем ввода</b>\n\n"
        f"<b>С участником</b> (сначала «{e(PE.PROFILE)} Участники»):\n"
        f"{code_bot_mention(' встреча завтра в 17:00 с имя')}\n\n"
        f"<b>Время (Москва):</b>\n"
        "• <code>завтра в 19:00</code>\n"
        "• <code>сегодня в 15:30</code>\n"
        "• <code>в 15:30</code>\n"
        "• <code>через 2 часа</code> · <code>через 30 минут</code>"
    )


async def create_task_from_text(
    message: Message,
    text: str,
    *,
    personal_only: bool = False,
) -> None:
    if not text:
        await message.answer(help_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]))
        return

    user = message.from_user
    scope = scope_from_message(message)
    title = chat_title(message)
    await touch_scope(scope, title, user.id)

    task_text, reminder_at, deadline, alias, assignee_err = await prepare_task_from_query(
        text, user.id,
    )
    if personal_only and (alias or assignee_err):
        await message.answer(
            f"{e(PE.INFO)} Это «своя» задача — без участника.\n"
            f"Уберите <code>с имя</code> из текста или нажмите «{e(PE.PERSON_OK)} С участником».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )
        return
    if assignee_err:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ib("Участники", "menu_users", PE.PROFILE)],
            [_btn_back_menu()],
        ])
        await message.answer(
            f"{e(PE.INFO)} Участник «{assignee_err['name']}» не найден.\n"
            "Добавьте в разделе «Участники».",
            reply_markup=kb,
        )
        return

    assignee_uid = alias.get("linked_user_id") if alias else None
    assignee_label = alias["display_name"] if alias else None

    try:
        task_id = await save_and_notify(
            task_text, scope, title, user.id, sender_name(user), deadline, reminder_at,
            assignee_user_id=assignee_uid,
            assignee_label=assignee_label,
            alias=alias,
        )
        deadline_s = f" · {e(PE.CLOCK)} {fmt_date(deadline)}" if deadline else ""
        reminder = f" · {e(PE.BELL)} {fmt_datetime(reminder_at)}" if reminder_at else ""
        who = f" · {e(PE.TAG)} {assignee_label}" if assignee_label else ""
        task = await db.get_task(task_id)
        num = task_display_no(task, user.id) if task else task_id
        kind = e(PE.PEOPLE) if assignee_label else e(PE.FILE)
        await message.answer(
            f"{e(PE.CHECK)} {kind} Задача #{num} сохранена{who}{deadline_s}{reminder}\n"
            f"{e(PE.PENCIL)} {task_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )
    except Exception:
        log.exception("Error saving task from message")
        await message.answer(
            f"{e(PE.CROSS)} Не удалось сохранить задачу",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )


@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await show_main_menu(message)


@dp.message(Command("task"))
async def cmd_task(message: Message) -> None:
    text = command_args(message)
    if not text:
        _pending_task_input[message.from_user.id] = "shared"
        await message.answer(
            f"{e(PE.PENCIL)} Опишите задачу одним сообщением.\n"
            "Для участника добавьте в конце: <code>с имя</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )
        return
    await create_task_from_text(message, text)


@dp.message(Command("users"))
async def cmd_users(message: Message) -> None:
    await send_users_list(message)


@dp.message(Command("addusers", "adduser"))
async def cmd_addusers(message: Message) -> None:
    name = command_args(message).strip()
    if not name:
        await send_users_list(message)
        return
    if len(name) > 32:
        await message.answer("❌ Имя слишком длинное (макс. 32 символа).")
        return
    alias = await db.create_user_alias(message.from_user.id, name)
    await send_alias_invite(message, alias)


@dp.message(Command("delusers", "deluser", "rmuser"))
async def cmd_delusers(message: Message) -> None:
    name = command_args(message).strip()
    if not name:
        await message.answer("🗑 Выберите участника в /users и нажмите 🗑")
        return
    removed = await db.delete_user_alias(message.from_user.id, name)
    if not removed:
        await message.answer(f"❌ Участник «{name}» не найден. Список: /users")
        return
    await message.answer(f"🗑 Участник <b>{removed['display_name']}</b> удалён.")


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

    if args.startswith("join_"):
        token = args[5:]
        alias = await db.link_alias_user(token, user_id)
        if alias:
            await message.answer(
                f"✅ Вы подключены как <b>{alias['display_name']}</b>!\n"
                "Будете получать задачи и напоминания, где указано ваше имя.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
            )
            return
        await message.answer("❌ Ссылка недействительна. Попросите новое приглашение.")
        return

    scope = scope_from_message(message)
    await touch_scope(scope, chat_title(message), user_id)
    await show_main_menu(message)


def _is_private_bot_chat(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and message.from_user
        and message.chat.id == message.from_user.id
    )


@dp.message(Command("tasks", "deals"))
async def cmd_tasks(message: Message) -> None:
    user_id = message.from_user.id
    await touch_scope(scope_from_message(message), chat_title(message), user_id)
    if _is_private_bot_chat(message):
        await show_main_menu(message)
        return
    await message.answer(
        f"{e(PE.FILE)} Задачи этого чата — выберите тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                ib("Мои", f"scope_tasks_p:{message.chat.id}", PE.FILE),
                ib("С людьми", f"scope_tasks_s:{message.chat.id}", PE.PEOPLE),
            ],
            [_btn_back_menu()],
        ]),
    )


@dp.message(Command("archive"))
async def cmd_archive(message: Message) -> None:
    user_id = message.from_user.id
    await touch_scope(scope_from_message(message), chat_title(message), user_id)
    if _is_private_bot_chat(message):
        await message.answer(
            f"{e(PE.BOX)} <b>Архив</b> — выберите:",
            reply_markup=archive_menu_keyboard(),
        )
        return
    await message.answer(
        f"{e(PE.BOX)} Архив чата:",
        reply_markup=archive_menu_keyboard(),
    )


@dp.message(Command("notifications"))
async def cmd_notifications(message: Message) -> None:
    user_id = message.from_user.id
    await touch_scope(scope_from_message(message), chat_title(message), user_id)
    notifications = await db.get_user_notifications(user_id)
    if not notifications:
        await message.answer(
            f"{e(PE.BELL)} Уведомлений нет",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )
        return
    await message.answer(
        format_notifications_list(notifications),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
    )
    await db.mark_notifications_read(user_id)


@dp.message(Command("overdue"))
async def cmd_overdue(message: Message) -> None:
    user_id = message.from_user.id
    await touch_scope(scope_from_message(message), chat_title(message), user_id)
    if _is_private_bot_chat(message):
        await message.answer(
            f"{e(PE.TIME_PAST)} <b>Просроченные</b> — выберите:",
            reply_markup=overdue_menu_keyboard(),
        )
        return
    await message.answer(
        f"{e(PE.TIME_PAST)} Просроченные:",
        reply_markup=overdue_menu_keyboard(),
    )


@dp.message(F.text & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}))
async def handle_chat_activity(message: Message) -> None:
    """Любая активность в чате — регистрируем участника и контекст."""
    if message.from_user.is_bot:
        return
    uid = message.from_user.id
    if uid in _pending_task_input:
        mode = _pending_task_input.pop(uid)
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            await message.answer(
                f"{e(PE.CROSS)} Напишите текст задачи.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
            )
            return
        await create_task_from_text(message, text, personal_only=(mode == "personal"))
        return
    if uid in _pending_add_user:
        _pending_add_user.discard(uid)
        name = (message.text or "").strip()
        if not name or name.startswith("/"):
            await message.answer("❌ Отправьте имя участника текстом (не команду).")
            return
        if len(name) > 32:
            await message.answer("❌ Имя слишком длинное (макс. 32 символа).")
            return
        alias = await db.create_user_alias(uid, name)
        await send_alias_invite(message, alias)
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

@dp.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_main_menu(callback.message, edit=True)


@dp.callback_query(F.data == "menu_tasks_personal")
async def cb_menu_tasks_personal(callback: CallbackQuery) -> None:
    await callback.answer()
    uid = callback.from_user.id
    tasks = await db.get_personal_tasks_for_user(uid)
    await show_task_list(
        callback.message,
        tasks,
        f"{e(PE.FILE)} <b>Мои задачи</b> (без участников)",
        uid,
        edit=True,
        empty_hint=f"{e(PE.BOX)} Своих задач нет",
    )


@dp.callback_query(F.data == "menu_tasks_shared")
async def cb_menu_tasks_shared(callback: CallbackQuery) -> None:
    await callback.answer()
    uid = callback.from_user.id
    tasks = await db.get_shared_tasks_for_user(uid)
    await show_task_list(
        callback.message,
        tasks,
        f"{e(PE.PEOPLE)} <b>Задачи с участниками</b>",
        uid,
        edit=True,
        empty_hint=f"{e(PE.BOX)} Задач с участниками нет",
    )


@dp.callback_query(F.data == "menu_add_personal")
async def cb_menu_add_personal(callback: CallbackQuery) -> None:
    _pending_task_input[callback.from_user.id] = "personal"
    await callback.answer()
    await callback.message.answer(
        f"{e(PE.PENCIL)} <b>Своя задача</b>\n\n"
        "Напишите одним сообщением, например:\n"
        "<code>купить молоко завтра в 18:00</code>\n\n"
        "Без <code>с имя</code> в конце.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
    )


@dp.callback_query(F.data == "menu_add_shared")
async def cb_menu_add_shared(callback: CallbackQuery) -> None:
    _pending_task_input[callback.from_user.id] = "shared"
    await callback.answer()
    await callback.message.answer(
        f"{e(PE.PENCIL)} <b>Задача с участником</b>\n\n"
        "Напишите задачу с именем в конце:\n"
        "<code>встреча завтра в 17:00 с имя</code>\n\n"
        f"Участника добавьте в «{e(PE.PROFILE)} Участники», если ещё нет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ib("Участники", "menu_users", PE.PROFILE)],
            [_btn_back_menu()],
        ]),
    )


@dp.callback_query(F.data == "menu_archive")
async def cb_menu_archive(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        f"{e(PE.BOX)} <b>Архив</b> — выберите раздел:",
        reply_markup=archive_menu_keyboard(),
    )


@dp.callback_query(F.data == "menu_archive_personal")
async def cb_menu_archive_personal(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_archive_list(
        callback.message, callback.from_user.id, "personal", edit=True,
    )


@dp.callback_query(F.data == "menu_archive_shared")
async def cb_menu_archive_shared(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_archive_list(
        callback.message, callback.from_user.id, "shared", edit=True,
    )


@dp.callback_query(F.data == "menu_overdue")
async def cb_menu_overdue(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        f"{e(PE.TIME_PAST)} <b>Просроченные</b> — выберите:",
        reply_markup=overdue_menu_keyboard(),
    )


async def _show_overdue(callback: CallbackQuery, tasks: list[dict], title: str) -> None:
    uid = callback.from_user.id
    if not tasks:
        await callback.message.edit_text(
            f"{e(PE.CHECK)} {title}: нет просроченных",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )
        return
    lines = [f"{e(PE.TIME_PAST)} <b>{title}</b> ({len(tasks)}):\n"]
    for t in tasks:
        num = task_display_no(t, uid)
        who = f" · {e(PE.TAG)} {t['assignee_label']}" if t.get("assignee_label") else ""
        lines.append(
            f"<b>#{num}</b> {t['text']}{who}\n  {e(PE.CLOCK)} {fmt_date(t['deadline'])}"
        )
    await callback.message.edit_text(
        "\n\n".join(lines),
        reply_markup=tasks_keyboard(tasks, viewer_id=uid),
    )


@dp.callback_query(F.data == "menu_overdue_personal")
async def cb_menu_overdue_personal(callback: CallbackQuery) -> None:
    await callback.answer()
    tasks = await db.get_overdue_personal_for_user(callback.from_user.id)
    await _show_overdue(callback, tasks, "Просроченные · мои")


@dp.callback_query(F.data == "menu_overdue_shared")
async def cb_menu_overdue_shared(callback: CallbackQuery) -> None:
    await callback.answer()
    tasks = await db.get_overdue_shared_for_user(callback.from_user.id)
    await _show_overdue(callback, tasks, "Просроченные · с людьми")


@dp.callback_query(F.data == "menu_users")
async def cb_menu_users(callback: CallbackQuery) -> None:
    await callback.answer()
    await send_users_list(callback.message, edit=True)


@dp.callback_query(F.data == "menu_notifications")
async def cb_menu_notifications(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    notifications = await db.get_user_notifications(uid)
    await callback.answer()
    if not notifications:
        await callback.message.edit_text(
            f"{e(PE.BELL)} Уведомлений нет",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
        )
        return
    await callback.message.edit_text(
        format_notifications_list(notifications),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
    )
    await db.mark_notifications_read(uid)


@dp.callback_query(F.data == "menu_help")
async def cb_menu_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        help_text(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn_back_menu()]]),
    )


@dp.callback_query(F.data.startswith("scope_tasks_p:"))
async def cb_scope_tasks_personal(callback: CallbackQuery) -> None:
    scope = callback.data.split(":", 1)[1]
    await callback.answer()
    tasks = await db.get_personal_tasks_for_scope(scope)
    await show_task_list(
        callback.message,
        tasks,
        f"{e(PE.FILE)} <b>Задачи чата</b> (без участников)",
        callback.from_user.id,
        edit=True,
        empty_hint=f"{e(PE.BOX)} Нет своих задач в этом чате",
    )


@dp.callback_query(F.data.startswith("scope_tasks_s:"))
async def cb_scope_tasks_shared(callback: CallbackQuery) -> None:
    scope = callback.data.split(":", 1)[1]
    await callback.answer()
    tasks = await db.get_shared_tasks_for_scope(scope)
    await show_task_list(
        callback.message,
        tasks,
        f"{e(PE.PEOPLE)} <b>Задачи чата</b> (с участниками)",
        callback.from_user.id,
        edit=True,
        empty_hint=f"{e(PE.BOX)} Нет задач с участниками",
    )


@dp.callback_query(F.data == "users_add")
async def cb_users_add(callback: CallbackQuery) -> None:
    _pending_add_user.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        f"{e(PE.PENCIL)} <b>Новый участник</b>\n\n"
        "Напишите имя одним сообщением (как будете указывать в задаче после «с»).",
    )


@dp.callback_query(F.data == "users_list")
async def cb_users_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await send_users_list(callback.message, edit=True)


@dp.callback_query(F.data.startswith("user_invite:"))
async def cb_user_invite(callback: CallbackQuery) -> None:
    alias_id = int(callback.data.split(":", 1)[1])
    alias = await db.get_user_alias_by_id(callback.from_user.id, alias_id)
    if not alias:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.answer("Приглашение отправлено")
    await send_alias_invite(callback.message, alias)


@dp.callback_query(F.data.startswith("user_del:"))
async def cb_user_del_ask(callback: CallbackQuery) -> None:
    alias_id = int(callback.data.split(":", 1)[1])
    alias = await db.get_user_alias_by_id(callback.from_user.id, alias_id)
    if not alias:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"{e(PE.TRASH)} Удалить участника <b>{alias['display_name']}</b>?",
        reply_markup=users_delete_confirm_keyboard(alias_id),
    )


@dp.callback_query(F.data.startswith("user_del_ok:"))
async def cb_user_del_ok(callback: CallbackQuery) -> None:
    alias_id = int(callback.data.split(":", 1)[1])
    removed = await db.delete_user_alias_by_id(callback.from_user.id, alias_id)
    if not removed:
        await callback.answer("Уже удалён", show_alert=True)
        return
    await callback.answer("Удалено")
    await send_users_list(callback.message, edit=True)


@dp.callback_query(F.data.startswith("done_task:"))
async def cb_done_task(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Задача не найдена")
        return

    user_id = callback.from_user.id
    if not await ensure_task_access(user_id, task):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await db.close_task(task_id)
    num = task_display_no(task, user_id)
    await callback.answer(f"✅ Задача #{num} выполнена!")

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


@dp.callback_query(F.data.startswith("del_task:"))
async def cb_del_task_ask(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка", show_alert=True)
        return
    task_id = int(parts[1])
    archive_kind = parts[2]
    task = await db.get_task(task_id)
    if not task or task.get("status") != "done":
        await callback.answer("Задача не найдена", show_alert=True)
        return
    if not await ensure_task_access(callback.from_user.id, task):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"{e(PE.TRASH)} Удалить задачу из архива?\n\n<b>{task['text']}</b>",
        reply_markup=archive_delete_confirm_keyboard(task_id, archive_kind),
    )


@dp.callback_query(F.data.startswith("del_task_ok:"))
async def cb_del_task_ok(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка", show_alert=True)
        return
    task_id = int(parts[1])
    archive_kind = parts[2]
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Уже удалена", show_alert=True)
        await show_archive_list(
            callback.message, callback.from_user.id, archive_kind, edit=True,
        )
        return
    if not await ensure_task_access(callback.from_user.id, task):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await db.delete_task(task_id)
    try:
        scheduler.remove_job(f"reminder_{task_id}")
    except Exception:
        pass
    await callback.answer("Удалено")
    await show_archive_list(
        callback.message, callback.from_user.id, archive_kind, edit=True,
    )


@dp.callback_query(F.data.startswith("del_task_no:"))
async def cb_del_task_no(callback: CallbackQuery) -> None:
    archive_kind = callback.data.split(":", 1)[1]
    await callback.answer()
    await show_archive_list(
        callback.message, callback.from_user.id, archive_kind, edit=True,
    )


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

            notified: set[int] = set()
            for t in scope_tasks:
                for uid in dm_notify_recipients(
                    t["from_user_id"],
                    t.get("assignee_user_id"),
                    posted_to_chat=bool(is_telegram_chat(scope) and scope not in _opaque_scopes),
                ):
                    if uid in notified:
                        continue
                    notified.add(uid)
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
        if is_premium_emoji_enabled():
            log.info("Premium emoji in messages: enabled (set PREMIUM_EMOJI=0 to disable)")
        else:
            log.info("Premium emoji in messages: disabled (PREMIUM_EMOJI=0)")
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