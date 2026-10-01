"""
Диагностика «зависаний»: что именно остановило сервер, какие запросы шли
медленно, какие ошибки случались у учеников в браузере.

Зачем. Бот и сайт живут в одном процессе. Если какая-то функция надолго
занимает цикл событий (тяжёлый расчёт прямо в обработчике), то в это время
не отвечает НИЧЕГО: страницы не открываются, кнопки «не нажимаются». Снаружи
это выглядит как «приложение зависло», а в логах — тишина. Этот модуль:

  • следит за циклами бота и сайта из отдельного потока и, если цикл стоит
    дольше порога, записывает, КАКАЯ строка кода его держит;
  • собирает медленные запросы, ошибки сервера и ошибки из браузера;
  • хранит последние события в памяти и в таблице diag_events — их видно
    админу на странице /admin/health.

Ничего тяжёлого в самих циклах не делает: запись в базу идёт из потока сторожа.
"""
import json
import logging
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))
STALL_SECONDS = 1.5            # цикл не отвечает дольше — это зависание
KEEP_ROWS = 3000               # сколько событий храним в базе
KINDS = {
    "stall": "🧊 Сервер не отвечал",
    "slow": "🐢 Медленный запрос",
    "error": "💥 Ошибка сервера",
    "client": "📱 Ошибка в браузере",
    "auth": "🔑 Потеря входа",
    "net": "📶 Сбой запроса у ученика",
}

_events = deque(maxlen=400)     # последние события — в памяти
_pending = []                   # ждут записи в базу
_pending_lock = threading.Lock()
_loops = {}                     # имя → состояние сторожа
_started = False
_booted_at = time.time()


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# Секреты в журнал не попадают никогда: токен бота, данные входа Telegram
# (initData / tgWebAppData с подписью), пароли и коды из адресов.
_SECRET_RES = (
    (re.compile(r"(tgWebAppData|init_?data|initData)\s*[=:]\s*\S+", re.I), r"\1=***"),
    (re.compile(r"\b(hash|signature|query_id|auth_date|password|passwd|token|code|secret)=[^&\s\"']+", re.I), r"\1=***"),
    (re.compile(r"\b(bot)?\d{3,12}:[A-Za-z0-9_-]{30,}"), "***"),
    (re.compile(r"user=%7B[^&\s\"']+", re.I), "user=***"),
)


def scrub(value):
    """Убрать из текста всё, что похоже на секрет."""
    if not isinstance(value, str):
        return value
    for rx, repl in _SECRET_RES:
        value = rx.sub(repl, value)
    return value


def record(kind: str, text: str, **meta) -> None:
    """Запомнить событие. Вызывать можно откуда угодно: не блокирует и не бросает."""
    try:
        ev = {"kind": kind, "text": scrub(str(text))[:1500],
              "meta": {k: scrub(v) for k, v in meta.items() if v not in (None, "")},
              "at": now_str()}
        _events.append(ev)
        with _pending_lock:
            if len(_pending) < 500:
                _pending.append(ev)
    except Exception:
        pass


def recent(kind: str = None, limit: int = 100) -> list:
    """Последние события: сначала из базы (переживают перезапуск), иначе из памяти."""
    try:
        import database as db
        sql, args = "SELECT kind, text, meta, created_at FROM diag_events", []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        rows = db.fetchall(sql + " ORDER BY id DESC LIMIT ?", tuple(args) + (int(limit),))
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta"] or "{}")
            except ValueError:
                meta = {}
            out.append({"kind": r["kind"], "text": r["text"], "meta": meta, "at": r["created_at"]})
        with _pending_lock:
            fresh = [e for e in reversed(_pending) if not kind or e["kind"] == kind]
        return (fresh + out)[:limit]
    except Exception:
        items = [e for e in reversed(_events) if not kind or e["kind"] == kind]
        return items[:limit]


def counts(hours: int = 24) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    out = {k: 0 for k in KINDS}
    try:
        import database as db
        for r in db.fetchall("SELECT kind, COUNT(*) AS n FROM diag_events WHERE created_at>=? GROUP BY kind", (since,)):
            out[r["kind"]] = r["n"]
    except Exception:
        for e in _events:
            if e["at"] >= since:
                out[e["kind"]] = out.get(e["kind"], 0) + 1
    return out


