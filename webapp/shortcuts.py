"""
Копии-ярлыки предметов, разделов и уроков + режимы доступа предмета.

Идея: копия — это НЕ дубликат контента, а запись-ссылка (`original_id`).
Копия хранит только «обложку»: название, описание, свои настройки
платности/видимости. Текст конспекта, фото, тест, карточки, заучивание —
всё берётся у оригинала и физически лежит в одном месте.

Прогресс, попытки и доступ тоже привязаны к оригиналу: ученик прошёл тест
через копию-витрину — засчитано и в оригинальном предмете, потому что тест
физически один и тот же.

Доступ выдаётся ВСЕГДА на оригинал: админ может показать витрину всем,
а купившему выдать доступ к настоящему предмету — и платные уроки
откроются в обеих точках входа.
"""
from typing import Optional

import database as db

# Поля, которые копия берёт у оригинала (контент)
_CONTENT_FIELDS = (
    "content_html", "video_url", "youtube_id", "test_id",
    "is_zachet", "zachet_per_attempt", "zachet_topic_threshold",
    "zachet_pass_percent",
)
# Поля, которые остаются своими у копии (обложка и правила показа)
_SHELL_FIELDS = ("title", "description", "status", "is_paid", "sort_order",
                 "section_id", "original_id")

MAX_DEPTH = 5   # защита от кольца копия→копия→…


# === Разрешение ссылок ===

def orig_id(table: str, row_id) -> int:
    """ID оригинала: идём по цепочке original_id до настоящей записи."""
    if not row_id:
        return row_id
    cur = int(row_id)
    for _ in range(MAX_DEPTH):
        row = db.fetchone(f"SELECT original_id FROM {table} WHERE id=?", (cur,))
        if not row or not row["original_id"]:
            return cur
        nxt = int(row["original_id"])
        if nxt == cur:
            return cur
        cur = nxt
    return cur


def orig_subject_id(subject_id) -> int:
    return orig_id("subjects", subject_id)


def orig_section_id(section_id) -> int:
    return orig_id("sections", section_id)


def orig_lesson_id(lesson_id) -> int:
    return orig_id("lessons", lesson_id)


def resolve_lesson(lesson) -> dict:
    """Урок для показа ученику.

    Для обычного урока — он сам. Для копии — контент оригинала под обложкой
    копии. `id` намеренно становится ID ОРИГИНАЛА: так прогресс, попытки и
    доступ автоматически общие у всех точек входа. ID самой копии остаётся
    в `copy_id` — он нужен только для ссылок и навигации.
    """
    if lesson is None:
        return None
    l = dict(lesson)
    l.setdefault("copy_id", l["id"])
    l["is_copy"] = bool(l.get("original_id"))
    if not l.get("original_id"):
        l["orig_lesson_id"] = l["id"]
        return l

    real_id = orig_lesson_id(l["id"])
    orig = db.fetchone("SELECT * FROM lessons WHERE id=?", (real_id,))
    if not orig:
        # Оригинал удалили — показываем пустую обложку, но не падаем
        l["orig_missing"] = True
        l["orig_lesson_id"] = l["id"]
        return l
    merged = dict(orig)
    for f in _SHELL_FIELDS:
        if f in l:
            merged[f] = l[f]
    # Ссылка на карточки: своя у копии, иначе оригинала
    if not (l.get("quizlet_url") or "").strip():
        merged["quizlet_url"] = orig["quizlet_url"]
    else:
        merged["quizlet_url"] = l["quizlet_url"]
    merged["id"] = real_id          # прогресс и доступ — на оригинале
    merged["copy_id"] = l["id"]     # для ссылок в интерфейсе
    merged["orig_lesson_id"] = real_id
    merged["is_copy"] = True
    return merged


def lesson_url_id(lesson: dict) -> int:
    """ID для ссылок: копию открываем по её собственному id."""
    return lesson.get("copy_id") or lesson["id"]


# === Режимы доступа предмета ===

OPEN, CLOSED, PREMIUM, PRIVATE = "open", "closed", "premium", "private"
MODES = (OPEN, CLOSED, PREMIUM, PRIVATE)

MODE_TITLES = {
    OPEN: "Открыт всем без выдачи доступа",
    CLOSED: "Виден в каталоге, весь контент только по выданному доступу",
    PREMIUM: "Виден в каталоге: бесплатные уроки открыты, платные — по Премиуму или доступу",
    PRIVATE: "Приватный: не виден в каталоге, только по выданному доступу",
}


