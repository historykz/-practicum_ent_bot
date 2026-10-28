"""
Содержимое урока на сайте: блоки по порядку.

Урок теперь может состоять из любого числа блоков в любом порядке:
  text         текстовое объяснение
  interactive  интерактивное задание из библиотеки (одно задание можно ставить в разные уроки)
  image        картинка или схема
  video        видео с YouTube

Конспект (в Telegram), видео урока, рабочая тетрадь и итоговый тест остаются на
своих прежних местах: блоки показываются между ними, перед тестом.
Блоки принадлежат настоящему уроку: у старого ярлыка показываются блоки оригинала.
"""
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

import config
import database as db

log = logging.getLogger(__name__)

KINDS = ("text", "interactive", "image", "video")
TEXT_MAX = 20000
IMAGE_MAX = 8 * 1024 * 1024
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def _orig(lesson_id: int) -> int:
    from webapp import shortcuts as sc
    return int(sc.orig_lesson_id(int(lesson_id)))


def images_dir() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "blocks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def image_url(name: Optional[str]) -> str:
    return f"/uploads/blocks/{name}" if name else ""


def _next_order(lesson_id: int) -> int:
    row = db.fetchone("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM lesson_blocks WHERE lesson_id=?",
                      (lesson_id,))
    return row["n"] if row else 1


def lesson_exists(lesson_id: int) -> bool:
    return bool(db.fetchone("SELECT 1 FROM lessons WHERE id=?", (int(lesson_id),)))


def list_blocks(lesson_id: int) -> list:
    rows = db.fetchall(
        "SELECT b.*, t.title AS task_title, t.status AS task_status, t.visibility AS task_visibility, "
        "t.source AS task_source, t.grading AS task_grading, t.frame_height AS task_height "
        "FROM lesson_blocks b LEFT JOIN interactive_tasks t ON t.id=b.task_id "
        "WHERE b.lesson_id=? ORDER BY b.sort_order, b.id", (_orig(lesson_id),))
    out = []
    for r in rows:
        r = dict(r)
        r["image_url"] = image_url(r.get("image_path"))
        out.append(r)
    return out


def student_blocks(lesson_id: int, is_admin: bool = False) -> list:
    """Блоки для страницы урока. Неопубликованные интерактивы видит только админ."""
    out = []
    for b in list_blocks(lesson_id):
        if b["kind"] == "interactive":
            if not b.get("task_title"):
                continue
            if b.get("task_status") != "published" and not is_admin:
                continue
        out.append(b)
    return out


def add_text(lesson_id: int, title: str, body: str) -> tuple:
    body = (body or "").strip()
    if not body:
        return None, "Введите текст блока."
    if len(body) > TEXT_MAX:
        return None, f"Текст блока длиннее {TEXT_MAX} символов: разбейте на несколько блоков."
    lid = _orig(lesson_id)
    cur = db.execute("INSERT INTO lesson_blocks (lesson_id, sort_order, kind, title, body) VALUES (?,?,?,?,?)",
                     (lid, _next_order(lid), "text", (title or "").strip()[:200], body))
    return cur.lastrowid, ""


def add_interactive(lesson_id: int, task_id: int, title: str = "") -> tuple:
    if not db.fetchone("SELECT 1 FROM interactive_tasks WHERE id=?", (int(task_id),)):
        return None, "Интерактивное задание не найдено."
    lid = _orig(lesson_id)
    cur = db.execute("INSERT INTO lesson_blocks (lesson_id, sort_order, kind, title, task_id) VALUES (?,?,?,?,?)",
                     (lid, _next_order(lid), "interactive", (title or "").strip()[:200], int(task_id)))
    return cur.lastrowid, ""


def add_tasks(lesson_id: int, task_ids: list) -> int:
    n = 0
    for tid in task_ids:
        bid, err = add_interactive(lesson_id, tid)
        if bid:
            n += 1
    return n


def add_image(lesson_id: int, file_name: str, blob: bytes, title: str = "", caption: str = "") -> tuple:
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext not in IMAGE_EXT:
        return None, "Нужна картинка: png, jpg, gif, webp или svg."
    if not blob:
        return None, "Файл пустой."
    if len(blob) > IMAGE_MAX:
        return None, "Картинка больше 8 МБ."
    name = secrets.token_hex(16) + ext
    with open(images_dir() / name, "wb") as f:
        f.write(blob)
    lid = _orig(lesson_id)
    cur = db.execute("INSERT INTO lesson_blocks (lesson_id, sort_order, kind, title, body, image_path) "
                     "VALUES (?,?,?,?,?,?)", (lid, _next_order(lid), "image", (title or "").strip()[:200],
                                             (caption or "").strip()[:2000], name))
    return cur.lastrowid, ""


def add_video(lesson_id: int, url: str, title: str = "") -> tuple:
    from webapp import video
    yid = video.extract_youtube_id((url or "").strip())
    if not yid:
        return None, "Нужна ссылка на видео YouTube."
    lid = _orig(lesson_id)
    cur = db.execute("INSERT INTO lesson_blocks (lesson_id, sort_order, kind, title, youtube_id) VALUES (?,?,?,?,?)",
                     (lid, _next_order(lid), "video", (title or "").strip()[:200], yid))
    return cur.lastrowid, ""


