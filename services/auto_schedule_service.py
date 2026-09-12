"""
Автозапуск тестов по расписанию (в чат, через лобби группового теста).

Расписаний может быть сколько угодно. У каждого:
  • источник тестов — разделы (один, несколько или «без раздела»), список
    тестов, выбранный вручную, или и то и другое; плюс «работа над ошибками»:
    N тестов, где ученики ошибаются чаще всего (services/difficulty_service);
  • периодичность — каждый день, через день, по дням недели, каждые N дней,
    конкретные даты; одно или несколько времён запуска в день;
  • срок — до даты или бессрочно (пока админ сам не остановит);
  • режим — по одному тесту за запуск (по списку, с повтором сначала или
    без), все тесты списка за один запуск (друг за другом), случайный,
    «умный выбор» (сначала новые, потом реже запускавшиеся — как раньше);
  • пауза / продолжение / остановка, любое поле меняется без пересоздания.

Каждый запуск (слот «дата время») отмечается в расписании ДО старта
(last_run_slot), а каждый тест слота — строкой auto_schedule_runs с
уникальным run_key. Перезапуск бота, второй проход цикла или сбой не дадут
запустить тот же тест второй раз. В одном чате идёт один групповой тест, поэтому
тесты одного слота стартуют друг за другом: следующий — когда завершён
предыдущий (on_quiz_done из services/group_quiz_service).

Старые расписания (одно время в день, один раздел, срок до даты) читаются
как есть: недостающие поля дополняются значениями по умолчанию.
"""
import asyncio
import json
import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot

import config
import database as db

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

# Ограничения интервала сообщений во время теста
SLOWMODE_DURING_TEST = 300   # 5 минут между сообщениями
SLOWMODE_NORMAL = 5          # 5 секунд обычный режим
# Если бот лежал и «проспал» время запуска больше чем на столько минут —
# слот пропускаем, чтобы тест не прилетел людям среди ночи.
MAX_LATE_MINUTES = 180
# Сколько дней вперёд искать следующий запуск
LOOKAHEAD_DAYS = 400

PERIODS = {"daily": "каждый день", "alt": "через день", "weekdays": "по дням недели",
           "everyn": "каждые N дней", "dates": "в выбранные даты"}
RUN_MODES = {"one": "по одному тесту за запуск, по списку",
             "all": "все тесты списка за один запуск (друг за другом)",
             "random": "случайный тест",
             "smart": "умный выбор: сначала новые, потом реже запускавшиеся"}
SOURCES = {"auto": "все тесты выбранных разделов", "manual": "выбранные вручную",
           "both": "разделы + выбранные вручную"}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
STATUS_LABELS = {"active": "🟢 Активно", "paused": "🟡 На паузе", "stopped": "🔴 Отключено",
                 "scheduled": "⚪ Запланировано", "finished": "✅ Завершено"}
RUN_STATUS = {"queued": "⏳ в очереди", "announced": "📣 анонс", "launched": "▶️ идёт",
              "finished": "✅ завершён", "cancelled": "😴 не набралось игроков",
              "error": "⚠️ ошибка", "skipped": "⏭ пропущен"}


def _now_almaty() -> datetime:
    return datetime.now(ALMATY)


def _today_almaty() -> str:
    return _now_almaty().strftime("%Y-%m-%d")


def norm_time(raw) -> str:
    """«9:00», « 9:0 », «09.00» → «09:00». Без этого сравнение с текущим
    временем никогда не совпадало и расписание молча не срабатывало."""
    t = str(raw or "").strip().replace(".", ":").replace("-", ":")
    parts = t.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return "09:00"
    h = max(0, min(23, h))
    m = max(0, min(59, m))
    return f"{h:02d}:{m:02d}"