def subject_mode(subject) -> str:
    """Режим доступа предмета с оглядкой на старые записи без access_mode."""
    if not subject:
        return PRIVATE
    m = (subject.get("access_mode") or "").strip().lower()
    if m in MODES:
        return m
    if subject.get("is_private"):
        return PRIVATE
    if subject.get("is_open"):
        return OPEN
    return CLOSED


def mode_flags(mode: str):
    """(is_open, is_private) для совместимости со старым кодом и ботом."""
    return (1 if mode == OPEN else 0, 1 if mode == PRIVATE else 0)


def hidden_from_catalog(subject) -> bool:
    """Приватный предмет не показываем тем, у кого нет доступа."""
    return subject_mode(subject) == PRIVATE


def premium_ignored(subject) -> bool:
    """Предмет продаётся отдельно: общий Премиум его платные уроки не открывает.

    Нужно, когда один курс продают сам по себе — иначе купивший Премиум ради
    другого предмета получал бы и этот бесплатно.
    """
    return bool(subject and subject.get("premium_ignored"))


def needs_subject_access(subject) -> bool:
    """Нужен ли доступ к предмету, чтобы вообще открывать уроки."""
    return subject_mode(subject) in (CLOSED, PRIVATE)


def copy_badge(row) -> str:
    """Подпись копии в админке."""
    return "🔗 копия" if row and row.get("original_id") else ""


# === Создание копий ===

def copy_lesson(lesson_id: int, target_section_id: int) -> Optional[int]:
    """Ярлык урока в другом разделе. Контент не дублируется."""
    src = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not src:
        return None
    real = orig_lesson_id(lesson_id)
    db.execute(
        "INSERT INTO lessons (section_id, title, description, status, is_paid, "
        "original_id, sort_order) VALUES (?,?,?,?,?,?, "
        "COALESCE((SELECT MAX(sort_order)+1 FROM lessons WHERE section_id=?),0))",
        (target_section_id, src["title"], src["description"], src["status"],
         src["is_paid"], real, target_section_id))
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def copy_section(section_id: int, target_subject_id: int) -> Optional[int]:
    """Ярлык раздела: сам раздел-обложка + ярлыки всех его уроков."""
    src = db.fetchone("SELECT * FROM sections WHERE id=?", (section_id,))
    if not src:
        return None
    real = orig_section_id(section_id)
    db.execute(
        "INSERT INTO sections (subject_id, title, original_id, sort_order) "
        "VALUES (?,?,?, COALESCE((SELECT MAX(sort_order)+1 FROM sections "
        "WHERE subject_id=?),0))",
        (target_subject_id, src["title"], real, target_subject_id))
    new_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
    for l in db.fetchall(
            "SELECT id FROM lessons WHERE section_id=? ORDER BY sort_order, id",
            (real,)):
        copy_lesson(l["id"], new_id)
    return new_id


def copy_subject(subject_id: int, new_title: str = "") -> Optional[int]:
    """Ярлык предмета целиком: обложка + ярлыки разделов и уроков.

    Копия создаётся открытой витриной (виден в каталоге, платные уроки
    по Премиуму) — админ дальше меняет режим как хочет.
    """
    src = db.fetchone("SELECT * FROM subjects WHERE id=?", (subject_id,))
    if not src:
        return None
    real = orig_subject_id(subject_id)
    title = (new_title or "").strip() or f"{src['title']} (копия)"
    db.execute(
        "INSERT INTO subjects (title, description, is_open, is_private, "
        "access_mode, original_id, created_by, status, sort_order, quizlet_url) "
        "VALUES (?,?,0,0,?,?,?, 'active', "
        "COALESCE((SELECT MAX(sort_order)+1 FROM subjects),0), ?)",
        (title, src["description"], PREMIUM, real, src["created_by"],
         src["quizlet_url"]))
    new_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
    for sec in db.fetchall(
            "SELECT id FROM sections WHERE subject_id=? ORDER BY sort_order, id",
            (real,)):
        copy_section(sec["id"], new_id)
    return new_id


def copies_of(table: str, row_id: int) -> list:
    """Где ещё стоит ярлык на эту запись."""
    rows = db.fetchall(
        f"SELECT id, title FROM {table} WHERE original_id=?", (orig_id(table, row_id),))
    return [dict(r) for r in rows]
