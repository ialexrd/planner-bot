"""Планерный бот: утром идея из книги + мотивация, вечером выжимка, чат с DeepSeek.

Версия для сервера 94.250.248.170, где уже работает assistant-bot:
- короткий опрос Telegram (TELEGRAM_POLL_TIMEOUT=2), иначе сообщения теряются в туннеле;
- DeepSeek через httpx, как у первого бота (без пакета openai);
- «печатает…» держится, пока DeepSeek думает;
- отдельное напоминание про шею по расписанию (NECK_TIME);
- файл состояния защищён от одновременной записи.
"""
import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

import planner
import yandex

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("planner-bot")

DATA = Path(os.getenv("DATA_DIR") or BASE / "data")
if not DATA.is_absolute():
    DATA = BASE / DATA
DATA.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / "state.json"

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
TZ = os.getenv("TZ_NAME", "Asia/Yekaterinburg")
MORNING = os.getenv("MORNING_TIME", "09:30")
EVENING = os.getenv("EVENING_TIME", "21:00")
NECK = os.getenv("NECK_TIME", "22:30")  # пусто = без отдельного напоминания
POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT") or 2)
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

SYSTEM = (
    "Ты личный ассистент Алексея в стиле Джарвиса: вежливый, собранный, слегка ироничный, "
    "пишешь по-русски, коротко и по делу. Помогаешь держать фокус на целях и не распыляться. "
    "Никогда не выдумывай точные цитаты: если не уверен в дословной формулировке, пересказывай "
    "мысль своими словами и так и пиши «мысль из книги», а не «цитата». "
    "Telegram-разметка: только *жирный* и _курсив_, без заголовков #.\n\n"
    "Контекст о пользователе:\n"
)

HISTORY_LIMIT = 12
state_lock = asyncio.Lock()


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("book_index", 0)
    state.setdefault("used_ideas", [])
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read_text(name: str) -> str:
    p = BASE / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def books() -> list[str]:
    return [l.strip() for l in read_text("books.txt").splitlines() if l.strip() and not l.startswith("#")]


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def today() -> dt.date:
    return dt.datetime.now(ZoneInfo(TZ)).date()


def system_prompt(tools: bool = False) -> str:
    now = dt.datetime.now(ZoneInfo(TZ))
    head = f"Сейчас {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}.\n\n"
    return head + SYSTEM + read_text("context.md") + (planner.PROMPT if tools else "")


