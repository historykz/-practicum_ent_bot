"""
Денежные вознаграждения, штрафы, рейтинг участников и итоговая грамота —
отдельно по каждому предмету.

Центральная точка — кнопка «✨ НАЧАТЬ ОБУЧЕНИЕ ✨» на странице предмета
(start()). Одно нажатие:
  • фиксирует дату и время начала (subject_participants.started_at);
  • делает ученика участником рейтинга предмета (одна запись на пару
    «ученик + предмет», повторное нажатие ничего не дублирует);
  • сохраняет весь старый прогресс: пройденные уроки, баллы, достижения
    остаются как были, в рейтинг он встаёт сразу со своими баллами;
  • запускает вознаграждения за уроки и контроль пропусков.

Деньги хранятся не одним числом, а операциями (reward_transactions):
reward, penalty, bonus, manual_adjustment, correction. Баланс — их сумма.
Суммы — в тиынах (1 ₸ = 100), чтобы штраф 62,50 ₸ считался точно.
Уникальный ref операции не даёт дважды оплатить урок или дважды
оштрафовать за один день — ни при двойном клике, ни при повторе запроса,
ни при повторном проходе фоновой задачи.

Дни считаются по Астане (UTC+5).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import database as db
import utils
from webapp import shortcuts as sc

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))
PENALTY_BASE = 5000                   # 50 ₸ — штраф за 3-й день подряд
PENALTY_GROWTH = Decimal("1.25")      # каждый следующий день +25%
GRACE_DAYS = 2                        # первые два дня без штрафа
PENALTY_CAP = 10 ** 15                # техническая граница целого числа в SQLite
WARN_FROM_HOUR, WARN_TO_HOUR = 10, 21 # предупреждения — днём по Астане

TYPES = ("reward", "penalty", "bonus", "manual_adjustment", "correction")
TYPE_TITLES = {
    "reward": "Вознаграждение за урок",
    "penalty": "Штраф за пропуск",
    "bonus": "Бонус",
    "manual_adjustment": "Корректировка системы",
    "correction": "Корректировка",
}
STATUS_TITLES = {"active": "учится", "completed": "курс завершён", "annulled": "аннулировано"}


# ───────────────────────── время и деньги ─────────────────────────

def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def local_dt(value) -> Optional[datetime]:
    dt = utils.parse_utc_naive(value)
    return dt.replace(tzinfo=timezone.utc).astimezone(ALMATY) if dt else None


def local_date(value) -> Optional[date]:
    d = local_dt(value)
    return d.date() if d else None


def today_local(now: datetime = None) -> date:
    return (now or _utcnow()).replace(tzinfo=timezone.utc).astimezone(ALMATY).date()


def fmt_date(value) -> str:
    d = local_dt(value)
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_dt(value) -> str:
    d = local_dt(value)
    return d.strftime("%d.%m.%Y %H:%M") if d else "—"


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")


def fmt_day_words(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def to_tiyn(value) -> int:
    """«70», «62,5», «1 250» → тиыны. ValueError — если не число или меньше нуля."""
    s = str(value if value is not None else "").strip().replace(" ", "").replace(" ", "")
    s = s.replace(",", ".").replace("₸", "")
    if not s:
        return 0
    v = (Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if v < 0:
        raise ValueError("сумма не может быть отрицательной")
    return int(v)


def fmt(tiyn, sign: bool = False) -> str:
    """Тиыны → «3 950 ₸», «62,50 ₸», «−150 ₸», «+70 ₸»."""
    tiyn = int(tiyn or 0)
    whole, frac = divmod(abs(tiyn), 100)
    s = f"{whole:,}".replace(",", " ")
    if frac:
        s += f",{frac:02d}"
    prefix = "−" if tiyn < 0 else ("+" if sign and tiyn > 0 else "")
    return f"{prefix}{s} ₸"


def penalty_amount(day_in_row: int) -> int:
    """Штраф за N-й день пропуска подряд (в тиынах). Дни 1–2 — ноль,
    3-й — 50 ₸, дальше каждый на 25% больше предыдущего, без потолка."""
    if day_in_row <= GRACE_DAYS:
        return 0
    v = (Decimal(PENALTY_BASE) * (PENALTY_GROWTH ** (day_in_row - GRACE_DAYS - 1))
         ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(min(v, PENALTY_CAP))


# ───────────────────────── предмет и его настройки ─────────────────────────

def real_subject_id(subject_id) -> int:
    return int(sc.orig_subject_id(int(subject_id)))


def subject_row(subject_id) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM subjects WHERE id=?", (real_subject_id(subject_id),))
    return dict(row) if row else None


def is_enabled(subject_id) -> bool:
    s = subject_row(subject_id)
    return bool(s and s.get("rewards_enabled"))


def _lessons(subject_id) -> list:
    from services import gamification as gm
    return gm.subject_lessons(real_subject_id(subject_id))


def priced_lessons_count(subject_id) -> int:
    ids = [l["id"] for l in _lessons(subject_id)]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    return db.fetchone(f"SELECT COUNT(*) AS c FROM lessons WHERE id IN ({ph}) "
                       f"AND COALESCE(reward_price,0)>0", tuple(ids))["c"]


def lesson_price(lesson_id) -> int:
    row = db.fetchone("SELECT reward_price FROM lessons WHERE id=?",
                      (sc.orig_lesson_id(int(lesson_id)),))
    return int((row or {}).get("reward_price") or 0)


def set_enabled(subject_id, on: bool, now: datetime = None) -> tuple:
    """Включить/выключить систему. Включить можно только после цен уроков."""
    real = real_subject_id(subject_id)
    now = now or _utcnow()
    if on and priced_lessons_count(real) == 0:
        return False, "Сначала задайте цену хотя бы одному уроку предмета."
    if on:
        was = is_enabled(real)
        if was:
            return True, "Система вознаграждений уже включена."
        # Дата включения — начало отсчёта пропусков. Меняем её только при
        # настоящем включении: повторный клик не должен прощать идущие серии.
        db.execute("UPDATE subjects SET rewards_enabled=1, rewards_enabled_at=? WHERE id=?",
                   (_iso(now), real))
        if not was:
            # Уроки, пройденные пока система была выключена, не оплачиваются
            # задним числом (если админ не разрешил это отдельно), а дни до
            # включения не штрафуются — отсчёт идёт с момента включения.
            subj = subject_row(real)
            for p in participants(real, active_only=True):
                if not (subj or {}).get("reward_pay_prior"):
                    done = set(completed_lesson_ids(p["tg_id"], p["user_id"], real))
                    prior = set(_json_list(p.get("prior_lessons"))) | done
                    db.execute("UPDATE subject_participants SET prior_lessons=? WHERE id=?",
                               (json.dumps(sorted(prior)), p["id"]))
        _invalidate()
        return True, "Система вознаграждений включена."
    db.execute("UPDATE subjects SET rewards_enabled=0 WHERE id=?", (real,))
    _invalidate()
    return True, "Система вознаграждений выключена — ученики её больше не видят."


def _invalidate() -> None:
    """Настройки уроков поменялись — сбросить кэши, чтобы ученики увидели сразу."""
    try:
        from webapp import learning as lg
        lg.invalidate_catalog_cache()
    except Exception:
        pass
    try:
        from services import gamification as gm
        gm.invalidate()
    except Exception:
        pass


def _required_ids(ids: list) -> list:
    """Обязательные для завершения уроки — прямо из базы. Список уроков
    предмета кэшируется на 30 секунд, а галочку «обязательный» админ мог
    поменять только что: из кэша курс не завершился бы, хотя всё сдано."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    keep = {r["id"] for r in db.fetchall(
        f"SELECT id FROM lessons WHERE id IN ({ph}) AND COALESCE(reward_required,1)=1", tuple(ids))}
    return [i for i in ids if i in keep]


