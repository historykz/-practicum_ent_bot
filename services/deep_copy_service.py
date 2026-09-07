"""
Полная копия предмета — со своими разделами, уроками, конспектами, тестами
и вопросами, зачётами, рабочими тетрадями и видео.

Отличие от копии-витрины (webapp/shortcuts.py): там копия — ярлык на общий
контент, а здесь всё дублируется, и дальше оригинал с копией живут независимо:
правка, удаление или замена теста в одном никак не трогают другой.

Личные данные учеников (прогресс, попытки, доступы, журналы) не копируются:
у нового предмета чистая история.

Строки копируются «как есть» по списку колонок из самой базы (PRAGMA
table_info): новая колонка в схеме автоматически попадёт в копию, ничего
дописывать не нужно.
"""
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

import config
import database as db
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

# Служебные колонки, которые копия получает заново
_SKIP_COLS = {"id", "created_at", "updated_at"}

# Обложка урока/раздела берётся у ярлыка (если копируем витрину), контент — у оригинала
_LESSON_SHELL = ("title", "description", "status", "is_paid", "sort_order",
                 "free_override", "price_stars")


def _cols(table: str) -> list:
    return [r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")]


def _insert_like(table: str, row: dict, **override) -> int:
    cols = [c for c in _cols(table) if c not in _SKIP_COLS]
    vals = [override[c] if c in override else row.get(c) for c in cols]
    db.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals)
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def _uploads_root() -> Path:
    return Path(config.DB_PATH).resolve().parent / "uploads"


def _dup_file(value: Optional[str], subdir: str) -> Optional[str]:
    """Копия файла под новым именем в том же каталоге.

    value может быть именем файла, путём вида /uploads/<subdir>/имя или
    абсолютным путём — ответ в том же формате. None, если файла нет: копия
    не должна ссылаться на чужой файл, иначе удаление одного урока унесёт
    файл у другого.
    """
    value = (value or "").strip()
    if not value:
        return None
    base = os.path.basename(value)
    folder = _uploads_root() / subdir
    candidates = [Path(value), _uploads_root() / value.lstrip("/"),
                  folder / base]
    if value.startswith("/uploads/"):
        candidates.insert(0, _uploads_root().parent / value.lstrip("/"))
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        return None
    new_name = f"{uuid.uuid4().hex}{src.suffix}"
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, src.parent / new_name)
    except Exception as e:
        log.warning("copy file %s: %s", src, e)
        return None
    return value[: len(value) - len(base)] + new_name if value.endswith(base) else new_name


def _copy_test(test_id: Optional[int]) -> Optional[int]:
    if not test_id:
        return None
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return None
    new_tid = _insert_like("tests", dict(test))
    for q in db.fetchall("SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id",
                         (test_id,)):
        q = dict(q)
        new_qid = _insert_like(
            "questions", q, test_id=new_tid, poll_id=None, serial_no=None,
            web_image_path=_dup_file(q.get("web_image_path"), "questions"))
        for o in db.fetchall(
                "SELECT * FROM question_options WHERE question_id=? ORDER BY order_num, id",
                (q["id"],)):
            _insert_like("question_options", dict(o), question_id=new_qid)
    try:
        modes = db.fetchone("SELECT * FROM test_modes WHERE test_id=?", (test_id,))
        if modes:
            _insert_like("test_modes", dict(modes), test_id=new_tid)
    except Exception:
        pass
    return new_tid


