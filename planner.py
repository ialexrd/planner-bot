"""Планер (ialexrd.github.io/planer) через его Firestore (проект planer-da5eb).

Как планер хранит данные: документ users/{uid}, поле kv — копия localStorage браузера:
- базовые дела зашиты прямо в index.html (массив BASE), их id = "k" + дата|начало|название;
- kv["planner-ov"]     — JSON: правки базовых дел {id: {date, s, e, title, meta, type, deleted}};
- kv["planner-custom"] — JSON: список своих дел [{id, date, s, e, title, meta, type, d}];
- kv["ev-<id>"]        — "1"/"0": отметка «сделано».
Бот читает BASE из опубликованного index.html, накладывает правки и пишет в те же ключи.

Ключ сервисного аккаунта проекта планера: путь в PLANER_CREDENTIALS. Без ключа планер выключен.
"""
import asyncio
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("planner")

AREAS = {"sales": "клиенты и рассылки", "blog": "блог", "work": "работа и развитие", "life": "жизнь и спорт"}
HTML_URLS = ["https://raw.githubusercontent.com/ialexrd/planer/main/index.html", "https://ialexrd.github.io/planer/"]
ROW = re.compile(r"\['(\d{4}-\d\d-\d\d)','(\d\d:\d\d)','(\d\d:\d\d)','([^']*)','([^']*)','(\w+)'(?:,\s*(\w+))?\]")

_db = None
_ref = None
_base: tuple[float, list[dict]] = (0.0, [])
_lock = asyncio.Lock()


def enabled() -> bool:
    path = os.getenv("PLANER_CREDENTIALS")
    return bool(path and os.path.exists(path))


def _doc_ref():
    """users/{uid} пользователя планера. uid из PLANER_USER_ID или единственный документ в users."""
    global _db, _ref
    if _ref is None:
        import firebase_admin
        from firebase_admin import credentials, firestore
        try:
            app = firebase_admin.get_app("planer")
        except ValueError:
            app = firebase_admin.initialize_app(credentials.Certificate(os.environ["PLANER_CREDENTIALS"]), name="planer")
        _db = firestore.client(app)
        uid = os.getenv("PLANER_USER_ID")
        if not uid:
            docs = list(_db.collection("users").limit(2).stream())
            if not docs:
                return None  # на сайте ещё никто не входил: дела видны только из index.html
            if len(docs) > 1:
                log.warning("В users несколько документов, беру первый. Укажите PLANER_USER_ID в .env")
            uid = docs[0].id
        _ref = _db.collection("users").document(uid)
    return _ref


async def _load_base() -> list[dict]:
    """Базовые дела из опубликованного планера, кэш на 10 минут."""
    global _base
    if time.time() - _base[0] < 600 and _base[1]:
        return _base[1]
    last_error = None
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        for url in HTML_URLS:
            try:
                r = await c.get(url)
                r.raise_for_status()
                rows = [{"id": f"k{m[0]}|{m[1]}|{m[3]}", "date": m[0], "s": m[1], "e": m[2], "title": m[3],
                         "meta": m[4], "type": m[5], "d": (m[6] or "") in ("1", "true")}
                        for m in ROW.findall(r.text)]
                if rows:
                    _base = (time.time(), rows)
                    return rows
            except httpx.HTTPError as ex:
                last_error = ex
    if _base[1]:
        return _base[1]
    raise RuntimeError(f"Не удалось скачать планер: {last_error}")


async def _read_kv() -> dict:
    ref = await asyncio.to_thread(_doc_ref)
    if ref is None:
        return {}
    snap = await asyncio.to_thread(ref.get)
    return (snap.to_dict() or {}).get("kv", {}) if snap.exists else {}


async def _write_kv(kv: dict) -> None:
    ref = await asyncio.to_thread(_doc_ref)
    if ref is None:
        raise RuntimeError("Владелец ещё не входил в планер на сайте через Google, сохранить изменения некуда. "
                           "Попроси его открыть ialexrd.github.io/planer и войти.")
    await asyncio.to_thread(ref.set, {"kv": kv, "updatedAt": int(time.time() * 1000)}, merge=True)


def _json(kv: dict, key: str, default):
    try:
        v = json.loads(kv.get(key) or "null")
        return default if v is None else v
    except ValueError:
        return default


async def _all() -> tuple[dict, list[dict]]:
    kv = await _read_kv()
    ov = _json(kv, "planner-ov", {})
    custom = _json(kv, "planner-custom", [])
    events = [{**e, **ov.get(e["id"], {})} for e in await _load_base()] + list(custom)
    for e in events:
        flag = kv.get("ev-" + e["id"])
        e["d"] = flag == "1" if flag is not None else bool(e.get("d"))
    return kv, [e for e in events if not e.get("deleted")]


def _out(e: dict) -> dict:
    return {"id": e["id"], "date": e.get("date", ""), "start": e.get("s", ""), "end": e.get("e", ""),
            "title": e.get("title", ""), "note": e.get("meta", ""), "area": e.get("type", ""), "done": bool(e.get("d"))}


async def tasks_between(start: str, end: str) -> list[dict]:
    _, events = await _all()
    res = [_out(e) for e in events if start <= e.get("date", "") <= end]
    return sorted(res, key=lambda t: (t["date"], t["start"]))


def _plus_hour(t: str) -> str:
    try:
        h, m = map(int, t.split(":"))
    except ValueError:
        return "10:00"
    return f"{min(h + 1, 23):02d}:{m:02d}"


async def add_task(date: str, title: str, start: str = "", end: str = "", note: str = "", area: str = "") -> str:
    start = start or "09:00"
    async with _lock:
        kv = await _read_kv()
        custom = _json(kv, "planner-custom", [])
        tid = f"j{int(time.time() * 1000)}"
        custom.append({"id": tid, "d": False, "date": date, "s": start, "e": end or _plus_hour(start),
                       "title": title, "meta": note, "type": area if area in AREAS else "work"})
        await _write_kv({"planner-custom": json.dumps(custom, ensure_ascii=False)})
    return tid


async def update_task(task_id: str, done: bool | None = None, deleted: bool = False, **fields) -> bool:
    names = {"start": "s", "end": "e", "note": "meta", "area": "type"}
    patch = {names.get(k, k): v for k, v in fields.items() if v}
    if deleted:
        patch["deleted"] = True
    async with _lock:
        kv, events = await _all()
        if not any(e["id"] == task_id for e in events):
            return False
        out = {}
        if done is not None:
            out["ev-" + task_id] = "1" if done else "0"
        if patch:
            custom = _json(kv, "planner-custom", [])
            mine = next((c for c in custom if c.get("id") == task_id), None)
            if mine is not None:
                if deleted:
                    custom = [c for c in custom if c.get("id") != task_id]
                else:
                    mine.update(patch)
                out["planner-custom"] = json.dumps(custom, ensure_ascii=False)
            else:
                ov = _json(kv, "planner-ov", {})
                ov[task_id] = {**ov.get(task_id, {}), **patch}
                out["planner-ov"] = json.dumps(ov, ensure_ascii=False)
        if out:
            await _write_kv(out)
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
        ok = await update_task(a["task_id"], done=done, date=a.get("date"), start=a.get("start"),
                               end=a.get("end"), title=a.get("title"), note=a.get("note"))
        return {"ok": ok} if ok else {"error": "дело не найдено"}
    if name == "delete_task":
        ok = await update_task(a["task_id"], deleted=True)
        return {"ok": ok} if ok else {"error": "дело не найдено"}
    return {"error": f"нет инструмента {name}"}