def set_pay_prior(subject_id, on: bool) -> None:
    db.execute("UPDATE subjects SET reward_pay_prior=? WHERE id=?",
               (1 if on else 0, real_subject_id(subject_id)))
    _invalidate()


def set_price_lesson(lesson_id, tiyn: int) -> None:
    db.execute("UPDATE lessons SET reward_price=? WHERE id=?",
               (max(0, int(tiyn)), sc.orig_lesson_id(int(lesson_id))))
    _invalidate()


def _section_lesson_ids(section_id) -> list:
    rows = db.fetchall("SELECT id FROM lessons WHERE section_id=?",
                       (sc.orig_section_id(int(section_id)),))
    return sorted({sc.orig_lesson_id(r["id"]) for r in rows})


def set_price_section(section_id, tiyn: int) -> int:
    ids = _section_lesson_ids(section_id)
    for lid in ids:
        db.execute("UPDATE lessons SET reward_price=? WHERE id=?", (max(0, int(tiyn)), lid))
    _invalidate()
    return len(ids)


def set_price_all(subject_id, tiyn: int) -> int:
    ids = sorted({l["id"] for l in _lessons(subject_id)})
    for lid in ids:
        db.execute("UPDATE lessons SET reward_price=? WHERE id=?", (max(0, int(tiyn)), lid))
    _invalidate()
    return len(ids)


def set_required_lesson(lesson_id, required: bool) -> None:
    db.execute("UPDATE lessons SET reward_required=? WHERE id=?",
               (1 if required else 0, sc.orig_lesson_id(int(lesson_id))))
    _invalidate()


def set_required_section(section_id, required: bool) -> int:
    ids = _section_lesson_ids(section_id)
    for lid in ids:
        db.execute("UPDATE lessons SET reward_required=? WHERE id=?", (1 if required else 0, lid))
    _invalidate()
    return len(ids)


# ───────────────────────── прогресс ученика ─────────────────────────

