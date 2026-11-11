"""Планер (ialexrd.github.io/planer) через Firestore: коллекция planner_events.

Поля дела как у сайта планера: date (ГГГГ-ММ-ДД), s и e (начало и конец ЧЧ:ММ), title, meta (подробности),
type (sales, blog, work, life), d (сделано), deleted. Ключ сервисного аккаунта тот же, что у первого бота:
путь в GOOGLE_APPLICATION_CREDENTIALS. Если ключа нет, планер просто выключен.
"""
import asyncio
import os
import time

AREAS = {"sales": "клиенты и рассылки", "blog": "блог", "work": "работа и развитие", "life": "жизнь и спорт"}
COLLECTION = "planner_events"
_db = None


def enabled() -> bool:
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    return bool(path and os.path.exists(path))


def _client():
    global _db
    if _db is None:
        import firebase_admin
        from firebase_admin import firestore
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        _db = firestore.client()
    return _db


def _out(doc_id: str, x: dict) -> dict:
    return {"id": doc_id, "date": x.get("date", ""), "start": x.get("s", ""), "end": x.get("e", ""),
            "title": x.get("title", ""), "note": x.get("meta", ""), "area": x.get("type", ""),
            "done": bool(x.get("d"))}


def _between(start: str, end: str) -> list[dict]:
    from firebase_admin import firestore
    q = (_client().collection(COLLECTION)
         .where(filter=firestore.FieldFilter("date", ">=", start))
         .where(filter=firestore.FieldFilter("date", "<=", end)))
    res = [_out(d.id, d.to_dict()) for d in q.stream() if not d.to_dict().get("deleted")]
    return sorted(res, key=lambda t: (t["date"], t["start"] or "99:99"))


async def tasks_between(start: str, end: str) -> list[dict]:
    return await asyncio.to_thread(_between, start, end)


def _plus_hour(t: str) -> str:
    try:
        h, m = map(int, t.split(":"))
    except ValueError:
        return ""
    return f"{min(h + 1, 23):02d}:{m:02d}"


async def add_task(date: str, title: str, start: str = "", end: str = "", note: str = "", area: str = "") -> str:
    start = start or "09:00"
    data = {"date": date, "s": start, "e": end or _plus_hour(start), "title": title, "meta": note,
            "type": area if area in AREAS else "work", "d": False,
            "source": "jarvis", "createdAt": int(time.time() * 1000), "updatedAt": int(time.time() * 1000)}
    _, ref = await asyncio.to_thread(_client().collection(COLLECTION).add, data)
    return ref.id


async def update_task(task_id: str, **fields) -> bool:
    names = {"start": "s", "end": "e", "note": "meta", "area": "type", "done": "d"}
    data = {names.get(k, k): v for k, v in fields.items() if v not in (None, "")}
    ref = _client().collection(COLLECTION).document(task_id)
    snap = await asyncio.to_thread(ref.get)
    if not snap.exists:
        return False
    data["updatedAt"] = int(time.time() * 1000)
    await asyncio.to_thread(ref.update, data)
    return True


def format_day(tasks: list[dict]) -> str:
    if not tasks:
        return "в планере пусто"
    return "\n".join(f"{'✅' if t['done'] else '▫️'} {t['start']}–{t['end']} {t['title']}" for t in tasks)


# ---------- инструменты для DeepSeek (формат OpenAI) ----------

def _fn(name, description, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or list(props)}}}


S = {"type": "string"}
TOOLS = [
    _fn("list_tasks", "Дела в планере за диапазон дат (включительно).",
        {"date_from": {"type": "string", "description": "ГГГГ-ММ-ДД"}, "date_to": {"type": "string", "description": "ГГГГ-ММ-ДД"}}),
    _fn("add_task", "Добавить дело в планер. Если конец не назван, ставь на час позже начала.", {
        "date": {"type": "string", "description": "ГГГГ-ММ-ДД"}, "title": S,
        "start": {"type": "string", "description": "ЧЧ:ММ или пусто"}, "end": {"type": "string", "description": "ЧЧ:ММ или пусто"},
        "note": {"type": "string", "description": "подробности или пусто"},
        "area": {"type": "string", "enum": list(AREAS), "description": "направление"}}, ["date", "title"]),
    _fn("update_task", "Изменить дело: перенести, переименовать, отметить сделанным. Сначала найди его id через list_tasks.", {
        "task_id": S, "date": S, "start": S, "end": S, "title": S, "note": S,
        "done": {"type": "string", "enum": ["", "yes", "no"]}}, ["task_id"]),
    _fn("delete_task", "Удалить дело из планера.", {"task_id": S}),
]

PROMPT = ("\n\nУ тебя есть доступ к планеру владельца (инструменты list_tasks, add_task, update_task, delete_task). "
          "Направления: sales клиенты и рассылки, blog блог, work работа и развитие, life жизнь и спорт. "
          "Даты вида «завтра», «в четверг» переводи в ГГГГ-ММ-ДД от сегодняшней даты. Перед изменением дела найди его id. "
          "После изменений коротко скажи, что сделал, с датой в виде «чт 13.11».")


async def call_tool(name: str, a: dict):
    if name == "list_tasks":
        return await tasks_between(a.get("date_from", ""), a.get("date_to") or a.get("date_from", ""))
    if name == "add_task":
        tid = await add_task(a["date"], a["title"], a.get("start", ""), a.get("end", ""), a.get("note", ""), a.get("area", ""))
        return {"ok": True, "task_id": tid}
    if name == "update_task":
        done = {"yes": True, "no": False}.get(a.get("done") or "")
        ok = await update_task(a["task_id"], date=a.get("date"), start=a.get("start"), end=a.get("end"),
                               title=a.get("title"), note=a.get("note"), done=done)
        return {"ok": ok} if ok else {"error": "дело не найдено"}
    if name == "delete_task":
        ok = await update_task(a["task_id"], deleted=True)
        return {"ok": ok} if ok else {"error": "дело не найдено"}
    return {"error": f"нет инструмента {name}"}