def slow_endpoints(hours: int = 24, limit: int = 12) -> list:
    """Какие адреса чаще всего отвечали медленно."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    agg = {}
    try:
        import database as db
        rows = db.fetchall("SELECT meta FROM diag_events WHERE kind='slow' AND created_at>=? ORDER BY id DESC LIMIT 2000",
                           (since,))
    except Exception:
        rows = []
    for r in rows:
        try:
            m = json.loads(r["meta"] or "{}")
        except ValueError:
            continue
        key = m.get("route") or m.get("path") or "?"
        a = agg.setdefault(key, {"route": key, "n": 0, "max_ms": 0, "sum_ms": 0})
        ms = int(m.get("ms") or 0)
        a["n"] += 1
        a["sum_ms"] += ms
        a["max_ms"] = max(a["max_ms"], ms)
    out = sorted(agg.values(), key=lambda a: -a["n"])[:limit]
    for a in out:
        a["avg_ms"] = a["sum_ms"] // max(1, a["n"])
    return out


def uptime_text() -> str:
    sec = int(time.time() - _booted_at)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d} д " if d else "") + f"{h} ч {rem // 60} мин"


def fmt_local(value: str) -> str:
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(ALMATY).strftime("%d.%m %H:%M:%S")
    except (ValueError, TypeError):
        return str(value or "")


# ───────────────────────── сторож циклов ─────────────────────────

def watch_loop(name: str, loop) -> None:
    """Поставить цикл событий под наблюдение (вызывать из потока этого цикла)."""
    _loops[name] = {"loop": loop, "thread": threading.get_ident(), "ack": time.monotonic(),
                    "sent": 0.0, "stalled": False, "stack": ""}
    _ensure_thread()


def _ensure_thread() -> None:
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_watchdog, name="diag-watchdog", daemon=True)
    t.start()


def _stack_of(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return ""
    lines = traceback.format_stack(frame)
    # Хвост стека, без внутренностей asyncio: именно там видно виновника
    keep = [l for l in lines if "/asyncio/" not in l and "\\asyncio\\" not in l][-8:]
    return "".join(keep).strip()


def _short_stack(stack: str) -> str:
    """«файл:строка функция» последних кадров — одной строкой для списка."""
    out = []
    for line in stack.splitlines():
        line = line.strip()
        if line.startswith('File "'):
            try:
                path, rest = line[6:].split('", line ', 1)
                num, fn = rest.split(", in ", 1)
                out.append(f"{path.replace(chr(92), '/').split('/')[-1]}:{num} {fn}")
            except ValueError:
                continue
    return " ← ".join(reversed(out[-4:]))


def _watchdog() -> None:
    last_flush = time.monotonic()
    while True:
        time.sleep(0.5)
        now = time.monotonic()
        for name, st in list(_loops.items()):
            try:
                loop = st["loop"]
                if loop.is_closed():
                    _loops.pop(name, None)
                    continue
                if st["sent"] <= st["ack"]:
                    # прошлый «пульс» получен — шлём новый
                    if st["stalled"]:
                        dur = now - st["stall_from"]
                        record("stall", f"Цикл «{name}» не отвечал {dur:.1f} с: {_short_stack(st['stack']) or 'стек не снят'}",
                               loop=name, seconds=round(dur, 1), stack=st["stack"][-1800:])
                        log.error("ЗАВИСАНИЕ: цикл «%s» не отвечал %.1f с\n%s", name, dur, st["stack"])
                        st["stalled"] = False
                    st["sent"] = now

                    def _ack(s=st):
                        s["ack"] = time.monotonic()
                    loop.call_soon_threadsafe(_ack)
                elif now - st["sent"] > STALL_SECONDS and not st["stalled"]:
                    st["stalled"] = True
                    st["stall_from"] = st["sent"]
                    st["stack"] = _stack_of(st["thread"])
            except Exception as e:
                log.debug("сторож цикла %s: %s", name, e)
        if now - last_flush >= 3:
            last_flush = now
            _flush()


def _flush() -> None:
    with _pending_lock:
        batch = list(_pending)
        del _pending[:]
    if not batch:
        return
    try:
        import database as db
        db.executemany("INSERT INTO diag_events (kind, text, meta, created_at) VALUES (?,?,?,?)",
                       [(e["kind"], e["text"], json.dumps(e["meta"], ensure_ascii=False)[:4000], e["at"]) for e in batch])
        db.execute("DELETE FROM diag_events WHERE id <= (SELECT MAX(id) FROM diag_events) - ?", (KEEP_ROWS,))
    except Exception as e:
        log.debug("запись диагностики: %s", e)


def start() -> None:
    """Запустить поток сторожа (запись событий в базу идёт из него)."""
    _ensure_thread()