def _json_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
        return [int(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def completed_lesson_ids(tg_id, user_id, subject_id) -> list:
    """Уроки предмета, пройденные по тем же правилам, что и везде на платформе:
    тест сдан на порог предмета, зачёт сдан, урок без теста прочитан."""
    from webapp import learning as lg
    real = real_subject_id(subject_id)
    subj = subject_row(real) or {}
    state = lg._completion_state_sync(user_id, tg_id)
    pass_pct = subj.get("pass_percent") or 0
    return [l["id"] for l in _lessons(real)
            if lg._lesson_completed_sync(user_id, tg_id, l, pass_pct, state)]


def activity(tg_id, user_id, subject_id) -> tuple:
    """(множество дней по Астане с настоящей учёбой, время последней учёбы).

    Учёба — завершённый тест предмета, сданный зачёт, прочитанный урок без
    теста, сданное ДЗ. Просто открыть приложение или бота — не учёба.
    """
    lessons = _lessons(subject_id)
    lids = [l["id"] for l in lessons]
    tids = sorted({l["test_id"] for l in lessons if l.get("test_id")})
    zids = [l["id"] for l in lessons if l.get("is_zachet")]
    nids = [l["id"] for l in lessons if not l.get("test_id") and not l.get("is_zachet")]
    stamps = []

    def grab(sql, ids, *head):
        if not ids:
            return
        ph = ",".join("?" * len(ids))
        try:
            for r in db.fetchall(sql.format(ph=ph), tuple(head) + tuple(ids)):
                if r["t"]:
                    stamps.append(r["t"])
        except Exception as e:
            log.debug("activity: %s", e)

    if user_id:
        grab("SELECT COALESCE(end_time, start_time) AS t FROM test_attempts WHERE user_id=? "
             "AND status='finished' AND COALESCE(publication_id,0)=0 AND test_id IN ({ph})",
             tids, user_id)
    grab("SELECT finished_at AS t FROM zachet_attempts WHERE user_tg_id=? "
         "AND status='finished' AND lesson_id IN ({ph})", zids, tg_id)
    grab("SELECT viewed_at AS t FROM lesson_progress WHERE user_tg_id=? AND lesson_id IN ({ph})",
         nids, tg_id)
    grab("SELECT created_at AS t FROM study_events WHERE tg_id=? AND event='hw_done' "
         "AND lesson_id IN ({ph})", lids, tg_id)
    days, last = set(), None
    for s in stamps:
        dt = utils.parse_utc_naive(s)
        if not dt:
            continue
        days.add(dt.replace(tzinfo=timezone.utc).astimezone(ALMATY).date())
        if last is None or dt > last:
            last = dt
    return days, last


def _homework_done(user_id, subject_id) -> int:
    tids = sorted({l["test_id"] for l in _lessons(subject_id) if l.get("test_id")})
    if not tids or not user_id:
        return 0
    ph = ",".join("?" * len(tids))
    return db.fetchone(
        f"SELECT COUNT(DISTINCT test_id) AS c FROM test_attempts WHERE user_id=? "
        f"AND status='finished' AND COALESCE(publication_id,0)=0 "
        f"AND COALESCE(attempt_num,1)<>999 AND test_id IN ({ph})",
        (user_id, *tids))["c"]


# ───────────────────────── участники ─────────────────────────

def get_participant(user_id, subject_id) -> Optional[dict]:
    if not user_id:
        return None
    row = db.fetchone("SELECT * FROM subject_participants WHERE user_id=? AND subject_id=?",
                      (int(user_id), real_subject_id(subject_id)))
    return dict(row) if row else None


def participant_by_id(pid) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM subject_participants WHERE id=?", (int(pid),))
    return dict(row) if row else None


def participants(subject_id, active_only: bool = False) -> list:
    sql = "SELECT * FROM subject_participants WHERE subject_id=?"
    if active_only:
        sql += " AND is_active=1"
    return [dict(r) for r in db.fetchall(sql + " ORDER BY id", (real_subject_id(subject_id),))]


def user_participations(tg_id) -> list:
    rows = db.fetchall(
        "SELECT p.*, s.title AS subject_title FROM subject_participants p "
        "LEFT JOIN subjects s ON s.id=p.subject_id WHERE p.tg_id=? ORDER BY p.id", (int(tg_id),))
    return [dict(r) for r in rows]


def _first_activity(tg_id, user_id, subject_id) -> Optional[str]:
    days, _ = activity(tg_id, user_id, subject_id)
    if not days:
        return None
    d = min(days)
    return _iso(datetime(d.year, d.month, d.day) - timedelta(hours=5))


def start(tg_id: int, user_id: int, subject_id: int, now: datetime = None) -> dict:
    """«✨ НАЧАТЬ ОБУЧЕНИЕ ✨». Повторное нажатие ничего не создаёт и не сбрасывает."""
    from services import gamification as gm
    now = now or _utcnow()
    real = real_subject_id(subject_id)
    if not gm.has_access(tg_id, user_id, real):
        return {"ok": False, "error": "no_access"}
    p = get_participant(user_id, real)
    if p and p["is_active"]:
        return {"ok": True, "result": "already", "participant": p}
    done = completed_lesson_ids(tg_id, user_id, real)
    first = _first_activity(tg_id, user_id, real)
    stamp = _iso(now)
    if p is None:
        db.execute(
            "INSERT OR IGNORE INTO subject_participants (user_id, tg_id, subject_id, status, "
            "is_active, started_at, first_started_at, joined_rating_at, prior_lessons, "
            "prior_first_activity_at, lessons_completed, last_updated_at) "
            "VALUES (?,?,?,'active',1,?,?,?,?,?,?,?)",
            (user_id, tg_id, real, stamp, stamp, stamp, json.dumps(sorted(done)), first,
             len(done), stamp))
        result = "created"
    else:
        # Вернулся после аннулирования (Премиум закончился и снова куплен):
        # прошлые начисления не возвращаются, отсчёт — заново с этого момента.
        db.execute(
            "UPDATE subject_participants SET status='active', is_active=1, started_at=?, "
            "joined_rating_at=?, prior_lessons=?, annulled_at=NULL, annul_reason=NULL, "
            "completed_at=NULL, completion_rank=NULL, last_updated_at=? WHERE id=?",
            (stamp, stamp, json.dumps(sorted(done)), stamp, p["id"]))
        result = "reactivated"
    p = get_participant(user_id, real)
    reconcile(p, now)
    p = refresh(p, now)
    return {"ok": True, "result": result, "participant": p}


def _insert_tx(p: dict, kind: str, amount: int, ref: str, lesson_id=None, reason: str = "",
               meta: dict = None, created_by=None, now: datetime = None) -> Optional[dict]:
    assert kind in TYPES
    stamp = _iso(now or _utcnow())
    cur = db.execute(
        "INSERT OR IGNORE INTO reward_transactions (user_id, tg_id, subject_id, lesson_id, type, "
        "amount, ref, reason, meta, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (p["user_id"], p["tg_id"], p["subject_id"], lesson_id, kind, int(amount), ref,
         reason or "", json.dumps(meta or {}, ensure_ascii=False), created_by, stamp))
    if not cur.rowcount:
        return None
    return {"type": kind, "amount": int(amount), "ref": ref, "reason": reason,
            "lesson_id": lesson_id, "created_at": stamp}


def reconcile(p: dict, now: datetime = None) -> list:
    """Начислить за уроки, пройденные после старта и ещё не оплаченные.

    Цена берётся в момент прохождения и дальше не пересчитывается: урок
    получает ровно одну запись — даже с нулевой суммой, если цены у него
    тогда не было (поднятая потом цена прошлые уроки не оплачивает).
    """
    if not p or not p.get("is_active") or p.get("status") == "annulled":
        return []
    subj = subject_row(p["subject_id"])
    if not subj or not subj.get("rewards_enabled"):
        return []
    prior = set() if subj.get("reward_pay_prior") else set(_json_list(p.get("prior_lessons")))
    titles = {l["id"]: l.get("title") or "" for l in _lessons(p["subject_id"])}
    # Уже учтённые уроки — одним запросом. Раньше фоновая сверка раз в 5 минут
    # пыталась заново записать начисление за КАЖДЫЙ пройденный урок каждого
    # участника: тысячи лишних захватов записи в SQLite на каждом проходе.
    done_refs = {r["ref"] for r in db.fetchall(
        "SELECT ref FROM reward_transactions WHERE user_id=? AND subject_id=? AND type='reward'",
        (p["user_id"], p["subject_id"]))}
    todo = [lid for lid in completed_lesson_ids(p["tg_id"], p["user_id"], p["subject_id"])
            if lid not in prior and f"lesson:{lid}" not in done_refs]
    if not todo:
        return []
    ph = ",".join("?" * len(todo))
    prices = {r["id"]: int(r["reward_price"] or 0) for r in db.fetchall(
        f"SELECT id, reward_price FROM lessons WHERE id IN ({ph})", tuple(todo))}
    new = []
    for lid in todo:
        price = prices.get(lid, 0)
        tx = _insert_tx(p, "reward", price, f"lesson:{lid}", lesson_id=lid,
                        reason=f"Урок «{titles.get(lid, lid)}»", now=now)
        if tx and price > 0:
            new.append(tx)
            queue(p["tg_id"], f"💰 Вам начислено {fmt(price, sign=True)} — урок "
                              f"«{titles.get(lid, '')}» ({subj['title']})",
                  dedup=f"reward:{p['id']}:{lid}", now=now)
    return new


def money(p: dict) -> dict:
    rows = db.fetchall("SELECT type, COALESCE(SUM(amount),0) AS s FROM reward_transactions "
                       "WHERE user_id=? AND subject_id=? GROUP BY type",
                       (p["user_id"], p["subject_id"]))
    by = {r["type"]: int(r["s"]) for r in rows}
    earned = by.get("reward", 0)
    penalties = by.get("penalty", 0)
    adjustments = by.get("bonus", 0) + by.get("manual_adjustment", 0) + by.get("correction", 0)
    balance = earned + penalties + adjustments
    return {"earned": earned, "penalties": penalties, "adjustments": adjustments,
            "balance": balance, "payout": max(0, balance)}


def refresh(p: dict, now: datetime = None, rank: bool = True) -> dict:
    """Пересчитать участника: уроки, ДЗ, баллы, деньги, дни, завершение курса."""
    from services import gamification as gm
    now = now or _utcnow()
    if not p:
        return p
    sid, uid, tg = p["subject_id"], p["user_id"], p["tg_id"]
    subj = subject_row(sid) or {}
    lessons = _lessons(sid)
    done = set(completed_lesson_ids(tg, uid, sid))
    required = _required_ids([l["id"] for l in lessons])
    days, last = activity(tg, uid, sid)
    start_d = local_date(p["started_at"])
    active_days = len([d for d in days if start_d and d >= start_d])
    m = money(p)
    fields = {
        "lessons_completed": len(done), "lessons_total": len(lessons),
        "homework_completed": _homework_done(uid, sid),
        "total_points": int(gm.points(uid, sid)),
        "achievements": len(gm.earned(uid)),
        "earned": m["earned"], "penalties": m["penalties"], "adjustments": m["adjustments"],
        "balance": m["balance"], "learning_days": active_days,
        "last_activity_at": _iso(last) if last else p.get("last_activity_at"),
        "last_updated_at": _iso(now),
    }
    completed_now = (p["status"] == "active" and p["is_active"] and subj.get("rewards_enabled")
                     and required and all(r in done for r in required))
    if completed_now:
        fields["status"] = "completed"
        fields["completed_at"] = _iso(now)
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE subject_participants SET {sets} WHERE id=?",
               tuple(fields.values()) + (p["id"],))
    if rank:
        rank_subject(sid)
    p = participant_by_id(p["id"])
    if completed_now:
        db.execute("UPDATE subject_participants SET completion_rank=? WHERE id=?",
                   (p["current_rank"], p["id"]))
        p = participant_by_id(p["id"])
        queue(tg, f"🎉 Поздравляем! Курс «{subj.get('title', '')}» завершён.\n"
                  f"Итоговое вознаграждение: {fmt(max(0, p['balance']))}. "
                  f"Заберите грамоту на странице предмета.",
              dedup=f"completed:{p['id']}:{p['completed_at']}", now=now)
    return p


