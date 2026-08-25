"""
Продажа отдельных уроков и целых разделов за Telegram Stars.

Всё регулирует админ на уровне предмета: включена ли продажа вообще, общая
цена урока по предмету, своя цена на раздел, своя на урок. Что задано
конкретнее — то и действует: урок → раздел → предмет.

Backend — окончательный судья: даже если кнопку как-то вызовут при
выключенной продаже, счёт не выставится и доступ не выдастся.
"""
import logging

import database as db
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

DEFAULT_LESSON_STARS = 80
DEFAULT_SECTION_STARS = 300


def _subject_of_section(section_id: int):
    row = db.fetchone(
        "SELECT s.* FROM subjects s JOIN sections sec ON sec.subject_id=s.id "
        "WHERE sec.id=?", (int(section_id),))
    return dict(row) if row else None


def sale_enabled_for_lesson(lesson_id: int) -> bool:
    """Разрешена ли отдельная покупка этого урока."""
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?",
                         (sc.orig_lesson_id(int(lesson_id)),))
    if not lesson or not lesson.get("is_paid"):
        return False
    subj = _subject_of_section(lesson["section_id"])
    return bool(subj and subj.get("unit_sale_enabled"))


def sale_enabled_for_section(section_id: int) -> bool:
    sec = db.fetchone("SELECT * FROM sections WHERE id=?", (int(section_id),))
    if not sec or not sec.get("sale_enabled", 1):
        return False
    subj = _subject_of_section(section_id)
    return bool(subj and subj.get("unit_sale_enabled"))


def lesson_price(lesson_id: int) -> int:
    """Цена урока: своя у урока → общая по предмету → запасная."""
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?",
                         (sc.orig_lesson_id(int(lesson_id)),))
    if not lesson:
        return 0
    if lesson.get("price_stars"):
        return int(lesson["price_stars"])
    subj = _subject_of_section(lesson["section_id"])
    if subj and subj.get("unit_price_stars"):
        return int(subj["unit_price_stars"])
    return DEFAULT_LESSON_STARS


def section_price(section_id: int) -> int:
    sec = db.fetchone("SELECT * FROM sections WHERE id=?", (int(section_id),))
    if not sec:
        return 0
    if sec.get("price_stars"):
        return int(sec["price_stars"])
    # По умолчанию — сумма цен платных уроков со скидкой не делается:
    # просто цена предмета за раздел либо запасная.
    subj = _subject_of_section(section_id)
    if subj and subj.get("unit_price_stars"):
        paid = db.fetchone("SELECT COUNT(*) AS c FROM lessons "
                           "WHERE section_id=? AND COALESCE(is_paid,0)=1",
                           (int(section_id),))["c"]
        if paid:
            return int(subj["unit_price_stars"]) * paid
    return DEFAULT_SECTION_STARS


def has_section_access(section_id: int, tg_id: int) -> bool:
    return db.fetchone(
        "SELECT id FROM section_access WHERE section_id=? AND user_tg_id=?",
        (int(section_id), int(tg_id))) is not None


def grant_lesson(tg_id: int, lesson_id: int, charge_id: str = "",
                 admin_tg: int = 0) -> None:
    db.execute(
        "INSERT OR IGNORE INTO lesson_access (lesson_id, user_tg_id, granted_by_admin) "
        "VALUES (?,?,?)",
        (sc.orig_lesson_id(int(lesson_id)), int(tg_id), admin_tg or None))
    log.info("Урок %s открыт для %s (%s)", lesson_id, tg_id, charge_id or "вручную")


def grant_section(tg_id: int, section_id: int, charge_id: str = "",
                  admin_tg: int = 0) -> None:
    """Купленный раздел открывает и все его уроки."""
    db.execute(
        "INSERT OR IGNORE INTO section_access (section_id, user_tg_id, charge_id, "
        "granted_by_admin) VALUES (?,?,?,?)",
        (int(section_id), int(tg_id), charge_id or None, admin_tg or None))
    for l in db.fetchall("SELECT id FROM lessons WHERE section_id=?",
                         (int(section_id),)):
        db.execute("INSERT OR IGNORE INTO lesson_access (lesson_id, user_tg_id) "
                   "VALUES (?,?)", (l["id"], int(tg_id)))
    log.info("Раздел %s открыт для %s (%s)", section_id, tg_id, charge_id or "вручную")


def manager_text(kind: str, obj_id: int) -> str:
    """Готовое сообщение менеджеру: что именно человек хочет купить."""
    if kind == "section":
        row = db.fetchone(
            "SELECT sec.title AS st, s.title AS subj FROM sections sec "
            "JOIN subjects s ON s.id=sec.subject_id WHERE sec.id=?", (int(obj_id),))
        if row:
            return (f"Хочу приобрести раздел «{row['st']}» "
                    f"по предмету «{row['subj']}».")
    else:
        row = db.fetchone(
            "SELECT l.title AS lt, s.title AS subj FROM lessons l "
            "JOIN sections sec ON sec.id=l.section_id "
            "JOIN subjects s ON s.id=sec.subject_id WHERE l.id=?",
            (sc.orig_lesson_id(int(obj_id)),))
        if row:
            return (f"Хочу приобрести урок «{row['lt']}» "
                    f"по предмету «{row['subj']}».")
    return "Хочу приобрести доступ к материалам."
