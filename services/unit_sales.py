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
    real = sc.orig_lesson_id(int(lesson_id))
    db.execute(
        "INSERT OR IGNORE INTO lesson_access (lesson_id, user_tg_id, granted_by_admin) "
        "VALUES (?,?,?)",
        (real, int(tg_id), admin_tg or None))
    # Кнопка покупки, выданная до v69 в витрине, несёт id урока-оригинала.
    # Раньше такая покупка открывала и витрину — открывает и уроки, в которые
    # витрина превратилась при переводе в независимые копии. Только в первые
    # 60 дней после перевода: дальше старых кнопок в чатах уже нет, а копии
    # могут продаваться отдельно.
    for r in db.fetchall(
            "SELECT l.id FROM lessons l JOIN sections s ON s.id=l.section_id "
            "JOIN subjects sub ON sub.id=s.subject_id WHERE l.legacy_lesson_id=? "
            "AND sub.converted_at IS NOT NULL AND sub.converted_at >= datetime('now', '-60 days')",
            (real,)):
        db.execute("INSERT OR IGNORE INTO lesson_access (lesson_id, user_tg_id, granted_by_admin) "
                   "VALUES (?,?,?)", (r["id"], int(tg_id), admin_tg or None))
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
    if kind == "subject":
        row = db.fetchone("SELECT title, own_price FROM subjects WHERE id=?", (int(obj_id),))
        if row:
            return (f"Хочу купить курс «{row['title']}»"
                    + (f" ({row['own_price']})" if (row["own_price"] or "").strip() else "") + ".")
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


# ───────────────────────── курс целиком («продаётся отдельно») ─────────────────────────
# Предмет с галочкой «Общий Премиум НЕ открывает» продаётся сам по себе. Ученик
# видит одну карточку: цену, срок доступа, что получает купивший, сколько уроков
# бесплатно, — и только потом кнопки «Написать менеджеру» и «Купить за звёзды».
# Покупка за звёзды выдаёт обычный доступ к предмету (subject_access) на срок.

DEFAULT_SUBJECT_BENEFITS = ("📖 Все конспекты курса — в Telegram, с вашей персональной меткой\n"
                            "📝 Все домашние задания и тесты\n"
                            "🃏 Карточки и режим заучивания\n"
                            "🏆 Рейтинг и прогресс по курсу")