def rank_subject(subject_id) -> None:
    """Места в рейтинге предмета: только участники, спортивная система
    (одинаковые баллы — одно место)."""
    from services import gamification as gm
    real = real_subject_id(subject_id)
    ps = participants(real, active_only=True)
    pts = gm._points_by_user(real)
    scored = [(p, int(pts.get(p["user_id"], 0))) for p in ps]
    total = len(scored)
    ordered = sorted((s for _p, s in scored), reverse=True)
    first_pos = {}
    for i, s in enumerate(ordered, start=1):
        first_pos.setdefault(s, i)                   # спортивное место: первое вхождение
    updates = []
    for p, mine in scored:
        place = first_pos[mine]
        # Пишем только изменившиеся строки: после сдачи одного теста обычно
        # меняются места единиц, а не всех участников предмета.
        if (p["total_points"], p["current_rank"], p["rank_total"]) != (mine, place, total):
            updates.append((mine, place, total, p["id"]))
    if updates:
        db.executemany("UPDATE subject_participants SET total_points=?, current_rank=?, "
                       "rank_total=? WHERE id=?", updates)
    db.execute("UPDATE subject_participants SET current_rank=0, rank_total=? "
               "WHERE subject_id=? AND is_active=0 AND (current_rank<>0 OR rank_total<>?)",
               (total, real, total))
    gm.invalidate()


