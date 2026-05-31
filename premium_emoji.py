"""Premium custom emoji IDs для Telegram (Bot API 7+)."""
import os
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_TG_EMOJI_RE = re.compile(
    r'<tg-emoji emoji-id="(\d+)">([^<]*)</tg-emoji>',
    re.IGNORECASE,
)

# Unicode-запасной вариант, если premium недоступен или ENTITY_TEXT_INVALID
_FALLBACKS: dict[str, str] = {
    "5870982283724328568": "⚙️",
    "5870994129244131212": "👤",
    "5870772616305839506": "👥",
    "5891207662678317861": "✅",
    "5893192487324880883": "❌",
    "5870528606328852614": "📄",
    "5870764288364252592": "😊",
    "5870930636742595124": "📈",
    "5870921681735781843": "📊",
    "5873147866364514353": "🏠",
    "6037249452824072506": "🔒",
    "6037496202990194718": "🔓",
    "6039422865189638057": "📣",
    "5870633910337015697": "✅",
    "5870657884844462243": "❌",
    "5870676941614354370": "✏️",
    "5870875489362513438": "🗑",
    "5893057118545646106": "◀️",
    "6039451237743595514": "📎",
    "5769289093221454192": "🔗",
    "6028435952299413210": "ℹ️",
    "6030400221232501136": "🤖",
    "6037397706505195857": "👁",
    "6037243349675544634": "🙈",
    "5963103826075456248": "📤",
    "6039802767931871481": "⬇️",
    "6039486778597970865": "🔔",
    "6032644646587338669": "🎁",
    "5983150113483134607": "🕐",
    "6041731551845159060": "🎉",
    "5870753782874246579": "✍️",
    "6035128606563241721": "🖼",
    "6042011682497106307": "📍",
    "5890937706803894250": "📅",
    "5886285355279193209": "🏷",
    "5775896410780079073": "⏰",
    "5884479287171485878": "📦",
    "5771851822897566479": "➕",
    "5904462880941545555": "💰",
    "5345906554510012647": "⏳",
}

_env_flag = os.getenv("PREMIUM_EMOJI", "").strip().lower()
_enabled: bool = _env_flag not in ("0", "false", "no", "off")


class PE:
    SETTINGS = "5870982283724328568"
    PROFILE = "5870994129244131212"
    PEOPLE = "5870772616305839506"
    PERSON_OK = "5891207662678317861"
    PERSON_NO = "5893192487324880883"
    FILE = "5870528606328852614"
    SMILE = "5870764288364252592"
    CHART_UP = "5870930636742595124"
    CHART = "5870921681735781843"
    HOME = "5873147866364514353"
    LOCK = "6037249452824072506"
    UNLOCK = "6037496202990194718"
    MEGAPHONE = "6039422865189638057"
    CHECK = "5870633910337015697"
    CROSS = "5870657884844462243"
    PENCIL = "5870676941614354370"
    TRASH = "5870875489362513438"
    DOWN = "5893057118545646106"
    CLIP = "6039451237743595514"
    LINK = "5769289093221454192"
    INFO = "6028435952299413210"
    BOT = "6030400221232501136"
    EYE = "6037397706505195857"
    EYE_OFF = "6037243349675544634"
    SEND = "5963103826075456248"
    DOWNLOAD = "6039802767931871481"
    BELL = "6039486778597970865"
    GIFT = "6032644646587338669"
    CLOCK = "5983150113483134607"
    PARTY = "6041731551845159060"
    WRITE = "5870753782874246579"
    PHOTO = "6035128606563241721"
    GEO = "6042011682497106307"
    CALENDAR = "5890937706803894250"
    TAG = "5886285355279193209"
    TIME_PAST = "5775896410780079073"
    BOX = "5884479287171485878"
    ADD_TEXT = "5771851822897566479"
    MONEY = "5904462880941545555"
    LOADING = "5345906554510012647"
    BACK = "5893057118545646106"


def is_premium_emoji_enabled() -> bool:
    return _enabled


def disable_premium_emoji() -> None:
    global _enabled
    _enabled = False


def fallback_char(emoji_id: str) -> str:
    return _FALLBACKS.get(emoji_id, "•")


