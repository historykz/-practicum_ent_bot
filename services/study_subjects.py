"""
Предметы под контролем обучения: кого отслеживаем и насколько он отстаёт.

Раньше контроль обучения смотрел на человека целиком: «есть Премиум — значит
ученик», «сколько дней не занимался». К какому предмету относится Премиум и
прогресс, система не знала. Теперь предмет — единица отслеживания.

Откуда берётся предмет: из той же ссылки, что даёт платформа
(t.me/бот/app?startapp=subj_12, t.me/бот?start=subj_12, сайт /learn/12).
Такие ссылки админ уже вставлял в бота в кампании «Конспекты ЕНТ» — они
подхватываются автоматически (migrate_from_campaigns), заново прикреплять
ничего не нужно. Плюс есть отдельное подключение по ссылке в «Контроле
обучения».

Кто ученик предмета: тот, у кого сейчас есть доступ именно к нему —
выданный на предмет (subject_access) или общий Премиум, если предмет его
принимает (режим «открыт»/«премиум» и не продаётся отдельно).

Что считается прогрессом: ровно то же, что и в стоп-уроках платформы —
урок закрыт, когда сдан его тест на проходной процент (или зачёт), а урок
без теста — когда прочитан. Просто открытый конспект или заход в бота
закрытым уроком не считается.

План: темп «уроков в неделю» (по предмету или общий) от даты начала.
Дата начала фиксируется один раз (первое действие в предмете или дата
выдачи доступа — что раньше) и при продлении Премиума не сдвигается.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import database as db
import utils
from services import study_settings as ss
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

ST_OK, ST_SLIGHT, ST_BEHIND, ST_FAR = "ok", "slight", "behind", "far"
_SEVERITY = {ST_OK: 0, ST_SLIGHT: 1, ST_BEHIND: 2, ST_FAR: 3}

_ready = False
_last_sync_at = 0.0


def ensure_schema() -> None:
    """Таблицы создаёт database.py; здесь подстраховка для старой базы."""
    global _ready
    if _ready:
        return
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS study_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL UNIQUE,
            source TEXT DEFAULT 'manual', link TEXT DEFAULT '',
            pace_per_week INTEGER, enabled INTEGER DEFAULT 1,
            students_count INTEGER DEFAULT 0, behind_count INTEGER DEFAULT 0,
            last_sync_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""CREATE TABLE IF NOT EXISTS study_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_tg_id INTEGER NOT NULL, subject_id INTEGER NOT NULL,
            active INTEGER DEFAULT 1, via TEXT DEFAULT '',
            plan_start TEXT, access_until TEXT,
            lessons_total INTEGER DEFAULT 0, lessons_done INTEGER DEFAULT 0,
            lessons_opened INTEGER DEFAULT 0, expected_done INTEGER DEFAULT 0,
            lag INTEGER DEFAULT 0, status TEXT DEFAULT 'ok',
            last_activity_at TEXT, deactivated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT,
            UNIQUE(user_tg_id, subject_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_study_tracking_user "
                   "ON study_tracking(user_tg_id, active)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_study_tracking_subject "
                   "ON study_tracking(subject_id, active)")
        _ready = True
    except Exception as e:
        log.warning("study_subjects schema: %s", e)


# ---------- Ссылка → предмет ----------

_LINK_RES = (
    re.compile(r"startapp=subj_(\d+)", re.I),
    re.compile(r"[?&]start=subj_(\d+)", re.I),
    re.compile(r"/learn/(\d+)(?:[/?#]|$)", re.I),
    re.compile(r"[?&]subject=(\d+)", re.I),
    re.compile(r"^\s*subj_?(\d+)\s*$", re.I),
    re.compile(r"^\s*(\d+)\s*$"),
)


def parse_subject_link(text: str) -> Optional[int]:
    """ID предмета из ссылки платформы (или просто из числа). None — не ссылка."""
    text = (text or "").strip()
    if not text:
        return None
    for rx in _LINK_RES:
        m = rx.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def resolve_subject(raw_id: int) -> Optional[dict]:
    """Оригинал предмета (копия-витрина делит с ним доступ и прогресс)."""
    if not raw_id:
        return None
    real = sc.orig_subject_id(int(raw_id))
    row = db.fetchone("SELECT * FROM subjects WHERE id=?", (real,))
    return dict(row) if row else None


# ---------- Список предметов под контролем ----------

def attach(subject_id: int, link: str = "", source: str = "manual") -> Optional[dict]:
    """Подключить предмет (повторное подключение — просто обновит ссылку)."""
    ensure_schema()
    subj = resolve_subject(subject_id)
    if not subj:
        return None
    db.execute(
        "INSERT INTO study_subjects (subject_id, source, link, enabled) VALUES (?,?,?,1) "
        "ON CONFLICT(subject_id) DO UPDATE SET enabled=1, "
        "link=CASE WHEN excluded.link<>'' THEN excluded.link ELSE study_subjects.link END",
        (subj["id"], source, (link or "")[:300]))
    return get(subj["id"])


def detach(subject_id: int) -> None:
    ensure_schema()
    real = sc.orig_subject_id(int(subject_id))
    db.execute("UPDATE study_subjects SET enabled=0 WHERE subject_id=?", (real,))
    db.execute("UPDATE study_tracking SET active=0, deactivated_at=? "
               "WHERE subject_id=? AND active=1", (_now_iso(), real))


def get(subject_id: int) -> Optional[dict]:
    ensure_schema()
    row = db.fetchone(
        "SELECT ss.*, s.title, s.pass_percent, s.access_mode FROM study_subjects ss "
        "JOIN subjects s ON s.id = ss.subject_id WHERE ss.subject_id=?",
        (sc.orig_subject_id(int(subject_id)),))
    return dict(row) if row else None


def tracked(only_enabled: bool = True) -> list:
    ensure_schema()
    sql = ("SELECT ss.*, s.title FROM study_subjects ss "
           "JOIN subjects s ON s.id = ss.subject_id ")
    if only_enabled:
        sql += "WHERE ss.enabled=1 "
    sql += "ORDER BY ss.id"
    try:
        return [dict(r) for r in db.fetchall(sql)]
    except Exception:
        return []


def set_pace(subject_id: int, pace: Optional[int]) -> None:
    ensure_schema()
    db.execute("UPDATE study_subjects SET pace_per_week=? WHERE subject_id=?",
               (pace if pace and pace > 0 else None, sc.orig_subject_id(int(subject_id))))


def pace_for(subj_row: dict) -> int:
    pace = subj_row.get("pace_per_week") if subj_row else None
    if pace and int(pace) > 0:
        return int(pace)
    return max(1, ss.get_int("study_pace_per_week", 3))


def migrate_from_campaigns() -> list:
    """Подхватить предметы из ссылок, которые админ уже вставлял в бота.

    Кнопки кампаний «Конспекты ЕНТ» и поздравления после Премиума ведут на
    предмет платформы (…startapp=subj_N). Это и есть ранее «прикреплённые»
    предметы — их не нужно подключать заново.
    """
    ensure_schema()
    added = []
    try:
        rows = db.fetchall(
            "SELECT campaign_key, button_url, button2_url FROM reminder_campaigns "
            "WHERE status='active'")
    except Exception:
        rows = []
    for r in rows:
        for url in (r.get("button_url"), r.get("button2_url")):
            sid = parse_subject_link(url or "")
            if not sid:
                continue
            subj = resolve_subject(sid)
            if not subj:
                continue
            exists = db.fetchone("SELECT id FROM study_subjects WHERE subject_id=?",
                                 (subj["id"],))
            if exists:
                continue
            attach(subj["id"], url or "", source=f"campaign:{r['campaign_key']}")
            added.append(subj["id"])
            log.info("контроль обучения: предмет %s подхвачен из ссылки кампании %s",
                     subj["id"], r["campaign_key"])
    return added


# ---------- Кто ученик предмета ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _active_until(expires_at) -> bool:
    """То же решение, что у доступа в боте и на сайте (utils.deadline_active).

    Раньше нечитаемая дата считалась здесь вечной: доступа у человека уже
    нет, а контроль обучения и рейтинг предмета продолжали его считать.
    """
    import utils as _u
    return _u.deadline_active(expires_at)


def subject_students(subject_id: int) -> list:
    """Ученики предмета прямо сейчас: [{tg_id, user_id, via, access_start, access_until}].

    via: 'subject' — доступ выдан на предмет, 'premium' — общий Премиум,
    'activity' — без доступа, но занимался (только если контроль не «только Премиум»).
    """
    subj = resolve_subject(subject_id)
    if not subj:
        return []
    sid = subj["id"]
    out = {}
    # 1) доступ, выданный именно на предмет
    for r in db.fetchall(
            "SELECT sa.user_tg_id, sa.granted_at, sa.expires_at, u.id AS user_id "
            "FROM subject_access sa JOIN users u ON u.tg_id = sa.user_tg_id "
            "WHERE sa.subject_id=?", (sid,)):
        if not _active_until(r["expires_at"]):
            continue
        out[r["user_tg_id"]] = {"tg_id": r["user_tg_id"], "user_id": r["user_id"],
                                "via": "subject", "access_start": r["granted_at"],
                                "access_until": r["expires_at"]}
    # 2) общий Премиум — если предмет его принимает
    mode = sc.subject_mode(subj)
    if mode in (sc.OPEN, sc.PREMIUM) and not sc.premium_ignored(subj):
        for r in db.fetchall(
                "SELECT u.tg_id, u.id AS user_id, p.granted_at, p.expires_at "
                "FROM premium_users p JOIN users u ON u.id = p.user_id "
                "WHERE u.tg_id IS NOT NULL"):
            if r["tg_id"] in out or not _active_until(r["expires_at"]):
                continue
            out[r["tg_id"]] = {"tg_id": r["tg_id"], "user_id": r["user_id"],
                               "via": "premium", "access_start": r["granted_at"],
                               "access_until": r["expires_at"]}
    # 3) контроль для всех — добавляем тех, кто занимался предметом без доступа
    if not ss.get_bool("study_premium_only"):
        lids = _lesson_ids(sid)
        if lids:
            ph = ",".join("?" * len(lids))
            for r in db.fetchall(
                    f"SELECT DISTINCT lp.user_tg_id AS tg_id, u.id AS user_id "
                    f"FROM lesson_progress lp JOIN users u ON u.tg_id = lp.user_tg_id "
                    f"WHERE lp.lesson_id IN ({ph})", tuple(lids)):
                if r["tg_id"] not in out:
                    out[r["tg_id"]] = {"tg_id": r["tg_id"], "user_id": r["user_id"],
                                       "via": "activity", "access_start": None,
                                       "access_until": None}
    return list(out.values())


# ---------- Прогресс по предмету ----------

def _lessons(subject_id: int) -> list:
    from webapp import learning as lg
    return [l for l in lg._flatten_subject_lessons_sync(subject_id)
            if (l.get("status") or "open") == "open"]


def _lesson_ids(subject_id: int) -> list:
    return [l["id"] for l in _lessons(subject_id)]


def _min_max(values) -> tuple:
    dts = [d for d in (_parse(v) for v in values) if d]
    return (min(dts) if dts else None, max(dts) if dts else None)


def _activity_bounds(tg_id: int, user_id, lids: list, tids: list) -> tuple:
    """(первое, последнее) учебное действие человека в этом предмете."""
    if not lids:
        return None, None
    ph = ",".join("?" * len(lids))
    firsts, lasts = [], []
    for sql, params in (
        (f"SELECT MIN(created_at) AS a, MAX(created_at) AS b FROM study_events "
         f"WHERE tg_id=? AND lesson_id IN ({ph}) AND event IN ('note_open','hw_start','hw_done')",
         (tg_id, *lids)),
        (f"SELECT MIN(viewed_at) AS a, MAX(viewed_at) AS b FROM lesson_progress "
         f"WHERE user_tg_id=? AND lesson_id IN ({ph})", (tg_id, *lids)),
    ):
        try:
            r = db.fetchone(sql, params)
            if r:
                firsts.append(r["a"]); lasts.append(r["b"])
        except Exception:
            pass
    if tids and user_id:
        tph = ",".join("?" * len(tids))
        try:
            r = db.fetchone(
                f"SELECT MIN(start_time) AS a, MAX(COALESCE(end_time, start_time)) AS b "
                f"FROM test_attempts WHERE user_id=? AND test_id IN ({tph}) "
                f"AND COALESCE(publication_id,0)=0", (user_id, *tids))
            if r:
                firsts.append(r["a"]); lasts.append(r["b"])
        except Exception:
            pass
    first, _ = _min_max(firsts)
    _, last = _min_max(lasts)
    return first, last


def status_for_lag(lag: int) -> str:
    if lag >= ss.get_int("study_lag_far", 6):
        return ST_FAR
    if lag >= ss.get_int("study_lag_behind", 3):
        return ST_BEHIND
    if lag >= ss.get_int("study_lag_slight", 1):
        return ST_SLIGHT
    return ST_OK


def compute_progress(tg_id: int, user_id, subject_id: int, subj_row: dict = None,
                     plan_start=None, access_start=None, state: dict = None) -> dict:
    """Реальный прогресс человека по предмету и его отставание от плана."""
    from webapp import learning as lg
    subj = resolve_subject(subject_id) or {}
    sid = subj.get("id", subject_id)
    lessons = _lessons(sid)
    lids = [l["id"] for l in lessons]
    tids = [l["test_id"] for l in lessons if l.get("test_id")]
    total = len(lessons)
    if state is None:
        state = lg._completion_state_sync(user_id, tg_id)
    pass_pct = subj.get("pass_percent") or 0
    done = sum(1 for l in lessons
               if lg._lesson_completed_sync(user_id, tg_id, l, pass_pct, state))
    opened = sum(1 for l in lessons if l["id"] in state["viewed"] or
                 (l.get("test_id") and l["test_id"] in state["best"]))

    # У копии, переведённой из ярлыка (v69), события до перевода — на уроках оригинала
    legacy = [l["legacy_lesson_id"] for l in lessons if l.get("legacy_lesson_id")]
    first_act, last_act = _activity_bounds(tg_id, user_id, lids + legacy, tids)
    # Начало плана: что раньше — первое действие или выдача доступа. Уже
    # зафиксированное начало вперёд не двигаем (продление Премиума — не старт).
    candidates = [d for d in (_parse(plan_start), first_act, _parse(access_start)) if d]
    start = min(candidates) if candidates else datetime.now(timezone.utc)
    # Предмет не может считаться начатым раньше, чем он вообще появился под
    # контролем: иначе курс, подключённый сегодня, сразу помечал бы всех
    # давних премиум-учеников «сильно отстают» на весь объём предмета.
    tracked_since = _parse((subj_row or {}).get("created_at")
                           or (get(sid) or {}).get("created_at"))
    if tracked_since and start < tracked_since and not first_act:
        start = tracked_since
    days = max(0.0, (datetime.now(timezone.utc) - start).total_seconds() / 86400)
    pace = pace_for(subj_row or get(sid) or {})
    expected = min(total, int(days / 7.0 * pace))
    lag = max(0, expected - done)
    return {
        "lessons_total": total, "lessons_done": done, "lessons_opened": opened,
        "expected_done": expected, "lag": lag, "status": status_for_lag(lag),
        "plan_start": start.isoformat(timespec="seconds"),
        "last_activity_at": last_act.isoformat(timespec="seconds") if last_act else None,
        "pace": pace, "days_on_plan": int(days),
        "percent": round(done * 100 / total) if total else 0,
    }


# ---------- Синхронизация ----------

def sync_subject(subject_id: int) -> dict:
    """Пересобрать список учеников предмета и пересчитать каждому прогресс."""
    ensure_schema()
    subj_row = get(subject_id)
    if not subj_row:
        return {"error": "not_tracked"}
    sid = subj_row["subject_id"]
    students = subject_students(sid)
    existing = {r["user_tg_id"]: dict(r) for r in db.fetchall(
        "SELECT * FROM study_tracking WHERE subject_id=?", (sid,))}
    now = _now_iso()
    added = updated = behind = 0
    seen = set()
    for s in students:
        seen.add(s["tg_id"])
        old = existing.get(s["tg_id"])
        p = compute_progress(s["tg_id"], s["user_id"], sid, subj_row,
                             plan_start=(old or {}).get("plan_start"),
                             access_start=s.get("access_start"))
        if p["status"] in (ST_BEHIND, ST_FAR):
            behind += 1
        if old:
            db.execute(
                "UPDATE study_tracking SET active=1, via=?, plan_start=?, access_until=?, "
                "lessons_total=?, lessons_done=?, lessons_opened=?, expected_done=?, lag=?, "
                "status=?, last_activity_at=?, deactivated_at=NULL, updated_at=? "
                "WHERE id=?",
                (s["via"], p["plan_start"], s.get("access_until"), p["lessons_total"],
                 p["lessons_done"], p["lessons_opened"], p["expected_done"], p["lag"],
                 p["status"], p["last_activity_at"], now, old["id"]))
            updated += 1
        else:
            db.execute(
                "INSERT INTO study_tracking (user_tg_id, subject_id, active, via, plan_start, "
                "access_until, lessons_total, lessons_done, lessons_opened, expected_done, lag, "
                "status, last_activity_at, updated_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
                (s["tg_id"], sid, s["via"], p["plan_start"], s.get("access_until"),
                 p["lessons_total"], p["lessons_done"], p["lessons_opened"],
                 p["expected_done"], p["lag"], p["status"], p["last_activity_at"], now))
            added += 1
    removed = 0
    for tg, old in existing.items():
        if tg not in seen and old.get("active"):
            db.execute("UPDATE study_tracking SET active=0, deactivated_at=?, updated_at=? "
                       "WHERE id=?", (now, now, old["id"]))
            removed += 1
    db.execute("UPDATE study_subjects SET students_count=?, behind_count=?, last_sync_at=? "
               "WHERE subject_id=?", (len(students), behind, now, sid))
    res = {"subject_id": sid, "title": subj_row.get("title"), "students": len(students),
           "added": added, "updated": updated, "removed": removed, "behind": behind}
    log.info("контроль обучения: синхронизация предмета %s «%s»: учеников %d "
             "(новых %d, выбыло %d), отстают от плана %d",
             sid, subj_row.get("title"), len(students), added, removed, behind)
    return res


def sync_all(max_age_seconds: Optional[int] = None) -> list:
    """Все предметы под контролем. С max_age — не чаще, чем раз в N секунд."""
    global _last_sync_at
    if max_age_seconds and time.time() - _last_sync_at < max_age_seconds:
        return []
    ensure_schema()
    migrate_from_campaigns()
    out = []
    for s in tracked():
        try:
            out.append(sync_subject(s["subject_id"]))
        except Exception as e:
            log.warning("синхронизация предмета %s: %s", s.get("subject_id"), e)
    _last_sync_at = time.time()
    return out


# ---------- Что показывать по человеку и по предмету ----------

def user_summary(tg_id: int) -> list:
    """Предметы, по которым человек под контролем, с прогрессом и отставанием."""
    ensure_schema()
    try:
        rows = db.fetchall(
            "SELECT t.*, s.title, ss.pace_per_week FROM study_tracking t "
            "JOIN study_subjects ss ON ss.subject_id = t.subject_id AND ss.enabled=1 "
            "JOIN subjects s ON s.id = t.subject_id "
            "WHERE t.user_tg_id=? AND t.active=1 ORDER BY t.lag DESC, s.title", (tg_id,))
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["pace"] = pace_for(d)
        d["percent"] = round(d["lessons_done"] * 100 / d["lessons_total"]) if d["lessons_total"] else 0
        d["status_title"] = STATUS_TITLES.get(d.get("status") or ST_OK, "")
        out.append(d)
    return out


def worst_of(subjects: list) -> dict:
    """Самое сильное отставание из списка предметов человека."""
    best = {"status": ST_OK, "lag": 0, "title": "", "expected_done": 0,
            "lessons_done": 0, "lessons_total": 0}
    for s in subjects or []:
        if _SEVERITY.get(s.get("status"), 0) > _SEVERITY.get(best["status"], 0) or (
                s.get("status") == best["status"] and (s.get("lag") or 0) > best["lag"]):
            best = {"status": s.get("status") or ST_OK, "lag": s.get("lag") or 0,
                    "title": s.get("title") or "", "expected_done": s.get("expected_done") or 0,
                    "lessons_done": s.get("lessons_done") or 0,
                    "lessons_total": s.get("lessons_total") or 0}
    return best


def students_of(subject_id: int, only_behind: bool = False) -> list:
    ensure_schema()
    sql = ("SELECT t.*, u.username, u.first_name FROM study_tracking t "
           "LEFT JOIN users u ON u.tg_id = t.user_tg_id "
           "WHERE t.subject_id=? AND t.active=1 ")
    if only_behind:
        sql += "AND t.status IN ('behind','far') "
    sql += "ORDER BY t.lag DESC, t.lessons_done ASC"
    return [dict(r) for r in db.fetchall(sql, (sc.orig_subject_id(int(subject_id)),))]


def tracked_tg_ids() -> list:
    ensure_schema()
    try:
        rows = db.fetchall(
            "SELECT DISTINCT t.user_tg_id FROM study_tracking t "
            "JOIN study_subjects ss ON ss.subject_id = t.subject_id AND ss.enabled=1 "
            "WHERE t.active=1")
        return [r["user_tg_id"] for r in rows]
    except Exception:
        return []


def totals() -> dict:
    ensure_schema()
    subjects = tracked()
    ids = tracked_tg_ids()
    behind = 0
    try:
        behind = db.fetchone(
            "SELECT COUNT(DISTINCT t.user_tg_id) AS c FROM study_tracking t "
            "JOIN study_subjects ss ON ss.subject_id = t.subject_id AND ss.enabled=1 "
            "WHERE t.active=1 AND t.status IN ('behind','far')")["c"]
    except Exception:
        pass
    return {"subjects": len(subjects), "students": len(ids), "behind": behind}


STATUS_TITLES = {
    ST_OK: "🟢 по плану",
    ST_SLIGHT: "🟡 чуть отстаёт",
    ST_BEHIND: "🟠 отстаёт",
    ST_FAR: "🔴 сильно отстаёт",
}
