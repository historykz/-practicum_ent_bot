"""
Учебная активность ученика: что он делал, где остановился и насколько отстаёт.

Главная мысль: заход в бота — это ещё не учёба. Тема считается закрытой,
только когда человек и конспект открыл, и домашнее задание сдал. Поэтому
здесь отдельно копятся события «зашёл», «открыл конспект», «начал ДЗ»,
«сдал ДЗ», а из них уже собирается картина по каждому ученику.

Все пороги настраиваются админом и лежат в settings — в коде чисел нет.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import database as db
import utils

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))     # время, по которому живут ученики

# Событие = что именно сделал человек
VISIT = "visit"            # просто зашёл в бота
NOTE_OPEN = "note_open"    # открыл конспект
HW_START = "hw_start"      # начал домашнее задание
HW_DONE = "hw_done"        # сдал домашнее задание

# Статусы темы — их видит админ в карточке ученика
ST_NONE = "not_started"
ST_NOTE = "note_opened"
ST_HW_STARTED = "hw_started"
ST_DONE = "done"
ST_OVERDUE = "overdue"

STATUS_TITLES = {
    ST_NONE: "Не начато",
    ST_NOTE: "Открыт конспект",
    ST_HW_STARTED: "ДЗ начато, не закончено",
    ST_DONE: "Выполнено",
    ST_OVERDUE: "Просрочено",
}

RISK_TITLES = {
    "ok": "🟢 Норма",
    "slight": "🟡 Небольшое отставание",
    "behind": "🟠 Отстаёт",
    "far": "🔴 Сильно отстаёт",
}

_ready = False


def ensure_schema() -> None:
    """Таблицы создаёт database.py; если его забыли обновить — не падаем."""
    global _ready
    if _ready:
        return
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS study_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL,
            event TEXT NOT NULL, lesson_id INTEGER, test_id INTEGER,
            details TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""CREATE TABLE IF NOT EXISTS study_state (
            tg_id INTEGER PRIMARY KEY, last_visit_at TEXT, last_note_at TEXT,
            last_note_lesson INTEGER, last_hw_at TEXT, last_hw_lesson INTEGER,
            warn_level INTEGER DEFAULT 0, last_warn_at TEXT, last_notify_at TEXT,
            last_motivation_at TEXT, returned_at TEXT, risk TEXT DEFAULT 'ok',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""CREATE TABLE IF NOT EXISTS study_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL,
            kind TEXT NOT NULL, text TEXT DEFAULT '', motivation_id INTEGER,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP, reaction TEXT DEFAULT '')""")
        _ready = True
    except Exception:
        pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_since(value) -> Optional[float]:
    dt = _parse(value)
    if not dt:
        return None
    return (now() - dt).total_seconds() / 86400


# ---------- Запись событий ----------

def track(tg_id: int, event: str, lesson_id: int = None,
          test_id: int = None, details: str = "") -> None:
    """Записать учебное действие. Вызывается из мест, где оно происходит."""
    if not tg_id:
        return
    ensure_schema()
    stamp = _iso(now())
    try:
        db.execute(
            "INSERT INTO study_events (tg_id, event, lesson_id, test_id, details, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (tg_id, event, lesson_id, test_id, (details or "")[:200], stamp))
    except Exception:
        return

    _ensure_state(tg_id)
    if event == VISIT:
        db.execute("UPDATE study_state SET last_visit_at=?, updated_at=? WHERE tg_id=?",
                   (stamp, stamp, tg_id))
    elif event == NOTE_OPEN:
        db.execute("UPDATE study_state SET last_note_at=?, last_note_lesson=?, "
                   "last_visit_at=?, updated_at=? WHERE tg_id=?",
                   (stamp, lesson_id, stamp, stamp, tg_id))
    elif event == HW_DONE:
        # Человек вернулся к учёбе — строгую цепочку останавливаем
        db.execute("UPDATE study_state SET last_hw_at=?, last_hw_lesson=?, "
                   "last_visit_at=?, warn_level=0, returned_at=?, updated_at=? "
                   "WHERE tg_id=?", (stamp, lesson_id, stamp, stamp, stamp, tg_id))
    elif event == HW_START:
        db.execute("UPDATE study_state SET last_visit_at=?, updated_at=? WHERE tg_id=?",
                   (stamp, stamp, tg_id))

    # Отмечаем в истории уведомлений, чем человек ответил на последнее письмо
    try:
        last = db.fetchone(
            "SELECT id, reaction FROM study_notifications WHERE tg_id=? "
            "ORDER BY id DESC LIMIT 1", (tg_id,))
        if last and not (last["reaction"] or "").strip():
            db.execute("UPDATE study_notifications SET reaction=? WHERE id=?",
                       (event, last["id"]))
    except Exception:
        pass


def _ensure_state(tg_id: int) -> None:
    try:
        db.execute("INSERT OR IGNORE INTO study_state (tg_id) VALUES (?)", (tg_id,))
    except Exception:
        pass


def get_state(tg_id: int) -> dict:
    ensure_schema()
    row = db.fetchone("SELECT * FROM study_state WHERE tg_id=?", (tg_id,))
    return dict(row) if row else {"tg_id": tg_id}


def set_state(tg_id: int, **fields) -> None:
    if not fields:
        return
    ensure_schema()
    _ensure_state(tg_id)
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE study_state SET {sets}, updated_at=? WHERE tg_id=?",
               tuple(fields.values()) + (_iso(now()), tg_id))


