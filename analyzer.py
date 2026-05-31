"""
Анализатор сообщений: ищет задачи через regex + ключевые слова, извлекает дедлайны.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


def _now() -> datetime:
    return datetime.now(MOSCOW).replace(tzinfo=None)

TASK_PATTERNS = [
    r"(?:сделай|сделайте|сделать)\s+(.{5,80})",
    r"(?:напомни|напомните|напомнить)\s+(.{5,80})",
    r"(?:скинь|скиньте|скинуть|пришли|пришлите)\s+(.{5,80})",
    r"(?:не забудь|не забудьте)\s+(.{5,80})",
    r"(?:нужно|надо|необходимо)\s+(.{5,80})",
    r"(?:проверь|проверьте)\s+(.{5,80})",
    r"(?:позвони|позвоните)\s+(.{5,80})",
    r"(?:купи|купите|закажи|закажите)\s+(.{5,80})",
    r"(?:подготовь|подготовьте)\s+(.{5,80})",
    r"(?:отправь|отправьте)\s+(.{5,80})",
    r"(?:пришлю|скину|отправлю)\s+(.{3,60})",
    r"(?:сделаю|выполню|завершу)\s+(.{3,60})",
    r"(?:позвоню|напишу|отвечу)\s+(.{3,60})",
    r"(?:приеду|приду|встретимся)\s+(.{3,60})",
    r"(?:подготовлю|подготовить)\s+(.{3,60})",
    r"(?:куплю|закажу|оплачу)\s+(.{3,60})",
    r"(?:проверю|посмотрю|уточню)\s+(.{3,60})",
    r"(?:договорились|ок|окей|хорошо)[,\s]+(.{3,60})",
    r"(?:завтра|послезавтра|в понедельник|во вторник|в среду|в четверг|"
    r"в пятницу|в субботу|в воскресенье)\s+(.{3,60})",
    r"(.{3,60})\s+(?:завтра|послезавтра|в понедельник|во вторник|в среду|"
    r"в четверг|в пятницу|в субботу|в воскресенье)",
    r"(.{3,60})\s+(?:на следующей неделе|на этой неделе)",
    r"(.{3,60})\s+через\s+(?:\d+\s+)?(?:день|дня|дней|неделю|недели|час|часа|часов)",
    r"встреча\s+(.{3,60})",
    r"встретимся\s+(.{3,60})",
    r"можешь\s+(?:мне\s+)?(.{5,80})\?",
    r"можете\s+(?:мне\s+)?(.{5,80})\?",
    r"(?:задача|задание|todo|task|договорённость|договоренность|обещание)[:\s]+(.{5,80})",
]

DEADLINE_PATTERNS = {
    "завтра": lambda: (_now() + timedelta(days=1)).date().isoformat(),
    "послезавтра": lambda: (_now() + timedelta(days=2)).date().isoformat(),
    "в понедельник": lambda: _next_weekday(0),
    "во вторник": lambda: _next_weekday(1),
    "в среду": lambda: _next_weekday(2),
    "в четверг": lambda: _next_weekday(3),
    "в пятницу": lambda: _next_weekday(4),
    "в субботу": lambda: _next_weekday(5),
    "в воскресенье": lambda: _next_weekday(6),
    "на следующей неделе": lambda: (_now() + timedelta(weeks=1)).date().isoformat(),
}


def _next_weekday(weekday: int) -> str:
    today = _now()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (today + timedelta(days=days_ahead)).date().isoformat()


@dataclass
class AnalysisResult:
    is_task: bool = False
    task_text: str = ""
    deadline: str | None = None


def analyze_message(text: str) -> AnalysisResult:
    result = AnalysisResult()
    low = text.lower().strip()
    deadline = _extract_deadline(low)

    for pattern in TASK_PATTERNS:
        m = re.search(pattern, low, re.IGNORECASE)
        if m:
            result.is_task = True
            result.task_text = _clean(m.group(1) if m.lastindex else text)
            break

    if result.is_task or low.startswith(
        ("задача ", "задачу ", "task ", "договорённость ", "договоренность ", "обещание ", "deal ")
    ):
        result.is_task = True
        if not result.task_text:
            for prefix in ("задача ", "задачу ", "task ", "договорённость ", "договоренность ", "обещание ", "deal "):
                if low.startswith(prefix):
                    result.task_text = _clean(text.split(" ", 1)[1])
                    break
            if not result.task_text:
                result.task_text = _clean(text)
        result.deadline = deadline
    elif deadline:
        # «завтра ночевка», «в пятницу встреча» и т.п.
        result.is_task = True
        result.task_text = _clean(text)
        result.deadline = deadline

    return result


def _extract_deadline(text: str) -> str | None:
    for keyword, fn in DEADLINE_PATTERNS.items():
        if keyword in text:
            return fn()

    m = re.search(r"до\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        day, month, year = map(int, m.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    m = re.search(r"до\s+(\d{1,2})\.(\d{1,2})\.(\d{2})", text)
    if m:
        day, month, year = map(int, m.groups())
        year += 2000
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    m = re.search(r"через\s+(\d+)\s+(день|дня|дней)", text)
    if m:
        return (_now() + timedelta(days=int(m.group(1)))).date().isoformat()

    m = re.search(r"через\s+(\d+)\s+(час|часа|часов)", text)
    if m:
        return (_now() + timedelta(hours=int(m.group(1)))).date().isoformat()

    return None


def _clean(text: str) -> str:
    text = text.strip(" .,!?;:")
    text = re.sub(r"\s+", " ", text)
    return text[:200]
