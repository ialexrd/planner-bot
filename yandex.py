"""Яндекс Cloud: голос → текст (SpeechKit) и картинка → текст (Vision OCR).

Ключи те же, что у первого бота: YANDEX_API_KEY (роли ai.speechkit-stt.user и ai.vision.user)
и YANDEX_FOLDER_ID.
"""
import asyncio
import base64
import os
import tempfile
from pathlib import Path

import httpx

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
OCR_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
CHUNK_SEC = 28  # SpeechKit v1 принимает до 30 секунд за раз


def _key() -> str:
    key = os.getenv("YANDEX_API_KEY")
    if not key:
        raise RuntimeError("YANDEX_API_KEY не задан в .env")
    return key


def _folder() -> str:
    folder = os.getenv("YANDEX_FOLDER_ID")
    if not folder:
        raise RuntimeError("YANDEX_FOLDER_ID не задан в .env")
    return folder


def enabled() -> bool:
    return bool(os.getenv("YANDEX_API_KEY") and os.getenv("YANDEX_FOLDER_ID"))


async def _recognize_chunk(client: httpx.AsyncClient, audio: bytes) -> str:
    r = await client.post(
        STT_URL,
        params={"folderId": _folder(), "lang": "ru-RU", "format": "oggopus"},
        headers={"Authorization": f"Api-Key {_key()}"},
        content=audio,
    )
    r.raise_for_status()
    return r.json().get("result", "").strip()


async def _split(audio: bytes) -> list[bytes]:
    """Режет голосовое на куски по CHUNK_SEC секунд через ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.ogg"
        src.write_bytes(audio)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(src),
            "-f", "segment", "-segment_time", str(CHUNK_SEC),
            "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k",
            str(Path(tmp) / "part%03d.ogg"),
        )
        if await proc.wait() != 0:
            raise RuntimeError("ffmpeg не смог разрезать голосовое")
        return [p.read_bytes() for p in sorted(Path(tmp).glob("part*.ogg"))]


async def speech_to_text(audio: bytes, duration: int) -> str:
    """Голосовое Telegram (ogg/opus) → текст."""
    parts = [audio] if duration <= CHUNK_SEC else await _split(audio)
    async with httpx.AsyncClient(timeout=60) as client:
        texts = [await _recognize_chunk(client, p) for p in parts]
    return " ".join(t for t in texts if t).strip()


async def image_to_text(image: bytes, mime: str = "JPEG") -> str:
    """Скриншот или фото → распознанный текст."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            OCR_URL,
            headers={"Authorization": f"Api-Key {_key()}", "x-folder-id": _folder()},
            json={
                "mimeType": mime,
                "languageCodes": ["ru", "en"],
                "model": "page",
                "content": base64.b64encode(image).decode(),
            },
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("textAnnotation", {}).get("fullText", "").strip()