# ---------- Что с темами ученика ----------

def lesson_status(tg_id: int, lesson_id: int) -> str:
    """Статус одной темы для конкретного ученика."""
    ensure_schema()
    done = db.fetchone(
        "SELECT id FROM study_events WHERE tg_id=? AND lesson_id=? AND event=? LIMIT 1",
        (tg_id, lesson_id, HW_DONE))
    if done:
        return ST_DONE
    started = db.fetchone(
        "SELECT id FROM study_events WHERE tg_id=? AND lesson_id=? AND event=? LIMIT 1",
        (tg_id, lesson_id, HW_START))
    if started:
        return ST_HW_STARTED
    opened = db.fetchone(
        "SELECT id FROM study_events WHERE tg_id=? AND lesson_id=? AND event=? LIMIT 1",
        (tg_id, lesson_id, NOTE_OPEN))
    if opened:
        return ST_NOTE
    return ST_NONE


def unfinished_topics(tg_id: int, limit: int = 50) -> list:
    """Темы, которые начаты, но не закрыты домашним заданием."""
    ensure_schema()
    rows = db.fetchall(
        "SELECT DISTINCT lesson_id FROM study_events "
        "WHERE tg_id=? AND lesson_id IS NOT NULL AND event IN (?,?) "
        "ORDER BY id DESC LIMIT ?",
        (tg_id, NOTE_OPEN, HW_START, limit))
    out = []
    for r in rows:
        lid = r["lesson_id"]
        st = lesson_status(tg_id, lid)
        if st in (ST_NOTE, ST_HW_STARTED):
            lesson = db.fetchone("SELECT id, title FROM lessons WHERE id=?", (lid,))
            out.append({"lesson_id": lid,
                        "title": (lesson["title"] if lesson else f"Урок {lid}"),
                        "status": st, "status_title": STATUS_TITLES[st]})
    return out


def done_count(tg_id: int) -> int:
    ensure_schema()
    row = db.fetchone(
        "SELECT COUNT(DISTINCT lesson_id) AS c FROM study_events "
        "WHERE tg_id=? AND event=? AND lesson_id IS NOT NULL", (tg_id, HW_DONE))
    return row["c"] if row else 0


def opened_count(tg_id: int) -> int:
    ensure_schema()
    row = db.fetchone(
        "SELECT COUNT(DISTINCT lesson_id) AS c FROM study_events "
        "WHERE tg_id=? AND event=? AND lesson_id IS NOT NULL", (tg_id, NOTE_OPEN))
    return row["c"] if row else 0


def study_days(tg_id: int, days: int = 14) -> int:
    """В скольких днях из последних N человек реально занимался."""
    ensure_schema()
    since = _iso(now() - timedelta(days=days))
    rows = db.fetchall(
        "SELECT DISTINCT substr(created_at, 1, 10) AS d FROM study_events "
        "WHERE tg_id=? AND event IN (?,?,?) AND created_at >= ?",
        (tg_id, NOTE_OPEN, HW_START, HW_DONE, since))
    return len(rows)


# ---------- Насколько человек отстал ----------

def days_without_study(tg_id: int) -> Optional[float]:
    """Сколько дней не было НАСТОЯЩЕЙ учёбы (заход в бот не считается)."""
    ensure_schema()
    row = db.fetchone(
        "SELECT created_at FROM study_events WHERE tg_id=? AND event IN (?,?,?) "
        "ORDER BY id DESC LIMIT 1", (tg_id, NOTE_OPEN, HW_START, HW_DONE))
    if not row:
        return None                     # ни разу не занимался
    return days_since(row["created_at"])