def get_block(block_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM lesson_blocks WHERE id=?", (int(block_id),))
    return dict(row) if row else None


def update_block(block_id: int, title: str, body: str) -> str:
    b = get_block(block_id)
    if not b:
        return "Блок не найден."
    body = (body or "").strip()
    if b["kind"] == "text" and not body:
        return "Текст блока не может быть пустым."
    if len(body) > TEXT_MAX:
        return f"Текст длиннее {TEXT_MAX} символов."
    db.execute("UPDATE lesson_blocks SET title=?, body=? WHERE id=?", ((title or "").strip()[:200], body, block_id))
    return ""


def _remove_image(b: dict) -> None:
    if b.get("kind") == "image" and b.get("image_path"):
        others = db.fetchone("SELECT COUNT(*) AS c FROM lesson_blocks WHERE image_path=? AND id<>?",
                             (b["image_path"], b["id"]))["c"]
        if not others:
            try:
                (images_dir() / b["image_path"]).unlink()
            except OSError:
                pass


def delete_block(block_id: int) -> Optional[int]:
    b = get_block(block_id)
    if not b:
        return None
    _remove_image(b)
    db.execute("DELETE FROM lesson_blocks WHERE id=?", (block_id,))
    return b["lesson_id"]


def move_block(block_id: int, direction: str) -> Optional[int]:
    b = get_block(block_id)
    if not b:
        return None
    ids = [r["id"] for r in db.fetchall("SELECT id FROM lesson_blocks WHERE lesson_id=? ORDER BY sort_order, id",
                                        (b["lesson_id"],))]
    i = ids.index(b["id"])
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for n, bid in enumerate(ids, 1):
        db.execute("UPDATE lesson_blocks SET sort_order=? WHERE id=?", (n, bid))
    return b["lesson_id"]


def reorder(lesson_id: int, order: list) -> None:
    lid = _orig(lesson_id)
    own = {r["id"] for r in db.fetchall("SELECT id FROM lesson_blocks WHERE lesson_id=?", (lid,))}
    clean = [int(x) for x in order if str(x).isdigit() and int(x) in own]
    rest = [i for i in sorted(own) if i not in clean]
    for n, bid in enumerate(clean + rest, 1):
        db.execute("UPDATE lesson_blocks SET sort_order=? WHERE id=?", (n, bid))


def transfer(block_id: int, target_lesson_id: int, copy: bool = False) -> tuple:
    """Перенести (или скопировать) блок в конец другого урока."""
    b = get_block(block_id)
    if not b:
        return None, "Блок не найден."
    if not lesson_exists(target_lesson_id):
        return None, "Урок назначения не найден."
    target = _orig(target_lesson_id)
    if copy:
        image = b.get("image_path")
        if b["kind"] == "image" and image:
            src = images_dir() / image
            if src.exists():
                image = secrets.token_hex(16) + os.path.splitext(image)[1]
                (images_dir() / image).write_bytes(src.read_bytes())
        cur = db.execute(
            "INSERT INTO lesson_blocks (lesson_id, sort_order, kind, title, body, task_id, image_path, youtube_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (target, _next_order(target), b["kind"], b["title"], b["body"], b["task_id"], image, b["youtube_id"]))
        return cur.lastrowid, ""
    db.execute("UPDATE lesson_blocks SET lesson_id=?, sort_order=? WHERE id=?",
               (target, _next_order(target), block_id))
    return block_id, ""


def copy_all(src_lesson_id: int, dst_lesson_id: int) -> int:
    """Все блоки урока — в другой урок (копирование урока или предмета).
    Интерактивы не дублируются: копия ссылается на то же задание библиотеки."""
    n = 0
    for b in db.fetchall("SELECT id FROM lesson_blocks WHERE lesson_id=? ORDER BY sort_order, id",
                         (int(src_lesson_id),)):
        bid, _err = transfer(b["id"], dst_lesson_id, copy=True)
        if bid:
            n += 1
    return n


def lessons_of_task(task_id: int) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT b.id AS block_id, l.id AS lesson_id, l.title AS lesson_title, s.title AS section_title, "
        "sub.title AS subject_title, sub.id AS subject_id, s.id AS section_id FROM lesson_blocks b "
        "JOIN lessons l ON l.id=b.lesson_id LEFT JOIN sections s ON s.id=l.section_id "
        "LEFT JOIN subjects sub ON sub.id=s.subject_id WHERE b.task_id=? ORDER BY sub.id, s.sort_order, l.sort_order",
        (int(task_id),))]


def task_in_lesson(task_id: int, lesson_id: int) -> bool:
    return bool(db.fetchone("SELECT 1 FROM lesson_blocks WHERE task_id=? AND lesson_id=?",
                            (int(task_id), _orig(lesson_id))))


def lesson_choices() -> list:
    """Все уроки для выпадающих списков «вставить в урок»: предмет › раздел › урок."""
    return [dict(r) for r in db.fetchall(
        "SELECT l.id, l.title, s.title AS section_title, sub.title AS subject_title FROM lessons l "
        "JOIN sections s ON s.id=l.section_id JOIN subjects sub ON sub.id=s.subject_id "
        "WHERE l.original_id IS NULL ORDER BY sub.sort_order, sub.id, s.sort_order, s.id, l.sort_order, l.id")]
