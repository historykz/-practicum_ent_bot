"""
Ручные послабления стоп-уроков для конкретного ученика.

Стоп-уроки предмета (последовательность + минимальное время чтения) — правило
для всех. Но бывает, что одному человеку их нужно ослабить: он уже проходил
курс раньше и не должен сдавать всё заново, или просто спешит. Админ делает
это на странице предмета в админке, ученику ничего нажимать не нужно.

Два вида послаблений:
  unlock — уроки до указанного включительно считаются пройденными: следующий
           за ним открывается сразу, а на самих этих уроках не тикает таймер
           чтения;
  off    — стоп-уроки для этого ученика в предмете выключены целиком.

Ключи всегда на ОРИГИНАЛЕ предмета и урока: копия-витрина делит их с ним,
как и доступ.
"""
from typing import Optional

import database as db
from webapp import shortcuts as sc

OFF, UNLOCK = "off", "unlock"

_DDL = """
CREATE TABLE IF NOT EXISTS stop_lesson_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_tg_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                  -- off | unlock
    lesson_id INTEGER DEFAULT 0,         -- для unlock: до какого урока включительно
    granted_by INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_tg_id, subject_id, kind)
)
"""


def ensure_table() -> None:
    """Таблицу создаёт database.init_db; это подстраховка для старой базы."""
    try:
        db.execute(_DDL)
        db.execute("CREATE INDEX IF NOT EXISTS idx_stop_over_user "
                   "ON stop_lesson_overrides(user_tg_id, subject_id)")
    except Exception:
        pass


def state(tg_id, subject_id) -> dict:
    """{"off": bool, "unlock_to": id урока-оригинала или 0}."""
    if not tg_id or not subject_id:
        return {"off": False, "unlock_to": 0}
    try:
        rows = db.fetchall(
            "SELECT kind, lesson_id FROM stop_lesson_overrides "
            "WHERE user_tg_id=? AND subject_id=?",
            (int(tg_id), sc.orig_subject_id(subject_id)))
    except Exception:
        ensure_table()
        return {"off": False, "unlock_to": 0}
    off = any(r["kind"] == OFF for r in rows)
    unlock_to = next((int(r["lesson_id"] or 0) for r in rows if r["kind"] == UNLOCK), 0)
    return {"off": off, "unlock_to": unlock_to}


def set_off(tg_id: int, subject_id: int, admin_tg_id: Optional[int] = None) -> None:
    ensure_table()
    db.execute(
        "INSERT INTO stop_lesson_overrides (user_tg_id, subject_id, kind, lesson_id, granted_by) "
        "VALUES (?,?,?,0,?) ON CONFLICT(user_tg_id, subject_id, kind) "
        "DO UPDATE SET granted_by=excluded.granted_by, created_at=CURRENT_TIMESTAMP",
        (int(tg_id), sc.orig_subject_id(subject_id), OFF, admin_tg_id))


def unlock_upto(tg_id: int, subject_id: int, lesson_id: int,
                admin_tg_id: Optional[int] = None) -> None:
    ensure_table()
    db.execute(
        "INSERT INTO stop_lesson_overrides (user_tg_id, subject_id, kind, lesson_id, granted_by) "
        "VALUES (?,?,?,?,?) ON CONFLICT(user_tg_id, subject_id, kind) "
        "DO UPDATE SET lesson_id=excluded.lesson_id, granted_by=excluded.granted_by, "
        "created_at=CURRENT_TIMESTAMP",
        (int(tg_id), sc.orig_subject_id(subject_id), UNLOCK,
         sc.orig_lesson_id(lesson_id), admin_tg_id))


def remove(tg_id: int, subject_id: int, kind: Optional[str] = None) -> None:
    ensure_table()
    if kind in (OFF, UNLOCK):
        db.execute("DELETE FROM stop_lesson_overrides WHERE user_tg_id=? AND subject_id=? AND kind=?",
                   (int(tg_id), sc.orig_subject_id(subject_id), kind))
    else:
        db.execute("DELETE FROM stop_lesson_overrides WHERE user_tg_id=? AND subject_id=?",
                   (int(tg_id), sc.orig_subject_id(subject_id)))


def list_for_subject(subject_id: int) -> list:
    """Кому и что ослаблено в предмете — для админки."""
    ensure_table()
    rows = db.fetchall(
        "SELECT o.*, u.username, u.first_name, l.title AS lesson_title "
        "FROM stop_lesson_overrides o "
        "LEFT JOIN users u ON u.tg_id = o.user_tg_id "
        "LEFT JOIN lessons l ON l.id = o.lesson_id "
        "WHERE o.subject_id=? ORDER BY o.id DESC",
        (sc.orig_subject_id(subject_id),))
    return [dict(r) for r in rows]