def e(emoji_id: str, alt: str | None = None) -> str:
    """HTML premium-эмодзи или unicode, если premium отключён."""
    fb = alt if alt and alt != "\u200b" else fallback_char(emoji_id)
    if not _enabled:
        return fb
    return f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji>'


def strip_tg_emoji(text: str) -> str:
    """Убрать tg-emoji-теги, оставив читаемый unicode."""

    def repl(match: re.Match[str]) -> str:
        emoji_id, inner = match.group(1), match.group(2)
        if inner and inner != "\u200b":
            return inner
        return fallback_char(emoji_id)

    return _TG_EMOJI_RE.sub(repl, text)


def plain_button(btn: InlineKeyboardButton) -> InlineKeyboardButton:
    eid = btn.icon_custom_emoji_id
    if not eid:
        return btn
    fb = fallback_char(str(eid))
    data = btn.model_dump(exclude_none=True)
    data.pop("icon_custom_emoji_id", None)
    data["text"] = f"{fb} {data.get('text', '')}".strip()
    return InlineKeyboardButton(**data)


def plain_reply_markup(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [plain_button(b) for b in row]
            for row in markup.inline_keyboard
        ],
    )


def _apply_entity_fallback(
    text: str | None,
    kwargs: dict,
) -> tuple[str | None, dict]:
    disable_premium_emoji()
    out = dict(kwargs)
    if out.get("reply_markup") is not None:
        out["reply_markup"] = plain_reply_markup(out["reply_markup"])
    if text is not None:
        text = strip_tg_emoji(text)
    return text, out


def install_html_send_patch() -> None:
    """Повтор отправки без premium-эмодзи при ENTITY_TEXT_INVALID."""
    from aiogram import Bot
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import Message

    if getattr(Message, "_premium_patch", False):
        return

    log = __import__("logging").getLogger(__name__)
    _orig_answer = Message.answer
    _orig_edit = Message.edit_text
    _orig_bot_send = Bot.send_message

    def _invalid(err: BaseException) -> bool:
        return (
            isinstance(err, TelegramBadRequest)
            and "ENTITY_TEXT_INVALID" in str(err)
        )

    async def safe_answer(self, text: str = "", **kwargs):
        try:
            return await _orig_answer(self, text, **kwargs)
        except TelegramBadRequest as err:
            if not _invalid(err):
                raise
            log.warning("ENTITY_TEXT_INVALID → unicode emoji (answer)")
            text, kwargs = _apply_entity_fallback(text, kwargs)
            return await _orig_answer(self, text, **kwargs)

    async def safe_edit(self, text: str = "", **kwargs):
        try:
            return await _orig_edit(self, text, **kwargs)
        except TelegramBadRequest as err:
            if not _invalid(err):
                raise
            log.warning("ENTITY_TEXT_INVALID → unicode emoji (edit)")
            text, kwargs = _apply_entity_fallback(text, kwargs)
            return await _orig_edit(self, text, **kwargs)

    async def safe_bot_send(self, chat_id, text: str = "", **kwargs):
        try:
            return await _orig_bot_send(self, chat_id, text, **kwargs)
        except TelegramBadRequest as err:
            if not _invalid(err):
                raise
            log.warning("ENTITY_TEXT_INVALID → unicode emoji (send_message)")
            text, kwargs = _apply_entity_fallback(text, kwargs)
            return await _orig_bot_send(self, chat_id, text, **kwargs)

    Message.answer = safe_answer  # type: ignore[method-assign]
    Message.edit_text = safe_edit  # type: ignore[method-assign]
    Bot.send_message = safe_bot_send  # type: ignore[method-assign]
    Message._premium_patch = True


def ib(
    text: str,
    callback_data: str,
    emoji_id: str,
) -> InlineKeyboardButton:
    """Inline-кнопка: premium-иконка или unicode-префикс в тексте."""
    if _enabled:
        return InlineKeyboardButton(
            text=text,
            callback_data=callback_data,
            icon_custom_emoji_id=emoji_id,
        )
    return InlineKeyboardButton(
        text=f"{fallback_char(emoji_id)} {text}",
        callback_data=callback_data,
    )
