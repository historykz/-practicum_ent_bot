"""
Хранилище страниц конспекта в Telegram.

Страница конспекта — это либо файл на диске сервера (как раньше), либо
file_id в Telegram (новый способ). Во втором случае на сервере не занимается
ни байта: в базе лежит только привязка к уроку, порядок и file_id.

Порядок страниц определяется message_id: Telegram нумерует сообщения строго
по возрастанию, поэтому пачка из 30 фото сохранится ровно в том порядке,
в каком её отправили, даже если бот обработает сообщения вразнобой.
"""
import logging
from typing import Optional

import database as db

log = logging.getLogger(__name__)

TG = "telegram"
DISK = "disk"


def add_telegram_page(lesson_id: int, file_id: str, unique_id: str = "",
                      message_id: int = 0, as_document: bool = False,
                      file_name: str = "", added_by: int = 0) -> int:
    """Добавить страницу конспекта, лежащую в Telegram.

    sort_order = message_id: порядок отправки сохраняется сам собой.
    Повторную отправку того же файла игнорируем — защита от дублей, когда
    Telegram переприсылает апдейт.
    """
    if unique_id:
        dup = db.fetchone(
            "SELECT id FROM lesson_images WHERE lesson_id=? AND file_unique_id=?",
            (lesson_id, unique_id))
        if dup:
            return dup["id"]
    db.execute(
        "INSERT INTO lesson_images (lesson_id, image_path, sort_order, file_id, "
        "file_unique_id, storage, as_document, file_name, added_by) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (lesson_id, "", message_id or 0, file_id, unique_id, TG,
         1 if as_document else 0, file_name or "", added_by or 0))
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def renumber(lesson_id: int) -> int:
    """Пронумеровать страницы 1..N по текущему порядку. Вызывать после
    приёмки пачки — тогда message_id превращаются в аккуратные 1,2,3…"""
    rows = db.fetchall(
        "SELECT id FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
        (lesson_id,))
    for i, r in enumerate(rows, 1):
        db.execute("UPDATE lesson_images SET sort_order=? WHERE id=?", (i, r["id"]))
    return len(rows)


def pages(lesson_id: int) -> list:
    rows = db.fetchall(
        "SELECT * FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
        (lesson_id,))
    return [dict(r) for r in rows]


def page_count(lesson_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM lesson_images WHERE lesson_id=?", (lesson_id,))
    return (row["c"] if row else 0) or 0


def clear(lesson_id: int) -> int:
    """Удалить все страницы конспекта. Файлы с диска тоже подчищаем."""
    rows = pages(lesson_id)
    removed = 0
    for r in rows:
        _remove_disk_file(r)
        removed += 1
    db.execute("DELETE FROM lesson_images WHERE lesson_id=?", (lesson_id,))
    return removed


def delete_page(image_id: int) -> Optional[int]:
    row = db.fetchone("SELECT * FROM lesson_images WHERE id=?", (image_id,))
    if not row:
        return None
    lesson_id = row["lesson_id"]
    _remove_disk_file(dict(row))
    db.execute("DELETE FROM lesson_images WHERE id=?", (image_id,))
    renumber(lesson_id)
    return lesson_id


def _remove_disk_file(row: dict) -> None:
    if (row.get("storage") or DISK) != DISK:
        return
    path = (row.get("image_path") or "").strip()
    if not path:
        return
    try:
        import config
        from pathlib import Path
        root = Path(config.DB_PATH).resolve().parent
        fp = root / path.lstrip("/")
        if fp.exists():
            fp.unlink()
    except Exception as e:
        log.warning("remove note file: %s", e)


def storage_summary(lesson_id: int) -> dict:
    """Сколько страниц где лежит — для понятных подписей в интерфейсе."""
    rows = pages(lesson_id)
    in_tg = sum(1 for r in rows if (r.get("storage") or DISK) == TG)
    return {"total": len(rows), "telegram": in_tg, "disk": len(rows) - in_tg}