def subjects_for(lesson_id=None, test_id=None) -> list:
    """Предметы-оригиналы, в которых лежит этот урок или тест."""
    lids = set()
    if lesson_id:
        lids.add(int(lesson_id))
        lids.add(int(sc.orig_lesson_id(int(lesson_id))))
    if test_id:
        lids |= {r["id"] for r in db.fetchall("SELECT id FROM lessons WHERE test_id=?", (int(test_id),))}
    if not lids:
        return []
    ph = ",".join("?" * len(lids))
    rows = db.fetchall(
        f"SELECT DISTINCT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
        f"WHERE l.id IN ({ph}) OR l.original_id IN ({ph})", tuple(lids) * 2)
    return sorted({real_subject_id(r["subject_id"]) for r in rows})


def on_learning_event(tg_id, user_id=None, lesson_id=None, test_id=None,
                      now: datetime = None) -> None:
    """Урок, ДЗ или зачёт завершены — начислить и пересчитать. Не бросает исключений:
    учебный процесс не должен падать из-за денежной части."""
    try:
        from services import gamification as gm
        if not user_id:
            u = utils.get_user_by_tg(tg_id)
            user_id = u["id"] if u else None
        if not user_id:
            return
        gm.invalidate()
        for sid in subjects_for(lesson_id, test_id):
            p = get_participant(user_id, sid)
            if p and p["is_active"]:
                reconcile(p, now)
                refresh(p, now)
    except Exception as e:
        log.exception("вознаграждение: событие tg=%s урок=%s тест=%s: %s", tg_id, lesson_id, test_id, e)


# ───────────────────────── пропуски и штрафы ─────────────────────────

def _anchor(p: dict, subj: dict) -> Optional[date]:
    """С какого дня идёт отсчёт пропусков: день старта или включения системы."""
    days = [d for d in (local_date(p.get("started_at")), local_date(subj.get("rewards_enabled_at"))) if d]
    return max(days) if days else None


def absence_streak(days: set, anchor: date, until: date) -> int:
    """Сколько дней подряд без учёбы, считая назад от until включительно."""
    n, d = 0, until
    while d > anchor and d not in days:
        n += 1
        d -= timedelta(days=1)
    return n


def penalize(p: dict, now: datetime = None) -> list:
    """Штрафы за все завершившиеся дни пропуска, которых ещё нет (догоняет и после простоя)."""
    now = now or _utcnow()
    if not p or p.get("status") != "active" or not p.get("is_active"):
        return []
    subj = subject_row(p["subject_id"]) or {}
    if not subj.get("rewards_enabled"):
        return []
    anchor = _anchor(p, subj)
    end = today_local(now) - timedelta(days=1)          # только прошедшие дни
    if not anchor or end <= anchor:
        return []
    days, _ = activity(p["tg_id"], p["user_id"], p["subject_id"])
    new, streak, d = [], 0, anchor + timedelta(days=1)
    while d <= end:
        if d in days:
            streak = 0
        else:
            streak += 1
            amount = penalty_amount(streak)
            if amount:
                tx = _insert_tx(p, "penalty", -amount, f"absence:{d.isoformat()}",
                                reason=f"{streak}-й день отсутствия",
                                meta={"day": streak, "date": d.isoformat()}, now=now)
                if tx:
                    new.append(tx)
                    queue(p["tg_id"], f"⚠️ Начислен штраф {fmt(-amount)} — {streak}-й день без "
                                      f"занятий по предмету «{subj['title']}» ({fmt_day_words(d)}).",
                          dedup=f"penalty:{p['id']}:{d.isoformat()}", now=now)
        d += timedelta(days=1)
    if new:
        p = refresh(p, now, rank=False)
        if p["balance"] < 0:
            queue(p["tg_id"], f"🔴 Баланс по предмету «{subj['title']}»: {fmt(p['balance'])}.\n"
                              f"Это не долг — просто продолжайте учиться, новые начисления перекроют минус.",
                  dedup=f"negative:{p['id']}:{today_local(now).isoformat()}", now=now)
    return new


def warn(p: dict, now: datetime = None) -> bool:
    """«Вы не обучались уже 2 дня» — утром третьего дня, один раз."""
    now = now or _utcnow()
    if not p or p.get("status") != "active" or not p.get("is_active"):
        return False
    subj = subject_row(p["subject_id"]) or {}
    if not subj.get("rewards_enabled"):
        return False
    hour = now.replace(tzinfo=timezone.utc).astimezone(ALMATY).hour
    if not (WARN_FROM_HOUR <= hour <= WARN_TO_HOUR):
        return False
    today = today_local(now)
    anchor = _anchor(p, subj)
    if not anchor:
        return False
    days, _ = activity(p["tg_id"], p["user_id"], p["subject_id"])
    if today in days:
        return False
    if absence_streak(days, anchor, today - timedelta(days=1)) != GRACE_DAYS:
        return False
    return queue(p["tg_id"], f"⚠️ Вы не обучались уже {GRACE_DAYS} дня по предмету «{subj['title']}». "
                             f"Вернитесь сегодня, чтобы избежать штрафа {fmt(PENALTY_BASE)}.",
                 dedup=f"warn:{p['id']}:{today.isoformat()}", now=now)