async def _complete(client: httpx.AsyncClient, messages: list[dict], temperature: float, tools: list | None):
    body = {"model": MODEL, "messages": messages, "temperature": temperature, "max_tokens": 900}
    if tools:
        body["tools"] = tools
    r = await client.post(f"{DEEPSEEK_URL}/chat/completions",
                          headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"}, json=body)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


async def ask(messages: list[dict], temperature: float = 0.8, tools: bool = False) -> str:
    """Запрос к DeepSeek. С tools=True модель может работать с планером (до 8 шагов)."""
    use_tools = tools and planner.enabled()
    msgs = [{"role": "system", "content": system_prompt(use_tools)}, *messages]
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(8):
            msg = await _complete(client, msgs, temperature, planner.TOOLS if use_tools else None)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip()
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    result = await planner.call_tool(c["function"]["name"], args)
                except Exception as ex:  # ошибка инструмента уходит модели, пусть объяснит
                    log.exception("Инструмент %s упал", c["function"]["name"])
                    result = {"error": str(ex)}
                msgs.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
        return "Не успел закончить: слишком много шагов. Уточните, пожалуйста, задачу."


async def plan_today() -> list[dict]:
    if not planner.enabled():
        return []
    try:
        d = today().isoformat()
        return await planner.tasks_between(d, d)
    except Exception:
        log.exception("Не удалось прочитать планер")
        return []


@contextlib.asynccontextmanager
async def typing(bot: Bot, chat_id: int):
    """Держит статус «печатает…», пока DeepSeek думает (бывает до минуты)."""
    async def loop():
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


async def morning_text() -> str:
    async with state_lock:
        state = load_state()
        lst = books() or ["любая сильная книга о продуктивности"]
        book = lst[state["book_index"] % len(lst)]
        state["book_index"] += 1
        used = "; ".join(state["used_ideas"][-30:]) or "пока нет"
        save_state(state)
    text = await ask([{
        "role": "user",
        "content": (
            f"Утреннее сообщение. Книга: {book}.\n"
            "1) Одна ключевая идея из книги (3–5 предложений), "
            "2) как применить её сегодня именно мне, одним конкретным действием, "
            "3) короткая мотивация на день, привязанная к моим целям (2–3 предложения).\n"
            f"Не повторяй идеи, которые уже были: {used}.\n"
            "В последней строке напиши служебно: ИДЕЯ: <идея в 5–8 словах>."
        ),
    }])
    lines = text.splitlines()
    if lines and lines[-1].strip().startswith("ИДЕЯ:"):
        idea = lines[-1].strip()[5:].strip()
        text = "\n".join(lines[:-1]).strip()
        async with state_lock:
            state = load_state()
            state["used_ideas"].append(f"{book}: {idea}")
            state["used_ideas"] = state["used_ideas"][-100:]
            save_state(state)
    plan = await plan_today()
    tail = f"\n\n🗓 *План на сегодня*\n{planner.format_day(plan)}" if plan else ""
    return f"☀️ *Доброе утро*\n_{book}_\n\n{text}{tail}"


async def evening_text() -> str:
    state = load_state()
    last = state["used_ideas"][-1] if state["used_ideas"] else "идея дня"
    neck = f"3) напомни, что в {NECK} ОФП шеи 15 минут." if NECK else ""
    plan = await plan_today()
    undone = [t["title"] for t in plan if not t["done"]]
    if plan:
        neck += (f" 4) Из планера сегодня не отмечены: {'; '.join(undone)}. Мягко спроси, что успел, "
                 "и предложи перенести остальное на завтра (я могу ответить, и ты перенесёшь)." if undone
                 else " 4) Все дела дня в планере отмечены, коротко похвали.")
    text = await ask([{
        "role": "user",
        "content": (
            f"Вечернее сообщение. Утром была идея: {last}.\n"
            "1) Короткая выжимка-продолжение этой мысли (3–4 предложения), "
            "2) один вопрос для размышления о прошедшем дне" + (", " + neck if neck else ".")
        ),
    }])
    return f"🌙 *Итоги дня*\n\n{text}"


def is_owner(msg: Message) -> bool:
    return bool(OWNER_ID and msg.from_user and msg.from_user.id == OWNER_ID)


dp = Dispatcher()


@dp.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"Твой Telegram ID: `{msg.from_user.id}`", parse_mode="Markdown")


@dp.message(~F.func(is_owner))
async def not_owner(msg: Message):
    log.info("Отклонено сообщение от %s", msg.from_user.id if msg.from_user else "?")


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    await msg.answer(
        "Я на связи, сэр.\n\n"
        f"Каждый день в {MORNING} пришлю идею из книги и мотивацию, в {EVENING} короткий итог.\n\n"
        "/idea идея прямо сейчас\n/evening вечернее сообщение сейчас\n"
        "/books список книг\n/reset очистить историю разговора\n\n"
        "Остальное пиши текстом, голосовым или присылай скриншот (подпись к нему = что сделать)."
    )


