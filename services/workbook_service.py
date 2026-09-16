"""
Рабочая тетрадь урока — отдельный файл, который ученик скачивает себе.

Это не конспект внутри платформы: конспект читают на экране, а тетрадь
забирают на устройство и печатают. У каждого урока может быть свой файл,
админ загружает его в панели и в любой момент меняет или удаляет.

Если файла нет, кнопка ученику вообще не показывается.
"""
import uuid
from pathlib import Path
from typing import Optional

import config
import database as db

# Что разрешаем загружать. PDF — обязательный минимум, остальное для удобства.
ALLOWED_EXT = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt",
               ".pptx", ".ppt", ".xlsx", ".xls", ".zip"}
MAX_BYTES = 50 * 1024 * 1024        # 50 МБ

_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".txt": "text/plain; charset=utf-8",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".zip": "application/zip",
}


def upload_dir() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "workbooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mime_for(filename: str) -> str:
    return _MIME.get(Path(filename).suffix.lower(), "application/octet-stream")


def ensure_columns() -> None:
    """Колонки добавляет database.py; подстраховка на случай старой базы."""
    for sql in (
        "ALTER TABLE lessons ADD COLUMN workbook_path TEXT",
        "ALTER TABLE lessons ADD COLUMN workbook_name TEXT",
        "ALTER TABLE lessons ADD COLUMN workbook_size INTEGER DEFAULT 0",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass


def get(lesson_id: int) -> Optional[dict]:
    """Тетрадь урока или None, если её не прикрепляли.

    У копии-ярлыка тетрадь берётся с оригинала — как конспект и тест.
    """
    try:
        row = db.fetchone(
            "SELECT id, original_id, workbook_path, workbook_name, workbook_size "
            "FROM lessons WHERE id=?", (lesson_id,))
    except Exception:
        return None
    if not row:
        return None
    if not (row["workbook_path"] or "").strip() and row["original_id"]:
        try:
            row = db.fetchone(
                "SELECT id, original_id, workbook_path, workbook_name, workbook_size "
                "FROM lessons WHERE id=?", (row["original_id"],))
        except Exception:
            return None
    if not row or not (row["workbook_path"] or "").strip():
        return None
    path = upload_dir() / row["workbook_path"]
    if not path.exists():
        return None                 # запись есть, а файла нет — кнопку не рисуем
    return {
        "lesson_id": row["id"],
        "file": row["workbook_path"],
        "name": row["workbook_name"] or "Рабочая тетрадь",
        "size": row["workbook_size"] or 0,
        "url": f"/learn/lesson/{lesson_id}/workbook",
    }


def has_workbook(lesson_id: int) -> bool:
    return get(lesson_id) is not None


def save(lesson_id: int, data: bytes, original_filename: str) -> tuple:
    """Прикрепить файл к уроку. Возвращает (получилось, сообщение)."""
    ensure_columns()
    ext = Path(original_filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return False, ("Такой формат не поддерживается. Подойдут PDF, DOCX, "
                       "PPTX, XLSX, TXT или ZIP.")
    if not data:
        return False, "Файл пустой."
    if len(data) > MAX_BYTES:
        mb = round(len(data) / 1024 / 1024, 1)
        return False, (f"Файл {mb} МБ — слишком большой. "
                       f"Максимум {MAX_BYTES // 1024 // 1024} МБ.")

    delete(lesson_id, keep_record=True)      # старый файл больше не нужен
    new_name = f"{uuid.uuid4().hex}{ext}"
    (upload_dir() / new_name).write_bytes(data)
    nice = Path(original_filename).name[:200] or f"Рабочая тетрадь{ext}"
    db.execute(
        "UPDATE lessons SET workbook_path=?, workbook_name=?, workbook_size=? "
        "WHERE id=?", (new_name, nice, len(data), lesson_id))
    return True, f"Рабочая тетрадь «{nice}» прикреплена."


def delete(lesson_id: int, keep_record: bool = False) -> None:
    """Убрать файл. keep_record — когда сразу кладём новый вместо старого."""
    try:
        row = db.fetchone("SELECT workbook_path FROM lessons WHERE id=?", (lesson_id,))
    except Exception:
        return
    if row and (row["workbook_path"] or "").strip():
        try:
            (upload_dir() / row["workbook_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    if not keep_record:
        try:
            db.execute(
                "UPDATE lessons SET workbook_path=NULL, workbook_name=NULL, "
                "workbook_size=0 WHERE id=?", (lesson_id,))
        except Exception:
            pass


def size_label(size: int) -> str:
    if not size:
        return ""
    if size < 1024 * 1024:
        return f"{round(size / 1024)} КБ"
    return f"{round(size / 1024 / 1024, 1)} МБ"