# ───────────────────────── потеря доступа ─────────────────────────

def _lost_reason(tg_id, user_id, subject_id) -> str:
    row = db.fetchone("SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,))
    if row is None:
        return "Премиум отозван администратором"
    if not utils.deadline_active(row["expires_at"]):
        return "срок Премиума закончился"
    return "доступ к предмету закончился"


def has_certificate(user_id, subject_id) -> bool:
    return db.fetchone("SELECT id FROM reward_certificates WHERE user_id=? AND subject_id=? "
                       "AND revoked=0", (user_id, real_subject_id(subject_id))) is not None


def annul(p: dict, reason: str, now: datetime = None) -> dict:
    """Премиум закончился или отозван: вознаграждение аннулируется (баланс в ноль
    одной операцией), ученик выходит из рейтинга. История операций остаётся."""
    now = now or _utcnow()
    m = money(p)
    if m["balance"]:
        _insert_tx(p, "manual_adjustment", -m["balance"], f"annul:{_iso(now)}",
                   reason=f"Аннулировано: {reason}", now=now)
    db.execute("UPDATE subject_participants SET status='annulled', is_active=0, annulled_at=?, "
               "annul_reason=?, current_rank=0, last_updated_at=? WHERE id=?",
               (_iso(now), reason, _iso(now), p["id"]))
    p = refresh(participant_by_id(p["id"]), now, rank=False)
    rank_subject(p["subject_id"])
    subj = subject_row(p["subject_id"]) or {}
    queue(p["tg_id"], f"⛔️ {reason[:1].upper() + reason[1:]}: вознаграждение по предмету "
                      f"«{subj.get('title', '')}» аннулировано, вы исключены из рейтинга предмета.\n"
                      f"Пройденные уроки и баллы сохранены. Чтобы вернуться, откройте доступ и снова "
                      f"нажмите «Начать обучение».",
          dedup=f"annul:{p['id']}:{p['annulled_at']}", now=now)
    return p


def check_access(p: dict, now: datetime = None) -> bool:
    """True — если участник аннулирован сейчас."""
    from services import gamification as gm
    if not p or not p.get("is_active"):
        return False
    if has_certificate(p["user_id"], p["subject_id"]):
        return False                     # курс закрыт грамотой — итог окончательный
    if gm.has_access(p["tg_id"], p["user_id"], p["subject_id"]):
        return False
    annul(p, _lost_reason(p["tg_id"], p["user_id"], p["subject_id"]), now)
    return True


def check_user(tg_id, now: datetime = None) -> int:
    """Сразу после отзыва Премиума админом — не ждать фоновой проверки."""
    n = 0
    for p in user_participations(tg_id):
        try:
            n += 1 if check_access(p, now) else 0
        except Exception as e:
            log.warning("проверка доступа участника %s: %s", p.get("id"), e)
    return n


# ───────────────────────── грамота ─────────────────────────

_TRANSLIT = {"А": "A", "Ә": "A", "Б": "B", "В": "V", "Г": "G", "Ғ": "G", "Д": "D", "Е": "E",
             "Ё": "E", "Ж": "Z", "З": "Z", "И": "I", "Й": "I", "К": "K", "Қ": "K", "Л": "L",
             "М": "M", "Н": "N", "Ң": "N", "О": "O", "Ө": "O", "П": "P", "Р": "R", "С": "S",
             "Т": "T", "У": "U", "Ұ": "U", "Ү": "U", "Ф": "F", "Х": "H", "Һ": "H", "Ц": "C",
             "Ч": "C", "Ш": "S", "Щ": "S", "Ы": "Y", "І": "I", "Э": "E", "Ю": "U", "Я": "Y"}


def serial_prefix(title: str) -> str:
    words = [w for w in "".join(ch if ch.isalpha() else " " for ch in (title or "")).split() if w]
    letters = ""
    for w in words[:2]:
        ch = w[0].upper()
        letters += _TRANSLIT.get(ch, ch if "A" <= ch <= "Z" else "")
    return (letters or "SE")[:3]


def make_serial(title: str, year: int) -> str:
    prefix = serial_prefix(title)
    for _ in range(50):
        serial = f"{prefix}-{year}-{random.randint(0, 99_999_999):08d}"
        if not db.fetchone("SELECT id FROM reward_certificates WHERE serial=?", (serial,)):
            return serial
    raise RuntimeError("не удалось подобрать уникальный номер грамоты")


def get_certificate(user_id, subject_id) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM reward_certificates WHERE user_id=? AND subject_id=?",
                      (user_id, real_subject_id(subject_id)))
    return dict(row) if row else None


def completion_summary(p: dict) -> dict:
    s, c = local_date(p.get("started_at")), local_date(p.get("completed_at"))
    m = money(p)
    return {
        "started": fmt_date(p.get("started_at")), "completed": fmt_date(p.get("completed_at")),
        "days": ((c - s).days + 1) if (s and c) else 0,
        "lessons": p.get("lessons_completed") or 0, "lessons_total": p.get("lessons_total") or 0,
        "earned": m["earned"], "penalties": m["penalties"], "adjustments": m["adjustments"],
        "balance": m["balance"], "payout": m["payout"],
        "rank": p.get("completion_rank") or p.get("current_rank") or 0,
        "rank_total": p.get("rank_total") or 0,
    }


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").replace("\n", " ").split())
    if not (3 <= len(name) <= 80):
        raise ValueError("Укажите имя и фамилию полностью (от 3 до 80 символов).")
    if not all(ch.isalpha() or ch in " -'.ʼ" for ch in name) or not any(ch.isalpha() for ch in name):
        raise ValueError("В имени можно использовать только буквы, пробел и дефис.")
    return name


