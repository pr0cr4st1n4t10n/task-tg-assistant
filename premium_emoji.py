"""Premium custom emoji IDs для Telegram (Bot API 7+)."""
from aiogram.types import InlineKeyboardButton


class PE:
  # Из списка пользователя
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
    BACK = "5893057118545646106"  # навигация «назад»


def e(emoji_id: str, alt: str = "\u200b") -> str:
    """HTML-фрагмент premium-эмодзи для текста сообщения."""
    return f'<tg-emoji emoji-id="{emoji_id}">{alt}</tg-emoji>'


def ib(
    text: str,
    callback_data: str,
    emoji_id: str,
) -> InlineKeyboardButton:
    """Inline-кнопка только с premium-иконкой (без unicode-эмодзи в text)."""
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=emoji_id,
    )