def days_text(days: int) -> str:
    n = int(days or 0)
    if n <= 0:
        return "бессрочно"
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    return f"{n} " + {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def fmt_until(value) -> str:
    """UTC из базы → дата по Астане."""
    import utils
    from datetime import timedelta
    dt = utils.parse_utc_naive(value) if value else None
    return (dt + timedelta(hours=5)).strftime("%d.%m.%Y") if dt else ""


def subject_access_state(subject_id: int, tg_id) -> tuple:
    """(есть ли действующий доступ, до какого времени UTC — None = бессрочно)."""
    import utils
    if not tg_id:
        return False, None
    row = db.fetchone("SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
                      (int(subject_id), int(tg_id)))
    if not row:
        return False, None
    if not row["expires_at"]:
        return True, None
    return bool(utils.deadline_active(row["expires_at"])), row["expires_at"]


def subject_offer(subject_id, tg_id=None) -> dict:
    """Карточка покупки предмета, который продаётся отдельно. None — предмет не такой."""
    import config
    from webapp import learning as lg
    try:
        real = sc.orig_subject_id(int(subject_id))
    except (TypeError, ValueError):
        return None
    row = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'", (real,))
    s = dict(row) if row else None
    if not s or not sc.premium_ignored(s):
        return None
    lessons = [l for l in lg._flatten_subject_lessons_sync(real) if (l.get("status") or "open") == "open"]
    has, until = subject_access_state(real, tg_id)
    stars = int(s.get("own_price_stars") or 0)
    days = int(s.get("own_access_days") or 0)
    bot = (getattr(config, "WEB_BOT_USERNAME", "") or "").lstrip("@")
    paywall = lg._paywall_context_sync()
    manager = (lg._manager_link_sync(manager_text("subject", real)) or paywall.get("premium_pay_url")
               or (f"https://t.me/{bot}" if bot else ""))
    return {
        "subject_id": real, "title": s["title"], "price_text": (s.get("own_price") or "").strip(),
        "stars": stars, "days": days, "days_text": days_text(days),
        "benefits": (s.get("own_benefits") or "").strip() or DEFAULT_SUBJECT_BENEFITS,
        "free_count": sum(1 for l in lessons if not l.get("is_paid")),
        "paid_count": sum(1 for l in lessons if l.get("is_paid")),
        "has_access": has, "access_until": fmt_until(until) if has and until else "",
        "manager_url": manager,
        "stars_url": f"https://t.me/{bot}?start=buysub_{real}" if stars and bot else "",
        "mode": sc.subject_mode(s),
    }


def offer_message(offer: dict) -> tuple:
    """То же предложение сообщением в боте: (текст HTML, клавиатура)."""
    import utils
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    esc = utils.escape_html
    lines = [f"💰 <b>Курс «{esc(offer['title'])}» продаётся отдельно</b>", ""]
    if offer["price_text"]:
        lines.append(f"Цена: <b>{esc(offer['price_text'])}</b>"
                     + (f" · или ⭐ <b>{offer['stars']}</b> Stars" if offer["stars"] else ""))
    elif offer["stars"]:
        lines.append(f"Цена: ⭐ <b>{offer['stars']}</b> Stars")
    lines.append(f"Доступ: <b>{offer['days_text']}</b> с момента покупки. Общий Премиум этот курс не открывает.")
    lines += ["", "<b>Что вы получите:</b>", esc(offer["benefits"])]
    counts = []
    if offer["free_count"]:
        counts.append(f"🆓 бесплатно уже сейчас: {offer['free_count']} ур.")
    if offer["paid_count"]:
        counts.append(f"💎 после покупки: {offer['paid_count']} ур.")
    if counts:
        lines += ["", " · ".join(counts)]
    rows = []
    if offer["manager_url"]:
        rows.append([InlineKeyboardButton(text="💬 Написать менеджеру", url=offer["manager_url"])])
    if offer["stars_url"]:
        rows.append([InlineKeyboardButton(text=f"⭐ Купить за {offer['stars']} Stars", url=offer["stars_url"])])
    return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


def grant_subject(tg_id: int, subject_id: int, charge_id: str = "", days: int = None):
    """Выдать доступ к предмету за покупку. Срок — из настроек предмета; действующий
    доступ продлевается от его конца, бессрочный не трогается. Возвращает конец (UTC) или None."""
    import utils
    from datetime import datetime, timedelta
    real = sc.orig_subject_id(int(subject_id))
    srow = db.fetchone("SELECT own_access_days FROM subjects WHERE id=?", (real,))
    days = int((srow or {}).get("own_access_days") or 0) if days is None else int(days)
    row = db.fetchone("SELECT id, expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
                      (real, int(tg_id)))
    if row and not row["expires_at"]:
        log.info("Курс %s: у %s уже бессрочный доступ (%s)", real, tg_id, charge_id or "вручную")
        return None
    now = datetime.utcnow().replace(microsecond=0)
    until = None
    if days > 0:
        cur_end = utils.parse_utc_naive(row["expires_at"]) if row and row["expires_at"] else None
        base = cur_end if cur_end and cur_end > now else now
        until = (base + timedelta(days=days)).isoformat(timespec="seconds")
    if row:
        db.execute("UPDATE subject_access SET expires_at=? WHERE id=?", (until, row["id"]))
    else:
        db.execute("INSERT INTO subject_access (subject_id, user_tg_id, expires_at, granted_by_admin) "
                   "VALUES (?,?,?,NULL)", (real, int(tg_id), until))
    log.info("Курс %s открыт для %s до %s (%s)", real, tg_id, until or "бессрочно", charge_id or "вручную")
    return until