def claim(tg_id: int, user_id: int, subject_id: int, full_name: str, now: datetime = None) -> dict:
    """«🏆 Забрать грамоту и вознаграждение». Повторно — та же грамота."""
    from services import gamification as gm
    now = now or _utcnow()
    real = real_subject_id(subject_id)
    existing = get_certificate(user_id, real)
    if existing:
        return {"ok": True, "certificate": existing, "result": "already"}
    p = get_participant(user_id, real)
    if not p or p["status"] != "completed" or not p["is_active"]:
        return {"ok": False, "error": "not_completed"}
    if not is_enabled(real):
        return {"ok": False, "error": "disabled"}
    if not gm.has_access(tg_id, user_id, real):
        return {"ok": False, "error": "no_access"}
    try:
        name = clean_name(full_name)
    except ValueError as e:
        return {"ok": False, "error": "bad_name", "message": str(e)}
    p = refresh(p, now)
    summ = completion_summary(p)
    subj = subject_row(real) or {}
    serial = make_serial(subj.get("title", ""), local_date(p["completed_at"]).year)
    db.execute(
        "INSERT OR IGNORE INTO reward_certificates (serial, user_id, tg_id, subject_id, full_name, "
        "subject_title, started_at, completed_at, days, lessons, earned, penalties, adjustments, "
        "payout, rank, rank_total, issued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (serial, user_id, tg_id, real, name, subj.get("title", ""), p["started_at"],
         p["completed_at"], summ["days"], summ["lessons"], summ["earned"], summ["penalties"],
         summ["adjustments"], summ["payout"], summ["rank"], summ["rank_total"], _iso(now)))
    cert = get_certificate(user_id, real)
    queue(tg_id, f"🏆 Грамота по предмету «{subj.get('title', '')}» готова.\n"
                 f"Серийный номер: {cert['serial']}. Итоговое вознаграждение: {fmt(cert['payout'])}.",
          dedup=f"certificate:{cert['serial']}", now=now)
    return {"ok": True, "certificate": cert, "result": "created"}


def verify(query: str) -> list:
    """Грамоты по серийному номеру или Telegram ID."""
    q = (query or "").strip()
    if not q:
        return []
    if q.isdigit():
        rows = db.fetchall("SELECT * FROM reward_certificates WHERE tg_id=? ORDER BY id", (int(q),))
    else:
        rows = db.fetchall("SELECT * FROM reward_certificates WHERE UPPER(serial)=UPPER(?)", (q,))
    return [dict(r) for r in rows]


def _secret() -> bytes:
    from services import workbook_mark_service as wm
    return hashlib.sha256(b"certificate::" + wm._secret()).digest()


def cert_token(serial: str, tg_id: int, ttl: int = 6 * 3600) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(_secret(), f"{serial}:{int(tg_id)}:{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{int(tg_id)}.{exp}.{sig}"


def parse_cert_token(serial: str, token: str) -> Optional[int]:
    try:
        tg_s, exp_s, sig = (token or "").split(".")
        tg, exp = int(tg_s), int(exp_s)
    except (ValueError, AttributeError):
        return None
    if exp < time.time():
        return None
    good = hmac.new(_secret(), f"{serial}:{tg}:{exp}".encode(), hashlib.sha256).hexdigest()[:24]
    return tg if hmac.compare_digest(good, sig) else None


# ───────────────────────── админ: ручные операции ─────────────────────────

def admin_adjust(p: dict, kind: str, tiyn: int, reason: str, admin_tg: int,
                 now: datetime = None, op_key: str = None) -> dict:
    """+ бонус / − корректировка. Причина обязательна, всё пишется в историю."""
    reason = " ".join((reason or "").split())
    if kind not in ("bonus", "correction"):
        raise ValueError("неизвестный вид операции")
    if len(reason) < 3:
        raise ValueError("Укажите причину — хотя бы несколько слов.")
    tiyn = abs(int(tiyn))
    if tiyn <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    amount = tiyn if kind == "bonus" else -tiyn
    # op_key — один на весь диалог «сумма → причина»: два быстрых сообщения
    # подряд не запишут операцию дважды, второе просто не пройдёт уникальность.
    ref = f"manual:{op_key or uuid.uuid4().hex}"
    tx = _insert_tx(p, kind, amount, ref, reason=reason, created_by=admin_tg, now=now)
    if tx is None:
        raise ValueError("Эта операция уже записана.")
    subj = subject_row(p["subject_id"]) or {}
    label = "🎁 Бонус" if kind == "bonus" else "✏️ Корректировка"
    queue(p["tg_id"], f"{label} {fmt(amount, sign=True)} по предмету «{subj.get('title', '')}».\n"
                      f"Причина: {reason}", dedup=f"manual:{tx['ref']}", now=now)
    refresh(participant_by_id(p["id"]), now, rank=False)
    return tx


def transactions(p: dict, kinds: tuple = None, limit: int = 500) -> list:
    sql = "SELECT * FROM reward_transactions WHERE user_id=? AND subject_id=?"
    args = [p["user_id"], p["subject_id"]]
    if kinds:
        sql += f" AND type IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    rows = db.fetchall(sql + " AND amount<>0 ORDER BY created_at DESC, id DESC LIMIT ?",
                       tuple(args) + (limit,))
    out = []
    for r in rows:
        r = dict(r)
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except ValueError:
            r["meta"] = {}
        r["amount_fmt"] = fmt(r["amount"], sign=True)
        r["type_title"] = TYPE_TITLES.get(r["type"], r["type"])
        day = r["meta"].get("date") if r["type"] == "penalty" else None
        r["when"] = (fmt_day_words(date.fromisoformat(day)) if day else fmt_dt(r["created_at"]))
        out.append(r)
    return out


