"""
Видео в уроках: своя загрузка (файл хранится рядом с БД, отдаётся сайтом
напрямую без прямой скачиваемой ссылки в интерфейсе) или встроенное видео
с YouTube (youtube-nocookie.com, повышенная приватность).

Честно: полностью запретить скачивание видео из браузера невозможно
(это ограничение любого веб-плеера, не только нашего). Отключаем то,
что реально можно — кнопку скачивания в плеере, контекстное меню,
показ прямой ссылки в интерфейсе.
"""
import re
import uuid
from pathlib import Path
from typing import Optional

import config

_YOUTUBE_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def extract_youtube_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = _YOUTUBE_RE.search(url.strip())
    return m.group(1) if m else None


def upload_dir() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_uploaded_video(data: bytes, original_filename: str) -> str:
    ext = Path(original_filename or "").suffix or ".mp4"
    new_name = f"{uuid.uuid4().hex}{ext}"
    (upload_dir() / new_name).write_bytes(data)
    return f"/uploads/videos/{new_name}"