async def send_safe(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception:  # кривая разметка от модели: шлём как есть
        await bot.send_message(chat_id, text)


async def reply_llm(msg: Message, bot: Bot, make):
    try:
        async with typing(bot, msg.chat.id):
            text = await make()
    except Exception:
        log.exception("DeepSeek не ответил")
        await msg.answer("DeepSeek сейчас не отвечает, попробуйте через минуту.")
        return
    await send_safe(bot, msg.chat.id, text)


@dp.message(Command("idea"))
async def cmd_idea(msg: Message, bot: Bot):
    await reply_llm(msg, bot, morning_text)


@dp.message(Command("evening"))
async def cmd_evening(msg: Message, bot: Bot):
    await reply_llm(msg, bot, evening_text)


@dp.message(Command("books"))
async def cmd_books(msg: Message):
    await msg.answer("\n".join(f"{i+1}. {b}" for i, b in enumerate(books())) or "Список пуст")


@dp.message(Command("reset"))
async def cmd_reset(msg: Message):
    async with state_lock:
        state = load_state()
        state["history"] = []
        save_state(state)
    await msg.answer("История разговора очищена.")


async def handle_text(msg: Message, bot: Bot, text: str):
    history = load_state()["history"][-HISTORY_LIMIT:]
    history.append({"role": "user", "content": text})

    async def make():
        answer = await ask(history, temperature=0.6, tools=True)
        async with state_lock:
            state = load_state()
            state["history"] = (history + [{"role": "assistant", "content": answer}])[-HISTORY_LIMIT:]
            save_state(state)
        return answer

    await reply_llm(msg, bot, make)


@dp.message(F.text)
async def chat(msg: Message, bot: Bot):
    await handle_text(msg, bot, msg.text)


@dp.message(F.voice | F.audio)
async def voice(msg: Message, bot: Bot):
    if not yandex.enabled():
        await msg.answer("Голос пока не подключён: в .env нет ключей Яндекса.")
        return
    media = msg.voice or msg.audio
    try:
        async with typing(bot, msg.chat.id):
            audio = (await bot.download(media)).read()
            text = await yandex.speech_to_text(audio, media.duration or 0)
    except Exception:
        log.exception("Не удалось распознать голосовое")
        await msg.answer("Не получилось распознать голосовое, попробуйте ещё раз или напишите текстом.")
        return
    if not text:
        await msg.answer("Ничего не расслышал, повторите, пожалуйста.")
        return
    await msg.answer(f"🎙 {text[:3500]}")
    await handle_text(msg, bot, text)


@dp.message(F.photo | (F.document & F.document.mime_type.in_({"image/jpeg", "image/png"})))
async def picture(msg: Message, bot: Bot):
    if not yandex.enabled():
        await msg.answer("Скриншоты пока не подключены: в .env нет ключей Яндекса.")
        return
    if msg.photo:
        media, mime = msg.photo[-1], "JPEG"
    else:
        media, mime = msg.document, "PNG" if msg.document.mime_type == "image/png" else "JPEG"
    try:
        async with typing(bot, msg.chat.id):
            image = (await bot.download(media)).read()
            text = await yandex.image_to_text(image, mime)
    except Exception:
        log.exception("Не удалось распознать картинку")
        await msg.answer("Не получилось прочитать картинку, попробуйте ещё раз.")
        return
    if not text:
        await msg.answer("На картинке не нашёл текста. Опишите словами, что на ней.")
        return
    task = msg.caption or "Разбери, что здесь, и скажи коротко, что мне с этим делать."
    await handle_text(msg, bot, f"Я прислал скриншот. Распознанный текст:\n\n{text[:6000]}\n\nМоя просьба: {task}")


async def scheduled(bot: Bot, kind: str):
    if not OWNER_ID:
        log.warning("OWNER_ID не задан, рассылка %s пропущена", kind)
        return
    try:
        if kind == "neck":
            text = "🦒 Время ОФП шеи: 15 минут. Потом можно отдыхать."
        else:
            text = await (morning_text() if kind == "morning" else evening_text())
        await send_safe(bot, OWNER_ID, text)
    except Exception:
        log.exception("Не удалось отправить %s", kind)


async def main():
    bot = Bot(TOKEN)
    sched = AsyncIOScheduler(timezone=TZ)
    jobs = [("morning", MORNING), ("evening", EVENING)] + ([("neck", NECK)] if NECK else [])
    for kind, hm in jobs:
        h, m = map(int, hm.split(":"))
        sched.add_job(scheduled, "cron", hour=h, minute=m, args=[bot, kind], misfire_grace_time=600)
    sched.start()
    log.info("Бот запущен. Утро %s, вечер %s, шея %s (%s). OWNER_ID=%s",
             MORNING, EVENING, NECK or "выкл", TZ, OWNER_ID or "не задан")
    await dp.start_polling(bot, polling_timeout=POLL_TIMEOUT)


if __name__ == "__main__":
    asyncio.run(main())