def parse_times(raw) -> list:
    """«19:00» или «10:00, 19:00» → ['10:00', '19:00'] (без повторов, по порядку)."""
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = [p for p in str(raw or "").replace(";", ",").replace("\n", ",").split(",")]
    out = []
    for p in items:
        p = str(p).strip()
        if not p:
            continue
        t = norm_time(p)
        if t not in out:
            out.append(t)
    return sorted(out) or ["09:00"]


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _valid_date(s) -> bool:
    try:
        datetime.strptime(str(s).strip(), "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# ───────────────────────── чтение и запись ─────────────────────────

_LIST_FIELDS = ("category_ids", "test_ids", "weekdays", "dates", "times")


def load(row) -> Optional[dict]:
    """Строка расписания → словарь с дополненными полями. Старые записи
    (один раздел, одно время, срок до даты) выглядят как новые."""
    if not row:
        return None
    s = dict(row)
    for f in _LIST_FIELDS:
        s[f] = _json_list(s.get(f))
    if not s["category_ids"] and s.get("category_id"):
        s["category_ids"] = [int(s["category_id"])]
    if not s["times"]:
        s["times"] = [norm_time(s.get("daily_time") or "09:00")]
    s["times"] = sorted(dict.fromkeys(norm_time(t) for t in s["times"]))
    s["period"] = s.get("period") or "daily"
    s["interval_days"] = int(s.get("interval_days") or 2)
    s["source"] = s.get("source") or ("manual" if s["test_ids"] and not s["category_ids"] else "auto")
    s["run_mode"] = s.get("run_mode") or "smart"
    s["loop"] = 1 if s.get("loop") is None else int(s["loop"] or 0)
    s["hardest_n"] = int(s.get("hardest_n") or 0)
    s["cursor"] = int(s.get("cursor") or 0)
    if not s.get("end_mode"):
        s["end_mode"] = "date" if s.get("end_date") else "never"
    if s["end_mode"] == "never":
        s["end_date"] = None
    s["tests_per_day"] = int(s.get("tests_per_day") or 1)
    s["announce_delay"] = int(s.get("announce_delay") or 60)
    s["title"] = (s.get("title") or "").strip()
    s["status"] = s.get("status") or "active"
    return s


def get_schedule(schedule_id: int) -> Optional[dict]:
    return load(db.fetchone("SELECT * FROM auto_schedule WHERE id=?", (int(schedule_id),)))


def list_schedules(active_only: bool = False) -> list:
    sql = "SELECT * FROM auto_schedule"
    if active_only:
        sql += " WHERE status='active'"
    return [load(r) for r in db.fetchall(sql + " ORDER BY (status='active') DESC, id DESC")]


def get_active_schedules() -> list:
    return list_schedules(active_only=True)


def get_schedule_by_chat(chat_id) -> Optional[dict]:
    """Активное расписание чата (последнее). Осталось для совместимости."""
    return load(db.fetchone(
        "SELECT * FROM auto_schedule WHERE chat_id=? AND status='active' "
        "ORDER BY id DESC LIMIT 1", (str(chat_id),)))


def _cols() -> set:
    return {r["name"] for r in db.fetchall("PRAGMA table_info(auto_schedule)")}


def create(data: dict) -> int:
    """Создать расписание из словаря полей нового образца. Возвращает id."""
    d = dict(data)
    d.setdefault("status", "active")
    d.setdefault("start_date", _today_almaty())
    times = parse_times(d.get("times") or d.get("daily_time") or "09:00")
    d["times"] = times
    d["daily_time"] = times[0]
    cats = [int(c) for c in _json_list(d.get("category_ids")) if str(c).lstrip("-").isdigit()]
    d["category_ids"] = cats
    d["category_id"] = cats[0] if len(cats) == 1 else None
    d["test_ids"] = [int(t) for t in _json_list(d.get("test_ids"))]
    d["weekdays"] = [int(w) for w in _json_list(d.get("weekdays"))]
    d["dates"] = [x for x in _json_list(d.get("dates")) if _valid_date(x)]
    d["end_mode"] = d.get("end_mode") or ("date" if d.get("end_date") else "never")
    d["end_date"] = (d.get("end_date") or "") if d["end_mode"] == "date" else ""
    d["chat_id"] = str(d.get("chat_id") or "")
    if d.get("channel_id"):
        d["channel_id"] = str(d["channel_id"])
    have = _cols()
    row = {}
    for k, v in d.items():
        if k not in have:
            continue
        row[k] = _dumps(v) if k in _LIST_FIELDS else v
    cols = ", ".join(row)
    cur = db.execute(f"INSERT INTO auto_schedule ({cols}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))
    sid = cur.lastrowid
    refresh_next_run(sid)
    return sid


def create_schedule(chat_id, category_id, start_date, end_date,
                    daily_time, tests_per_day=1, allow_paid=0,
                    allow_private=0, bot_username='', created_by=None,
                    channel_id=None, announce_delay=60, **extra) -> int:
    """Прежний способ создания (один раздел, одно время, срок до даты)."""
    data = {"chat_id": chat_id, "category_ids": [category_id] if category_id else [],
            "start_date": start_date, "end_date": end_date or "",
            "end_mode": "date" if end_date else "never",
            "times": [daily_time], "tests_per_day": tests_per_day, "allow_paid": allow_paid,
            "allow_private": allow_private, "bot_username": bot_username,
            "created_by": created_by, "channel_id": channel_id, "announce_delay": announce_delay,
            "source": "auto", "run_mode": "smart", "period": "daily"}
    data.update(extra)
    return create(data)


def update_schedule(schedule_id: int, **fields):
    """Обновить поля. Списки — как списки; время — в нормальном виде.
    Меняется всё, что влияет на будущие запуски; прошедшие не трогаются."""
    if not fields:
        return
    have = _cols()
    if "daily_time" in fields and "times" not in fields:
        fields["times"] = parse_times(fields["daily_time"])
    if "times" in fields:
        fields["times"] = parse_times(fields["times"])
        fields["daily_time"] = fields["times"][0]
    if "category_ids" in fields:
        cats = [int(c) for c in _json_list(fields["category_ids"])]
        fields["category_ids"] = cats
        fields["category_id"] = cats[0] if len(cats) == 1 else None
    if fields.get("end_mode") == "never":
        fields["end_date"] = ""
    row = {k: (_dumps(v) if k in _LIST_FIELDS else v) for k, v in fields.items() if k in have}
    if not row:
        return
    cols = ", ".join(f"{k}=?" for k in row)
    db.execute(f"UPDATE auto_schedule SET {cols} WHERE id=?", (*row.values(), int(schedule_id)))
    refresh_next_run(schedule_id)


def set_status(schedule_id: int, status: str) -> None:
    assert status in ("active", "paused", "stopped", "finished")
    fields = {"status": status}
    if status == "paused":
        fields["paused_at"] = _now_almaty().strftime("%Y-%m-%dT%H:%M:%S")
    elif status == "active":
        fields["paused_at"] = None
        # После паузы или остановки не «догоняем» пропущенные слоты
        fields["last_run_slot"] = _now_almaty().strftime("%Y-%m-%d %H:%M")
    update_schedule(schedule_id, **fields)


def stop_schedule(schedule_id: int):
    set_status(schedule_id, "stopped")


def pause_schedule(schedule_id: int):
    set_status(schedule_id, "paused")


def resume_schedule(schedule_id: int):
    set_status(schedule_id, "active")


def delete_schedule(schedule_id: int) -> None:
    db.execute("DELETE FROM auto_schedule_runs WHERE schedule_id=?", (int(schedule_id),))
    db.execute("DELETE FROM auto_schedule WHERE id=?", (int(schedule_id),))


# ───────────────────────── тесты расписания ─────────────────────────

def _test_filters(sched: dict) -> tuple:
    filters = ["status='active'", "(SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0"]
    if not sched.get("allow_paid"):
        filters.append("COALESCE(is_paid,0)=0")
    if not sched.get("allow_private"):
        filters.append("COALESCE(is_private,0)=0")
    return filters, []


def category_tests(sched: dict) -> list:
    """Тесты выбранных разделов (или всех, если разделы не выбраны)."""
    filters, params = _test_filters(sched)
    cats = sched.get("category_ids") or []
    if cats:
        filters.append(f"category_id IN ({','.join('?' * len(cats))})")
        params += list(cats)
    rows = db.fetchall(f"SELECT * FROM tests WHERE {' AND '.join(filters)} ORDER BY id",
                       tuple(params))
    return [dict(r) for r in rows]


def manual_tests(sched: dict) -> list:
    """Тесты, выбранные вручную, — в заданном админом порядке."""
    ids = [int(t) for t in sched.get("test_ids") or []]
    if not ids:
        return []
    rows = {r["id"]: dict(r) for r in db.fetchall(
        f"SELECT * FROM tests WHERE id IN ({','.join('?' * len(ids))}) AND status='active' "
        f"AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0", tuple(ids))}
    return [rows[i] for i in ids if i in rows]


def hardest_tests(sched: dict) -> list:
    """«Работа над ошибками»: тесты, где ученики ошибаются чаще всего."""
    n = int(sched.get("hardest_n") or 0)
    if n <= 0:
        return []
    try:
        from services import difficulty_service as ds
        ids = ds.get_hardest_test_ids(limit=n)
    except Exception as e:
        log.warning("hardest tests: %s", e)
        return []
    if not ids:
        return []
    filters, _ = _test_filters(sched)
    rows = {r["id"]: dict(r) for r in db.fetchall(
        f"SELECT * FROM tests WHERE id IN ({','.join('?' * len(ids))}) AND {' AND '.join(filters)}",
        tuple(ids))}
    return [rows[i] for i in ids if i in rows]


def pool(sched: dict) -> list:
    """Все тесты расписания по порядку: вручную выбранные → разделы → работа
    над ошибками. Без повторов."""
    src = sched.get("source") or "auto"
    parts = []
    if src in ("manual", "both"):
        parts += manual_tests(sched)
    if src in ("auto", "both"):
        parts += category_tests(sched)
    parts += hardest_tests(sched)
    seen, out = set(), []
    for t in parts:
        if t["id"] not in seen:
            seen.add(t["id"])
            out.append(t)
    return out


def _eligible_tests(sched: dict) -> list:
    return pool(sched)


def diagnose_schedule(sched: dict) -> str:
    """Почему расписание не может запустить тест — человеческим языком."""
    cats = sched.get("category_ids") or []
    where, params = "1=1", []
    if cats:
        where += f" AND category_id IN ({','.join('?' * len(cats))})"
        params += list(cats)
    total = db.fetchone(f"SELECT COUNT(*) c FROM tests WHERE {where}", tuple(params))["c"]
    active = db.fetchone(f"SELECT COUNT(*) c FROM tests WHERE status='active' AND {where}",
                         tuple(params))["c"]
    with_q = db.fetchone(
        f"SELECT COUNT(*) c FROM tests WHERE status='active' AND {where} AND "
        "(SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0", tuple(params))["c"]
    paid = db.fetchone(f"SELECT COUNT(*) c FROM tests WHERE status='active' AND {where} AND "
                       "COALESCE(is_paid,0)=1", tuple(params))["c"]
    priv = db.fetchone(f"SELECT COUNT(*) c FROM tests WHERE status='active' AND {where} AND "
                       "COALESCE(is_private,0)=1", tuple(params))["c"]
    lines = [f"Тестов по выбору: {total}", f"Из них активных: {active}", f"С вопросами: {with_q}",
             f"Выбрано вручную: {len(sched.get('test_ids') or [])}"]
    if paid and not sched.get("allow_paid"):
        lines.append(f"⚠️ Платных пропущено: {paid} — включите «Разрешить платные»")
    if priv and not sched.get("allow_private"):
        lines.append(f"⚠️ Приватных пропущено: {priv} — включите «Разрешить приватные»")
    if with_q == 0 and active:
        lines.append("⚠️ У тестов нет вопросов")
    return "\n".join(lines)


def _run_stats(sched_id: int) -> dict:
    stats = {}
    for r in db.fetchall(
            "SELECT test_id, COUNT(*) AS runs, COALESCE(SUM(participants),0) AS parts "
            "FROM auto_schedule_runs WHERE schedule_id=? AND status<>'skipped' GROUP BY test_id",
            (sched_id,)):
        stats[r["test_id"]] = {"runs": r["runs"], "parts": r["parts"]}
    return stats


def pick_next_test(sched: dict, exclude: set = None) -> Optional[dict]:
    """Умный выбор: ещё не запускавшиеся → реже запускавшиеся → с меньшим
    числом участников → повтор с начала."""
    tests = [t for t in pool(sched) if t["id"] not in (exclude or set())]
    if not tests:
        return None
    stats = _run_stats(sched["id"])
    tests.sort(key=lambda t: (stats.get(t["id"], {}).get("runs", 0),
                              stats.get(t["id"], {}).get("parts", 0), t["id"]))
    return tests[0]


def pick_for_slot(sched: dict) -> list:
    """Какие тесты ставить в очередь на этот запуск (по режиму). Двигает
    курсор списка для режима «по одному»."""
    mode = sched.get("run_mode") or "smart"
    p = pool(sched)
    if not p:
        return []
    if mode == "smart":
        out, ex = [], set()
        for _ in range(max(1, int(sched.get("tests_per_day") or 1))):
            t = pick_next_test(sched, ex)
            if not t:
                break
            out.append(t)
            ex.add(t["id"])
        return out
    if mode == "random":
        return random.sample(p, min(len(p), max(1, int(sched.get("tests_per_day") or 1))))
    if mode == "all":
        db.execute("UPDATE auto_schedule SET cursor=? WHERE id=?", (len(p), sched["id"]))
        return p
    # one — по списку
    n = max(1, int(sched.get("tests_per_day") or 1))
    cur = int(sched.get("cursor") or 0)
    if cur >= len(p):
        if not sched.get("loop"):
            return []
        cur = 0
    out = []
    for _ in range(n):
        if cur >= len(p):
            if not sched.get("loop"):
                break
            cur = 0
        out.append(p[cur])
        cur += 1
    db.execute("UPDATE auto_schedule SET cursor=? WHERE id=?", (cur, sched["id"]))
    return out


def list_exhausted(sched: dict) -> bool:
    """Список пройден до конца, повтор не включён — расписанию пора завершиться."""
    mode = sched.get("run_mode") or "smart"
    if mode not in ("one", "all") or sched.get("loop"):
        return False
    return int(sched.get("cursor") or 0) >= len(pool(sched)) > 0


# ───────────────────────── когда запускать ─────────────────────────

def is_run_day(sched: dict, d: date) -> bool:
    period = sched.get("period") or "daily"
    start = datetime.strptime(sched["start_date"], "%Y-%m-%d").date() if _valid_date(sched.get("start_date")) else d
    if d < start:
        return False
    if sched.get("end_mode") == "date" and _valid_date(sched.get("end_date")):
        if d > datetime.strptime(sched["end_date"], "%Y-%m-%d").date():
            return False
    if period == "daily":
        return True
    if period == "alt":
        return (d - start).days % 2 == 0
    if period == "everyn":
        n = max(1, int(sched.get("interval_days") or 2))
        return (d - start).days % n == 0
    if period == "weekdays":
        return d.weekday() in {int(w) for w in sched.get("weekdays") or []}
    if period == "dates":
        return d.isoformat() in set(sched.get("dates") or [])
    return True


def slots_for_day(sched: dict, d: date) -> list:
    return list(sched.get("times") or ["09:00"]) if is_run_day(sched, d) else []


def _slot_dt(d: date, hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=ALMATY)


def next_run_at(sched: dict, now: datetime = None) -> Optional[datetime]:
    """Ближайший будущий запуск (Астана) или None, если запусков больше не будет."""
    if sched.get("status") not in ("active", "paused", None):
        return None
    now = now or _now_almaty()
    d = now.date()
    last = sched.get("last_run_slot") or ""
    for _ in range(LOOKAHEAD_DAYS):
        for hm in slots_for_day(sched, d):
            key = f"{d.isoformat()} {hm}"
            if key <= last:
                continue
            dt = _slot_dt(d, hm)
            if dt >= now - timedelta(minutes=MAX_LATE_MINUTES):
                return dt
        d += timedelta(days=1)
        if sched.get("end_mode") == "date" and _valid_date(sched.get("end_date")) \
                and d > datetime.strptime(sched["end_date"], "%Y-%m-%d").date():
            return None
    return None


def refresh_next_run(schedule_id: int) -> Optional[str]:
    sched = get_schedule(schedule_id)
    if not sched:
        return None
    nxt = next_run_at(sched)
    val = nxt.strftime("%Y-%m-%d %H:%M") if nxt else None
    if val != sched.get("next_run_at"):
        db.execute("UPDATE auto_schedule SET next_run_at=? WHERE id=?", (val, schedule_id))
    return val


def display_status(sched: dict) -> str:
    st = sched.get("status") or "active"
    if st == "active" and _valid_date(sched.get("start_date")) and sched["start_date"] > _today_almaty():
        st = "scheduled"
    return STATUS_LABELS.get(st, st)


def describe_period(sched: dict) -> str:
    p = sched.get("period") or "daily"
    if p == "weekdays":
        days = ", ".join(WEEKDAYS[int(w)] for w in sorted(sched.get("weekdays") or []) if 0 <= int(w) <= 6)
        return f"по дням недели: {days or '—'}"
    if p == "everyn":
        return f"каждые {int(sched.get('interval_days') or 2)} дн."
    if p == "dates":
        ds = sched.get("dates") or []
        shown = ", ".join(datetime.strptime(x, "%Y-%m-%d").strftime("%d.%m") for x in ds[:6])
        return f"в даты: {shown}{' …' if len(ds) > 6 else ''}"
    return PERIODS.get(p, p)


def describe_end(sched: dict) -> str:
    if sched.get("end_mode") == "date" and sched.get("end_date"):
        return f"до {datetime.strptime(sched['end_date'], '%Y-%m-%d').strftime('%d.%m.%Y')}"
    return "бессрочно — пока не остановите"


def describe_source(sched: dict) -> str:
    cats = sched.get("category_ids") or []
    names = []
    for c in cats:
        r = db.fetchone("SELECT name FROM test_categories WHERE id=?", (c,))
        names.append(r["name"] if r else f"#{c}")
    src = sched.get("source") or "auto"
    parts = []
    if src in ("auto", "both"):
        parts.append(("разделы: " + ", ".join(names)) if names else "все тесты бота")
    if src in ("manual", "both"):
        parts.append(f"вручную: {len(manual_tests(sched))} тест(ов)")
    if int(sched.get("hardest_n") or 0):
        parts.append(f"работа над ошибками: {sched['hardest_n']} самых сложных")
    return "; ".join(parts) or "—"


def describe_run_mode(sched: dict) -> str:
    mode = sched.get("run_mode") or "smart"
    s = RUN_MODES.get(mode, mode)
    if mode in ("one", "all"):
        s += "; " + ("после конца списка — снова с начала" if sched.get("loop") else "после конца списка — стоп")
    if mode in ("one", "random", "smart") and int(sched.get("tests_per_day") or 1) > 1:
        s += f"; за запуск: {sched['tests_per_day']}"
    return s


# ───────────────────────── запуски и история ─────────────────────────

def record_run(schedule_id: int, test_id: int, participants: int = 0, slot_key: str = None,
               status: str = "launched", **extra):
    """Строка истории запуска. run_key делает её единственной для
    (расписание, слот, тест): повтор INSERT ничего не добавит."""
    slot_key = slot_key or _now_almaty().strftime("%Y-%m-%d %H:%M")
    run_key = f"{schedule_id}:{slot_key}:{test_id}"
    cols = {"schedule_id": schedule_id, "test_id": test_id,
            "run_date": _now_almaty().isoformat(timespec="seconds"), "participants": participants,
            "run_key": run_key, "slot_key": slot_key, "status": status}
    cols.update(extra)
    cur = db.execute(
        f"INSERT OR IGNORE INTO auto_schedule_runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()))
    return cur.rowcount or 0


def update_run_participants(schedule_id: int, test_id: int, participants: int):
    row = db.fetchone("SELECT id FROM auto_schedule_runs WHERE schedule_id=? AND test_id=? "
                      "ORDER BY id DESC LIMIT 1", (schedule_id, test_id))
    if row:
        db.execute("UPDATE auto_schedule_runs SET participants=? WHERE id=?", (participants, row["id"]))


def record_chat_activity(chat_id, user_tg_id):
    """Отметить что пользователь активен в чате (для статистики)."""
    try:
        db.execute("INSERT OR IGNORE INTO chat_activity (chat_id, user_tg_id) VALUES (?,?)",
                   (str(chat_id), user_tg_id))
    except Exception:
        pass


def run_history(schedule_id: int, limit: int = 15) -> list:
    rows = db.fetchall(
        "SELECT r.*, t.title, t.category_id FROM auto_schedule_runs r "
        "LEFT JOIN tests t ON t.id=r.test_id WHERE r.schedule_id=? ORDER BY r.id DESC LIMIT ?",
        (schedule_id, limit))
    out = []
    for r in rows:
        r = dict(r)
        cat = db.fetchone("SELECT name FROM test_categories WHERE id=?", (r["category_id"],)) \
            if r.get("category_id") else None
        r["category"] = cat["name"] if cat else "—"
        r["title"] = r.get("title") or f"тест #{r['test_id']}"
        r["status_label"] = RUN_STATUS.get(r.get("status") or "", r.get("status") or "")
        try:
            r["when"] = datetime.fromisoformat(r["run_date"]).strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            r["when"] = str(r.get("run_date") or "")[:16]
        try:
            r["planned"] = datetime.strptime(r.get("slot_key") or "", "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            r["planned"] = r["when"]
        try:
            r["actual"] = (datetime.fromisoformat(r["launched_at"]).strftime("%d.%m.%Y %H:%M")
                           if r.get("launched_at") else "—")
        except (ValueError, TypeError):
            r["actual"] = str(r.get("launched_at") or "—")[:16]
        out.append(r)
    return out


def journal(limit: int = 200, schedule_id: int = None) -> list:
    """Журнал автозапусков для админки сайта: все расписания, новые сверху."""
    sql = ("SELECT r.*, t.title, t.category_id, s.title AS sched_title, s.chat_id "
           "FROM auto_schedule_runs r LEFT JOIN tests t ON t.id=r.test_id "
           "LEFT JOIN auto_schedule s ON s.id=r.schedule_id")
    params = []
    if schedule_id:
        sql += " WHERE r.schedule_id=?"
        params.append(int(schedule_id))
    rows = [dict(r) for r in db.fetchall(sql + " ORDER BY r.id DESC LIMIT ?", (*params, int(limit)))]
    cats, groups, subjects = {}, {}, {}
    for r in rows:
        cid = r.get("category_id")
        if cid and cid not in cats:
            c = db.fetchone("SELECT name FROM test_categories WHERE id=?", (cid,))
            cats[cid] = c["name"] if c else f"#{cid}"
        chat = r.get("chat_id")
        if chat and chat not in groups:
            try:
                g = db.fetchone("SELECT title FROM known_groups WHERE chat_id=?", (int(chat),))
            except (TypeError, ValueError):
                g = None
            groups[chat] = (g["title"] if g and g["title"] else str(chat))
        tid = r.get("test_id")
        if tid and tid not in subjects:
            sub = db.fetchone("SELECT sub.title FROM lessons l JOIN sections sec ON sec.id=l.section_id "
                              "JOIN subjects sub ON sub.id=sec.subject_id WHERE l.test_id=? LIMIT 1", (tid,))
            subjects[tid] = sub["title"] if sub else None
        r["title"] = r.get("title") or (f"тест #{r['test_id']}" if r.get("test_id") else "—")
        r["subject"] = subjects.get(r.get("test_id")) or cats.get(cid) or "—"
        r["group"] = groups.get(chat, str(chat or "—"))
        r["status_label"] = RUN_STATUS.get(r.get("status") or "", r.get("status") or "")
        for src, dst, fmt in (("slot_key", "planned", "%Y-%m-%d %H:%M"), ("launched_at", "actual", None),
                              ("finished_at", "finished", None)):
            v = r.get(src)
            try:
                d = datetime.strptime(v, fmt) if (v and fmt) else (datetime.fromisoformat(v) if v else None)
                r[dst] = d.strftime("%d.%m.%Y %H:%M") if d else "—"
            except (ValueError, TypeError):
                r[dst] = str(v or "—")[:16]
    return rows


def get_schedule_stats(schedule_id: int) -> dict:
    """Статистика по расписанию для админа."""
    sched = get_schedule(schedule_id)
    if not sched:
        return {}
    activity = db.fetchone("SELECT COUNT(*) AS c FROM chat_activity WHERE chat_id=?",
                           (sched["chat_id"],))["c"]
    runs = db.fetchall(
        """SELECT r.test_id, t.title, COUNT(*) AS run_count,
                  COALESCE(SUM(r.participants),0) AS total_parts,
                  COALESCE(SUM(r.finished_count),0) AS total_finished
           FROM auto_schedule_runs r LEFT JOIN tests t ON t.id = r.test_id
           WHERE r.schedule_id=? AND r.status IN ('launched','finished','cancelled')
           GROUP BY r.test_id ORDER BY run_count DESC""", (schedule_id,))
    uniq = db.fetchone(
        "SELECT COUNT(DISTINCT p.tg_id) AS c FROM group_quiz_players p "
        "JOIN auto_schedule_runs r ON r.group_quiz_id=p.group_quiz_id WHERE r.schedule_id=?",
        (schedule_id,))["c"]
    eligible = pool(sched)
    run_test_ids = {r["test_id"] for r in runs}
    never_run = [t for t in eligible if t["id"] not in run_test_ids]
    return {"chat_activity": activity, "runs": [dict(r) for r in runs], "never_run": never_run,
            "total_eligible": len(eligible), "unique_players": uniq,
            "total_runs": sum(r["run_count"] for r in runs)}


# ───────────────────────── slow mode ─────────────────────────

async def set_chat_slowmode(bot: Bot, chat_id, seconds: int) -> bool:
    """Интервал между сообщениями в чате: 5 мин во время теста, 5 с обычно.
    Чат не закрывается, только меняется интервал."""
    try:
        await bot(_make_slowmode_request(int(chat_id), seconds))
        return True
    except Exception as e:
        log.warning("set slowmode %s: %s", chat_id, e)
        return False


def _make_slowmode_request(chat_id: int, seconds: int):
    from aiogram.methods import TelegramMethod

    class SetChatSlowMode(TelegramMethod[bool]):
        __returning__ = bool
        __api_method__ = "setChatSlowModeDelay"
        chat_id: int
        seconds: int

    return SetChatSlowMode(chat_id=chat_id, seconds=seconds)


# ───────────────────────── цикл планировщика ─────────────────────────

def _minutes_late(now: datetime, slot: datetime) -> int:
    return int((now - slot).total_seconds() // 60)


def claim_slot(sched: dict, key: str) -> bool:
    """Отметить слот за собой ДО запуска. Повторный проход (или второй
    экземпляр цикла) слот уже не получит."""
    cur = db.execute("UPDATE auto_schedule SET last_run_slot=?, last_run_date=? WHERE id=? "
                     "AND (last_run_slot IS NULL OR last_run_slot<?)",
                     (key, key[:10], sched["id"], key))
    return bool(cur.rowcount)


async def scheduler_loop(bot: Bot):
    """Каждые 30 секунд: не пора ли запустить какой-нибудь слот."""
    try:
        for r in db.fetchall("SELECT id, daily_time FROM auto_schedule"):
            fixed = norm_time(r["daily_time"])
            if fixed != (r["daily_time"] or ""):
                db.execute("UPDATE auto_schedule SET daily_time=? WHERE id=?", (fixed, r["id"]))
    except Exception as e:
        log.warning("normalize daily_time: %s", e)
    try:
        await recover(bot)
    except Exception as e:
        log.exception("восстановление автозапуска после перезапуска: %s", e)
    log.info("Планировщик автозапуска запущен")
    while True:
        try:
            tick(bot)
        except Exception as e:
            log.warning("scheduler_loop: %s", e)
        await asyncio.sleep(30)


ANNOUNCE_STUCK_MINUTES = 15       # анонс ушёл, а лобби так и не появилось — считаем сбоем


async def recover(bot: Bot) -> dict:
    """После перезапуска бота: закрыть тесты-сироты и продолжить запуски,
    которые оборвались (анонс ушёл, а лобби не успело создаться)."""
    from services import group_quiz_service as gqs
    res = {"orphans": 0, "resumed": 0, "expired": 0}
    try:
        res["orphans"] = await gqs.cleanup_orphans(bot)
    except Exception as e:
        log.warning("закрытие тестов-сирот: %s", e)
    now = _now_almaty()
    db.execute("UPDATE auto_schedule_runs SET status='queued', error='прерван перезапуском бота — "
               "запускаем заново' WHERE status='announced'")
    for r in db.fetchall("SELECT DISTINCT schedule_id, slot_key FROM auto_schedule_runs "
                         "WHERE status='queued' ORDER BY id"):
        try:
            slot = datetime.strptime(r["slot_key"], "%Y-%m-%d %H:%M").replace(tzinfo=ALMATY)
        except (ValueError, TypeError):
            slot = now
        if _minutes_late(now, slot) > MAX_LATE_MINUTES:
            db.execute("UPDATE auto_schedule_runs SET status='error', error='прерван перезапуском бота, "
                       "время запуска прошло' WHERE schedule_id=? AND slot_key=? AND status='queued'",
                       (r["schedule_id"], r["slot_key"]))
            res["expired"] += 1
            log.error("АВТОЗАПУСК: расписание %s, слот %s — потерян из-за перезапуска, время прошло",
                      r["schedule_id"], r["slot_key"])
            continue
        res["resumed"] += 1
        log.warning("АВТОЗАПУСК: расписание %s, слот %s — продолжаем после перезапуска",
                    r["schedule_id"], r["slot_key"])
        asyncio.ensure_future(_safe_launch_next(bot, r["schedule_id"]))
    if any(res.values()):
        log.info("автозапуск после перезапуска: %s", res)
    return res


def _watch_stuck(bot: Bot, now: datetime) -> None:
    """Анонс ушёл, а лобби не появилось за ANNOUNCE_STUCK_MINUTES — это сбой:
    отметить с причиной, сообщить админу, двинуть очередь дальше."""
    limit = (now - timedelta(minutes=ANNOUNCE_STUCK_MINUTES)).isoformat(timespec="seconds")
    for r in db.fetchall("SELECT * FROM auto_schedule_runs WHERE status='announced' AND run_date<?", (limit,)):
        cur = db.execute("UPDATE auto_schedule_runs SET status='error', error=? WHERE id=? AND status='announced'",
                         ("лобби не появилось после анонса (сбой запуска)", r["id"]))
        if not cur.rowcount:
            continue
        sched = get_schedule(r["schedule_id"])
        test = db.fetchone("SELECT title FROM tests WHERE id=?", (r["test_id"],))
        log.error("АВТОЗАПУСК: расписание %s, тест %s — анонс был, лобби не появилось %d мин",
                  r["schedule_id"], r["test_id"], ANNOUNCE_STUCK_MINUTES)
        if sched:
            asyncio.ensure_future(_notify_admin(
                bot, sched, f"⚠️ <b>Автозапуск: тест не запустился</b>\n\nРасписание #{sched['id']}, "
                            f"тест «{(test or {}).get('title') or r['test_id']}»\nПричина: анонс ушёл, "
                            f"а лобби не появилось. Очередь продолжена."))
            asyncio.ensure_future(_safe_launch_next(bot, sched["id"]))


def tick(bot: Bot, now: datetime = None) -> list:
    """Один проход цикла. Возвращает ключи запущенных слотов (для проверок)."""
    now = now or _now_almaty()
    today = now.date()
    launched = []
    try:
        _watch_stuck(bot, now)
    except Exception as e:
        log.warning("сторож зависших запусков: %s", e)
    for sched in get_active_schedules():
        try:
            if _valid_date(sched.get("start_date")) and today.isoformat() < sched["start_date"]:
                continue
            if sched.get("end_mode") == "date" and _valid_date(sched.get("end_date")) \
                    and today.isoformat() > sched["end_date"]:
                update_schedule(sched["id"], status="finished")
                continue
            if list_exhausted(sched):
                update_schedule(sched["id"], status="finished")
                continue
            last = sched.get("last_run_slot") or ""
            for hm in slots_for_day(sched, today):
                key = f"{today.isoformat()} {hm}"
                if key <= last:
                    continue
                slot = _slot_dt(today, hm)
                if now < slot:
                    continue
                late = _minutes_late(now, slot)
                if late > MAX_LATE_MINUTES:
                    # Бот лежал полдня — не спамим людям среди ночи
                    if claim_slot(sched, key):
                        record_run(sched["id"], 0, slot_key=key, status="skipped",
                                   error=f"опоздание {late} мин")
                        log.info("Расписание %s: слот %s пропущен (опоздали на %d мин)",
                                 sched["id"], key, late)
                    continue
                if not claim_slot(sched, key):
                    continue
                tests = pick_for_slot(sched)
                if not tests:
                    record_run(sched["id"], 0, slot_key=key, status="error", error="нет тестов")
                    asyncio.ensure_future(_notify_no_tests(bot, sched))
                    continue
                for t in tests:
                    record_run(sched["id"], t["id"], slot_key=key, status="queued")
                launched.append(key)
                log.info("Расписание %s: слот %s, тестов в очереди: %d", sched["id"], key, len(tests))
                asyncio.ensure_future(_safe_launch_next(bot, sched["id"]))
                break                      # один слот за проход; следующий — через 30 с
            refresh_next_run(sched["id"])
        except Exception as e:
            log.warning("расписание %s: %s", sched.get("id"), e)
    return launched


async def _notify_no_tests(bot: Bot, sched: dict):
    log.error("АВТОЗАПУСК: расписание %s — нет ни одного теста для запуска. %s",
              sched["id"], diagnose_schedule(sched).replace("\n", "; "))
    await _notify_admin(bot, sched, "⚠️ <b>Автозапуск не нашёл ни одного теста</b>\n\n"
                                    f"Расписание #{sched['id']}\n\n{diagnose_schedule(sched)}")


_launch_locks: dict = {}


async def _safe_launch_next(bot: Bot, schedule_id: int, slot_key: str = None):
    try:
        await launch_next(bot, schedule_id, slot_key)
    except Exception as e:
        log.exception("Расписание %s: запуск слота %s: %s", schedule_id, slot_key, e)


def _chat_busy(chat_id) -> bool:
    """В чате уже идёт групповой тест (свой или запущенный вручную).

    Тест-«сирота» — строка lobby/running, оставшаяся от процесса до перезапуска
    бота (таймеры потеряны, он никогда не завершится) — не считается: его
    закрываем, иначе расписание ждало бы его вечно. Именно так пропадал
    запуск: анонс уходил, а лобби «уже идёт»."""
    try:
        from services import group_quiz_service as gqs
        rows = db.fetchall("SELECT id FROM group_quizzes WHERE chat_id=? AND status IN ('lobby','running')",
                           (int(chat_id),))
    except (TypeError, ValueError):
        return False
    for r in rows:
        if gqs.is_orphan(r["id"]):
            gqs.cancel_orphan_sync(r["id"], "перезапуск бота")
            continue
        return True
    return False


async def launch_next(bot: Bot, schedule_id: int, slot_key: str = None) -> bool:
    """Запустить следующий тест из очереди расписания (самый ранний слот).
    Один за раз: в чате идёт только один групповой тест; пока он идёт, очередь
    ждёт — её двинет on_quiz_done."""
    lock = _launch_locks.setdefault(int(schedule_id), asyncio.Lock())
    async with lock:
        return await _launch_next_locked(bot, schedule_id, slot_key)


async def _launch_next_locked(bot: Bot, schedule_id: int, slot_key: str = None) -> bool:
    sched = get_schedule(schedule_id)
    if not sched:
        return False
    if sched.get("status") != "active":
        return False                       # пауза или остановка — очередь ждёт
    if _chat_busy(sched["chat_id"]):
        db.execute("UPDATE auto_schedule_runs SET error=? WHERE schedule_id=? AND status='queued' "
                   "AND COALESCE(error,'')=''", ("чат занят другим тестом — ждём его окончания", schedule_id))
        log.warning("Расписание %s: в чате %s идёт другой тест — очередь ждёт", schedule_id, sched["chat_id"])
        return False
    sql = "SELECT * FROM auto_schedule_runs WHERE schedule_id=? AND status='queued'"
    params = [schedule_id]
    if slot_key:
        sql += " AND slot_key=?"
        params.append(slot_key)
    row = db.fetchone(sql + " ORDER BY id LIMIT 1", tuple(params))
    if not row:
        return False
    # Забираем строку за собой: повторный вызов её уже не увидит
    cur = db.execute("UPDATE auto_schedule_runs SET status='announced' WHERE id=? AND status='queued'",
                     (row["id"],))
    if not cur.rowcount:
        return False
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (row["test_id"],))
    if not test:
        db.execute("UPDATE auto_schedule_runs SET status='error', error='тест удалён' WHERE id=?", (row["id"],))
        return await _launch_next_locked(bot, schedule_id, slot_key)
    reached = 0
    try:
        reached = await bot.get_chat_member_count(int(sched["chat_id"]))
    except Exception:
        pass
    try:
        ok, gq_id, err = await _announce_and_launch(
            bot, dict(test), sched["chat_id"], sched.get("bot_username") or "",
            category_id=(sched.get("category_ids") or [None])[0] if len(sched.get("category_ids") or []) == 1
            else test["category_id"],
            channel_id=sched.get("channel_id"), announce_delay=sched.get("announce_delay") or 60)
    except Exception as e:
        ok, gq_id, err = False, None, str(e)
    stamp = _now_almaty().isoformat(timespec="seconds")
    if ok:
        # Пометка «ждали чат» больше не нужна; пометка «прерван перезапуском —
        # запущен заново» остаётся: в журнале видно, что запуск был не с первого раза
        db.execute("UPDATE auto_schedule_runs SET status='launched', group_quiz_id=?, reached=?, launched_at=?, "
                   "error=CASE WHEN error LIKE 'чат занят%' THEN '' ELSE error END WHERE id=?",
                   (gq_id, reached, stamp, row["id"]))
        db.execute("UPDATE auto_schedule SET last_run_at=? WHERE id=?", (stamp, schedule_id))
        log.info("Расписание %s: тест %s «%s» запущен в чате %s (лобби #%s)",
                 schedule_id, test["id"], test["title"], sched["chat_id"], gq_id)
        return True
    if err == "already_running":
        if _chat_busy(sched["chat_id"]):
            # В чате идёт другой тест — вернём строку в очередь, её двинет on_quiz_done
            db.execute("UPDATE auto_schedule_runs SET status='queued', reached=?, error=? WHERE id=?",
                       (reached, "чат занят другим тестом — ждём его окончания", row["id"]))
            log.warning("Расписание %s: тест %s ждёт — в чате %s идёт другой тест",
                        schedule_id, test["id"], sched["chat_id"])
            return False
        # Мешал тест-сирота — он уже закрыт, пробуем ещё раз
        db.execute("UPDATE auto_schedule_runs SET status='queued' WHERE id=?", (row["id"],))
        return await _launch_next_locked(bot, schedule_id, slot_key)
    reason = (err or "лобби не создалось")[:300]
    db.execute("UPDATE auto_schedule_runs SET status='error', error=?, reached=? WHERE id=?",
               (reason, reached, row["id"]))
    log.error("АВТОЗАПУСК: расписание %s, тест %s «%s», чат %s — НЕ ЗАПУСТИЛСЯ: %s",
              schedule_id, test["id"], test["title"], sched["chat_id"], reason)
    await _notify_admin(bot, sched, f"⚠️ <b>Автозапуск: тест не запустился</b>\n\n"
                                    f"Расписание #{schedule_id}, тест «{test['title']}», чат "
                                    f"<code>{sched['chat_id']}</code>\nПричина: {reason}")
    return await _launch_next_locked(bot, schedule_id, slot_key)


async def _notify_admin(bot: Bot, sched: dict, text: str) -> None:
    admin = sched.get("created_by")
    if not admin:
        return
    try:
        await bot.send_message(int(admin), text, parse_mode="HTML")
    except Exception as e:
        log.warning("уведомление админу %s: %s", admin, e)


PAUSE_BETWEEN_TESTS = 20          # секунд между тестами одного запуска — увидеть итоги


async def on_quiz_done(bot: Bot, gq_id: int, cancelled: bool = False) -> None:
    """Групповой тест завершён (или отменён): записать итоги запуска и
    двинуть очередь расписаний этого чата."""
    gq = db.fetchone("SELECT * FROM group_quizzes WHERE id=?", (gq_id,))
    row = db.fetchone("SELECT * FROM auto_schedule_runs WHERE group_quiz_id=?", (gq_id,))
    if row:
        qcount = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (row["test_id"],))["c"]
        players = db.fetchall("SELECT * FROM group_quiz_players WHERE group_quiz_id=?", (gq_id,))
        started = len(players)
        finished = sum(1 for p in players
                       if (p["correct_answers"] or 0) + (p["wrong_answers"] or 0) >= max(1, qcount))
        db.execute("UPDATE auto_schedule_runs SET status=?, participants=?, started_count=?, "
                   "finished_count=?, finished_at=? WHERE id=?",
                   ("cancelled" if cancelled else "finished", started, started, finished,
                    _now_almaty().isoformat(timespec="seconds"), row["id"]))
    elif gq:
        # Тест не из расписания (запущен вручную) — старым расписаниям чата
        # по-прежнему считаем участников по чату
        sched = get_schedule_by_chat(gq["chat_id"])
        if sched:
            n = db.fetchone("SELECT COUNT(*) AS c FROM group_quiz_players WHERE group_quiz_id=?",
                            (gq_id,))["c"]
            update_run_participants(sched["id"], gq["test_id"], n)
    if not gq:
        return
    # Очередь этого чата: свой слот дальше, потом всё, что накопилось у
    # других расписаний того же чата
    waiting = db.fetchall(
        "SELECT DISTINCT r.schedule_id FROM auto_schedule_runs r JOIN auto_schedule s ON s.id=r.schedule_id "
        "WHERE s.chat_id=? AND s.status='active' AND r.status='queued' "
        "ORDER BY (r.schedule_id=?) DESC, r.id", (str(gq["chat_id"]), row["schedule_id"] if row else -1))
    for w in waiting:
        if PAUSE_BETWEEN_TESTS:
            await asyncio.sleep(PAUSE_BETWEEN_TESTS)
        if await launch_next(bot, w["schedule_id"]):
            return


async def _announce_and_launch(bot: Bot, test: dict, chat_id, bot_username: str,
                               category_id=None, channel_id=None, announce_delay=60):
    """Анонс на КАНАЛ и в ЧАТ, ожидание (чтобы зашли люди), затем лобби.
    Возвращает (ok, group_quiz_id, ошибка)."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    qc = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))['c']
    time_per_q = test.get('time_per_question') or 30
    cat = db.fetchone("SELECT name FROM test_categories WHERE id=?", (category_id,)) if category_id else None
    cat_name = cat['name'] if cat else "Общий"
    uname = bot_username.lstrip('@') if bot_username else ''
    bot_kb = None
    if uname:
        bot_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Перейти к тестированию", url=f"https://t.me/{uname}?start=quiz")]])
    delay_min = max(1, round(announce_delay / 60))
    if channel_id:
        try:
            await bot.send_message(
                int(channel_id),
                f"🆕 <b>Скоро тест по теме «{test['title']}»!</b>\n\n"
                f"📂 Раздел: {cat_name}\n❓ Вопросов: {qc}\n⏱ Время на вопрос: {time_per_q} сек\n\n"
                f"⏳ Старт через ~{delay_min} мин в нашем чате.\nЗаходи и участвуй! 💪",
                parse_mode="HTML", reply_markup=bot_kb)
        except Exception as e:
            log.warning("announce to channel %s: %s", channel_id, e)
    try:
        await bot.send_message(
            int(chat_id),
            f"🔔 <b>Внимание! Скоро начнётся тест!</b>\n\n"
            f"📚 Тема: «{test['title']}»\n📂 Раздел: {cat_name}\n❓ Вопросов: {qc}\n"
            f"⏱ Время на вопрос: {time_per_q} сек\n\n"
            f"⏳ Старт через ~{delay_min} мин — успей зайти!\nОтвечать будем прямо в чате 💪",
            parse_mode="HTML")
    except Exception as e:
        log.warning("announce to chat %s: %s", chat_id, e)
    await asyncio.sleep(announce_delay)
    await set_chat_slowmode(bot, chat_id, SLOWMODE_DURING_TEST)
    from services import group_quiz_service
    ok, key, gq_id = await group_quiz_service.start_lobby(
        bot, test, int(chat_id), admin_tg_id=0, language=test.get('language', 'ru'))
    if not ok:
        log.warning("start_lobby не запустил лобби: %s (chat=%s)", key, chat_id)
        return False, None, key
    return True, gq_id, None