# ───────────────────────── что видит ученик ─────────────────────────

def subject_view(tg_id, subject_id) -> Optional[dict]:
    """Блоки страницы предмета. None — предмет не открыт (Премиума нет):
    тогда ни денег, ни рейтинга, ни кнопки старта ученик не видит."""
    from services import gamification as gm
    if not tg_id:
        return None
    u = utils.get_user_by_tg(tg_id)
    if not u:
        return None
    real = real_subject_id(subject_id)
    if not gm.has_access(tg_id, u["id"], real):
        return None
    subj = subject_row(real) or {}
    p = get_participant(u["id"], real)
    active = bool(p and p["is_active"])
    view = {"eligible": True, "started": active, "enabled": bool(subj.get("rewards_enabled")),
            "subject_id": real, "status": p["status"] if active else None,
            "participant": p if active else None}
    if active:
        start_d = local_date(p["started_at"])
        view["started_label"] = fmt_date(p["started_at"])
        view["day_n"] = ((today_local() - start_d).days + 1) if start_d else 1
        view["completed"] = p["status"] == "completed"
        view["certificate"] = get_certificate(u["id"], real)
        done = set(completed_lesson_ids(tg_id, u["id"], real))
        nxt = next((l for l in _lessons(real) if l["id"] not in done), None)
        view["next_lesson"] = ({"title": nxt.get("title") or "",
                                "url": f"/learn/lesson/{sc.lesson_url_id(nxt)}"} if nxt else None)
        if view["enabled"]:
            m = money(p)
            view.update({k: m[k] for k in m})
            view.update({f"{k}_fmt": fmt(m[k]) for k in m})
            view["penalties_count"] = db.fetchone(
                "SELECT COUNT(*) AS c FROM reward_transactions WHERE user_id=? AND subject_id=? "
                "AND type='penalty' AND amount<>0", (u["id"], real))["c"]
    return view


# ───────────────────────── уведомления ─────────────────────────

def queue(tg_id, text: str, dedup: str = None, now: datetime = None) -> bool:
    if not tg_id or not text:
        return False
    cur = db.execute("INSERT OR IGNORE INTO reward_notifications (tg_id, text, dedup, created_at) "
                     "VALUES (?,?,?,?)", (int(tg_id), text, dedup, _iso(now or _utcnow())))
    return bool(cur.rowcount)


async def flush(bot, limit: int = 40) -> int:
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
    rows = await asyncio.to_thread(
        db.fetchall, "SELECT * FROM reward_notifications WHERE sent_at IS NULL AND attempts<5 "
                     "ORDER BY id LIMIT ?", (limit,))
    sent = 0
    for r in rows:
        try:
            # Простым текстом: у бота по умолчанию HTML, а в названиях уроков и
            # причинах бывают «<», «>», «&» — Telegram такое сообщение отверг бы.
            await bot.send_message(r["tg_id"], r["text"], parse_mode=None,
                                   disable_web_page_preview=True)
            status = "sent"
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(60, e.retry_after + 1))
            break
        except (TelegramForbiddenError, TelegramBadRequest):
            status = "gone"              # закрыл бота — больше не пытаемся
        except Exception as e:
            log.warning("уведомление %s: %s", r["id"], e)
            await asyncio.to_thread(db.execute, "UPDATE reward_notifications SET attempts=attempts+1 "
                                                "WHERE id=?", (r["id"],))
            continue
        await asyncio.to_thread(db.execute, "UPDATE reward_notifications SET sent_at=?, "
                                            "attempts=attempts+1 WHERE id=?",
                                (_iso(_utcnow()), r["id"]))
        sent += 1 if status == "sent" else 0
        await asyncio.sleep(0.05)
    return sent


# ───────────────────────── фоновая работа ─────────────────────────

def sweep(now: datetime = None) -> dict:
    """Страховка раз в несколько минут: доступ, начисления, штрафы, предупреждения, места."""
    now = now or _utcnow()
    res = {"annulled": 0, "rewards": 0, "penalties": 0, "warnings": 0}
    subjects = set()
    for r in db.fetchall("SELECT id FROM subject_participants WHERE is_active=1"):
        try:
            p = participant_by_id(r["id"])
            if check_access(p, now):
                res["annulled"] += 1
                continue
            res["rewards"] += len(reconcile(p, now))
            p = refresh(p, now, rank=False)
            res["penalties"] += len(penalize(p, now))
            res["warnings"] += 1 if warn(participant_by_id(p["id"]), now) else 0
            subjects.add(p["subject_id"])
        except Exception as e:
            log.warning("вознаграждения, участник %s: %s", r["id"], e)
    for sid in subjects:
        rank_subject(sid)
    return res


async def loop(bot) -> None:
    await asyncio.sleep(30)
    last_sweep = 0.0
    while True:
        try:
            await flush(bot)
        except Exception as e:
            log.warning("очередь уведомлений: %s", e)
        if time.time() - last_sweep >= 300:
            last_sweep = time.time()
            try:
                res = await asyncio.to_thread(sweep)
                if any(res.values()):
                    log.info("вознаграждения: %s", res)
            except Exception as e:
                log.warning("вознаграждения, проход: %s", e)
        await asyncio.sleep(15)
