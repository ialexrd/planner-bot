"""Интернет для агента: поиск через Yandex Search API и чтение страниц по ссылке.

Поиск: YANDEX_SEARCH_API_KEY (сервисный аккаунт с ролью search-api.webSearch.user) и YANDEX_FOLDER_ID.
Если отдельного ключа нет, пробуем YANDEX_API_KEY. Чтение страниц работает без ключей.
"""
import base64
import html
import os
import re
import xml.etree.ElementTree as ET

import httpx

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _key() -> str:
    return os.getenv("YANDEX_SEARCH_API_KEY") or os.getenv("YANDEX_API_KEY") or ""


def search_enabled() -> bool:
    return bool(_key() and os.getenv("YANDEX_FOLDER_ID"))


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


async def search(query: str, limit: int = 6) -> list[dict]:
    body = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(limit), "docsInGroup": "1"},
        "maxPassages": "3",
        "l10n": "LOCALIZATION_RU",
        "folderId": os.environ["YANDEX_FOLDER_ID"],
        "responseFormat": "FORMAT_XML",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(SEARCH_URL, headers={"Authorization": f"Api-Key {_key()}"}, json=body)
        if r.status_code in (401, 403):
            raise RuntimeError("Яндекс не пустил в поиск: проверьте ключ и роль search-api.webSearch.user")
        r.raise_for_status()
    root = ET.fromstring(base64.b64decode(r.json()["rawData"]))
    err = root.find(".//error")
    if err is not None:
        return [] if err.get("code") == "15" else [{"error": _text(err)}]  # 15 = ничего не найдено
    out = []
    for doc in root.iter("doc"):
        out.append({"title": _text(doc.find("title")), "url": _text(doc.find("url")),
                    "snippet": " ".join(_text(p) for p in doc.iter("passage")) or _text(doc.find("headline"))})
    return out[:limit]


async def read_page(url: str, limit: int = 6000) -> dict:
    """Текст страницы без разметки, обрезанный до limit символов."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return {"url": str(r.url), "error": f"это не страница, а {ctype or 'файл'}"}
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", r.text)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text).strip()
    return {"url": str(r.url), "title": html.unescape(title.group(1).strip()) if title else "",
            "text": text[:limit], "cut": len(text) > limit}


def _fn(name, description, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or list(props)}}}


SEARCH_TOOL = _fn("web_search", "Поиск в интернете через Яндекс. Возвращает заголовки, ссылки и фрагменты.",
                  {"query": {"type": "string", "description": "поисковый запрос"}})
READ_TOOL = _fn("read_page", "Открыть страницу по ссылке и прочитать её текст.", {"url": {"type": "string"}})

PROMPT = ("\n\nУ тебя есть интернет: web_search ищет в Яндексе, read_page открывает ссылку. Пользуйся ими, когда "
          "нужны свежие факты, цены, новости, расписания или содержимое ссылки. Если нужен ответ по делу, а фрагментов "
          "мало, открой 1–2 лучших ссылки. В ответе коротко укажи источники ссылками.")


def tools() -> list[dict]:
    return ([SEARCH_TOOL] if search_enabled() else []) + [READ_TOOL]


async def call_tool(name: str, a: dict):
    if name == "web_search":
        return await search(a["query"])
    if name == "read_page":
        return await read_page(a["url"])
    return None