def profile(tg_id: int) -> dict:
    """Полная картина по ученику — её же показываем админу."""
    ensure_schema()
    st = get_state(tg_id)
    user = db.fetchone(
        "SELECT id, tg_id, username, first_name FROM users WHERE tg_id=?", (tg_id,))
    idle = days_without_study(tg_id)
    unfinished = unfinished_topics(tg_id)
    done = done_count(tg_id)
    opened = opened_count(tg_id)
    rhythm = study_days(tg_id, 14)

    last_note_title = ""
    if st.get("last_note_lesson"):
        row = db.fetchone("SELECT title FROM lessons WHERE id=?",
                          (st["last_note_lesson"],))
        last_note_title = row["title"] if row else ""
    last_hw_title = ""
    if st.get("last_hw_lesson"):
        row = db.fetchone("SELECT title FROM lessons WHERE id=?",
                          (st["last_hw_lesson"],))
        last_hw_title = row["title"] if row else ""

    data = {
        "tg_id": tg_id,
        "user_id": user["id"] if user else None,
        "username": (user["username"] if user else "") or "",
        "name": (user["first_name"] if user else "") or "",
        "last_visit_at": st.get("last_visit_at"),
        "last_note_at": st.get("last_note_at"),
        "last_note_title": last_note_title,
        "last_hw_at": st.get("last_hw_at"),
        "last_hw_title": last_hw_title,
        "days_idle": idle,
        "unfinished": unfinished,
        "unfinished_count": len(unfinished),
        "done_topics": done,
        "opened_topics": opened,
        "rhythm_days": rhythm,          # дней учёбы за 2 недели
        "warn_level": st.get("warn_level") or 0,
        "last_warn_at": st.get("last_warn_at"),
        "premium_until": _premium_until(user["id"] if user else None),
    }
    data["percent"] = round(done / opened * 100) if opened else 0
    data["risk"] = risk_level(data)
    data["risk_title"] = RISK_TITLES[data["risk"]]
    data["case"] = behaviour_case(data)
    return data


def _premium_until(user_id) -> str:
    if not user_id:
        return ""
    try:
        row = db.fetchone(
            "SELECT expires_at FROM premium_users WHERE user_id=? "
            "ORDER BY id DESC LIMIT 1", (user_id,))
        return (row["expires_at"] or "") if row else ""
    except Exception:
        return ""


def risk_level(data: dict) -> str:
    """Насколько всё плохо. Учитываем и привычный ритм человека.

    Тому, кто занимался каждый день, два дня тишины — уже сигнал. Тому, кто
    занимается через день, один пропуск нормален. Поэтому пороги двигаются
    в зависимости от того, как человек занимался раньше.
    """
    idle = data.get("days_idle")
    if idle is None:
        return "ok"                       # ещё ни разу не занимался — не пугаем
    rhythm = data.get("rhythm_days") or 0

    # Послабление даём только тому, у кого действительно видна привычка
    # заниматься через день (4-7 дней из 14). Если человек занимался всего
    # раз-два, это не «его ритм», а как раз то самое отставание — растягивать
    # ему сроки нельзя, иначе бросивший ученик никогда не попадёт в отстающие.
    scale = 1.0
    if rhythm >= 8:
        scale = 0.7                       # занимался почти каждый день — реагируем раньше
    elif 4 <= rhythm <= 7:
        scale = 1.3                       # занимается через день — один пропуск не беда

    if idle >= 4 * scale:
        return "far"
    if idle >= 3 * scale:
        return "behind"
    if idle >= 2 * scale:
        return "slight"
    if data.get("unfinished_count", 0) >= 3:
        return "slight"                   # занимается, но хвосты копятся
    return "ok"


def behaviour_case(data: dict) -> str:
    """Что именно происходит с человеком — от этого зависит текст письма."""
    idle = data.get("days_idle")
    if idle is None:
        return "never_started"            # ни разу ничего не открывал
    note_days = days_since(data.get("last_note_at"))
    hw_days = days_since(data.get("last_hw_at"))

    if data.get("unfinished_count"):
        # Конспекты открывает, а задания не сдаёт — самый частый случай
        if note_days is not None and (hw_days is None or note_days < hw_days):
            return "reading_no_hw"
        return "hw_unfinished"
    if idle >= 1:
        return "idle"
    return "active"