def _copy_lesson(lesson_row: dict, new_section_id: int) -> Optional[int]:
    real_id = sc.orig_lesson_id(lesson_row["id"])
    real = db.fetchone("SELECT * FROM lessons WHERE id=?", (real_id,))
    if not real:
        return None
    merged = dict(real)
    if lesson_row.get("original_id"):
        for f in _LESSON_SHELL:
            if f in lesson_row and lesson_row.get(f) is not None:
                merged[f] = lesson_row[f]
    merged["sort_order"] = lesson_row.get("sort_order") or 0
    new_lid = _insert_like(
        "lessons", merged,
        section_id=new_section_id, original_id=None,
        test_id=_copy_test(real.get("test_id")),
        workbook_path=_dup_file(real.get("workbook_path"), "workbooks"),
        video_url=_dup_file(real.get("video_url"), "videos"),
    )
    # Страницы конспекта: с диска — копией файла, из Telegram — той же ссылкой
    for img in db.fetchall(
            "SELECT * FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
            (real_id,)):
        img = dict(img)
        if (img.get("storage") or "disk") == "telegram" or not (img.get("image_path") or "").strip():
            _insert_like("lesson_images", img, lesson_id=new_lid)
            continue
        new_path = _dup_file(img.get("image_path"), "lessons")
        if new_path:
            _insert_like("lesson_images", img, lesson_id=new_lid, image_path=new_path)
    # Банк вопросов зачёта
    for zq in db.fetchall(
            "SELECT * FROM zachet_questions WHERE lesson_id=? ORDER BY order_num, id",
            (real_id,)):
        _insert_like("zachet_questions", dict(zq), lesson_id=new_lid)
    return new_lid


def copy_subject_full(subject_id: int, new_title: str = "") -> Optional[int]:
    """Независимая копия предмета целиком. Возвращает id нового предмета."""
    shell = db.fetchone("SELECT * FROM subjects WHERE id=?", (subject_id,))
    if not shell:
        return None
    real_sid = sc.orig_subject_id(subject_id)
    real = db.fetchone("SELECT * FROM subjects WHERE id=?", (real_sid,)) or shell
    title = (new_title or "").strip() or f"{shell['title']} (копия)"
    merged = dict(real)
    for f in ("description", "status", "access_mode", "is_open", "is_private"):
        if shell.get(f) is not None:
            merged[f] = shell[f]
    max_sort = db.fetchone("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM subjects")["n"]
    new_sid = _insert_like("subjects", merged, title=title, original_id=None,
                           is_pinned=0, sort_order=max_sort)
    # Разделы берём у той записи, что скопировали (у витрины — её ярлыки),
    # а контент каждого раздела и урока — у их оригиналов.
    src_sections = db.fetchall(
        "SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (subject_id,))
    if not src_sections and real_sid != subject_id:
        src_sections = db.fetchall(
            "SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (real_sid,))
    for sec in src_sections:
        sec = dict(sec)
        sec_real_id = sc.orig_section_id(sec["id"])
        sec_real = dict(db.fetchone("SELECT * FROM sections WHERE id=?", (sec_real_id,)) or sec)
        sec_real["title"] = sec["title"]
        new_sec = _insert_like("sections", sec_real, subject_id=new_sid, original_id=None,
                               sort_order=sec.get("sort_order") or 0)
        lessons = db.fetchall(
            "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id", (sec["id"],))
        if not lessons and sec_real_id != sec["id"]:
            lessons = db.fetchall(
                "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id",
                (sec_real_id,))
        for les in lessons:
            _copy_lesson(dict(les), new_sec)
    try:
        from webapp import learning as _lg
        _lg.invalidate_catalog_cache()
    except Exception:
        pass
    return new_sid


def summary(subject_id: int) -> dict:
    """Сколько чего у предмета — для сообщения админу после копирования."""
    n_sec = db.fetchone("SELECT COUNT(*) AS c FROM sections WHERE subject_id=?", (subject_id,))["c"]
    n_les = db.fetchone(
        "SELECT COUNT(*) AS c FROM lessons l JOIN sections s ON s.id=l.section_id "
        "WHERE s.subject_id=?", (subject_id,))["c"]
    n_q = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions q JOIN lessons l ON l.test_id=q.test_id "
        "JOIN sections s ON s.id=l.section_id WHERE s.subject_id=?", (subject_id,))["c"]
    return {"sections": n_sec, "lessons": n_les, "questions": n_q}
