"""Что делал ученик (v97): последний вход, последнее действие, уроки, тесты, задания —
на сайте, в мини-приложении и в боте. Только чтение уже записанных данных."""
from typing import Optional

import database as db
import utils


def _t(v) -> str:
    return (str(v or "")[:16]).replace("T", " ")


def summary(tg_id: int) -> dict:
    u = utils.get_user_by_tg(tg_id) or {}
    uid = u.get("id") or -1
    login = db.fetchone("SELECT ts, device_label, ip, source FROM device_log WHERE user_tg=? AND event='login' "
                        "ORDER BY id DESC LIMIT 1", (tg_id,))
    seen = db.fetchone("SELECT MAX(last_seen_at) AS m FROM user_sessions WHERE user_tg=?", (tg_id,))
    cands = [u.get("last_activity_at"), seen["m"] if seen else None]
    for sql, args in (("SELECT MAX(COALESCE(end_time, start_time, created_at)) AS m FROM test_attempts WHERE user_id=?", (uid,)),
                      ("SELECT MAX(created_at) AS m FROM interactive_results WHERE user_id=?", (uid,)),
                      ("SELECT MAX(last_opened_at) AS m FROM lesson_progress WHERE user_tg_id=?", (tg_id,)),
                      ("SELECT MAX(created_at) AS m FROM study_events WHERE tg_id=?", (tg_id,))):
        r = db.fetchone(sql, args)
        cands.append(r["m"] if r else None)
    last = max((str(c).replace(" ", "T") for c in cands if c), default="")
    count = lambda sql, a: (db.fetchone(sql, a) or {"c": 0})["c"]  # noqa: E731
    return {
        "last_login": {"ts": _t(login["ts"]), "device": login["device_label"], "ip": login["ip"],
                       "source": login["source"]} if login else None,
        "last_action": _t(last),
        "lessons": count("SELECT COUNT(*) AS c FROM lesson_progress WHERE user_tg_id=?", (tg_id,)),
        "tests": count("SELECT COUNT(*) AS c FROM test_attempts WHERE user_id=? AND status='finished' "
                       "AND cloned_from_attempt_id IS NULL", (uid,)),
        "tasks": count("SELECT COUNT(DISTINCT task_id) AS c FROM interactive_results WHERE user_id=?", (uid,)),
        "homework": count("SELECT COUNT(*) AS c FROM study_events WHERE tg_id=? AND event='hw_done'", (tg_id,)),
    }


_EVENTS = {"note_open": "📖 открыл конспект в боте", "hw_start": "📝 начал домашнее задание",
           "hw_done": "✅ сдал домашнее задание", "visit": "👋 зашёл в бота"}


def timeline(tg_id: int, limit: int = 60) -> list:
    """Последние действия, новые сверху."""
    u = utils.get_user_by_tg(tg_id) or {}
    uid = u.get("id") or -1
    out = []
    for r in db.fetchall("SELECT p.first_opened_at, p.last_opened_at, p.open_count, l.title FROM lesson_progress p "
                         "LEFT JOIN lessons l ON l.id=p.lesson_id WHERE p.user_tg_id=? "
                         "ORDER BY COALESCE(p.last_opened_at, p.first_opened_at) DESC LIMIT ?", (tg_id, limit)):
        n = int(r["open_count"] or 1)
        out.append((r["last_opened_at"] or r["first_opened_at"], "урок",
                    f"📚 открыл урок «{r['title'] or 'удалён'}»" + (f" (всего {n} раз)" if n > 1 else "")))
    for r in db.fetchall("SELECT e.event, e.created_at, l.title FROM study_events e LEFT JOIN lessons l ON l.id=e.lesson_id "
                         "WHERE e.tg_id=? AND e.event IN ('note_open','hw_start','hw_done') ORDER BY e.id DESC LIMIT ?",
                         (tg_id, limit)):
        out.append((r["created_at"], "бот", _EVENTS.get(r["event"], r["event"]) + (f" «{r['title']}»" if r["title"] else "")))
    for r in db.fetchall("SELECT a.status, a.start_time, a.end_time, a.created_at, a.correct_answers, a.wrong_answers, a.score, "
                         "a.via, t.title FROM test_attempts a LEFT JOIN tests t ON t.id=a.test_id WHERE a.user_id=? "
                         "AND a.cloned_from_attempt_id IS NULL ORDER BY a.id DESC LIMIT ?", (uid, limit)):
        where = "сайт" if r["via"] in ("link", "preview", "site") else "бот или урок"
        if r["status"] == "finished":
            out.append((r["end_time"] or r["created_at"], "тест",
                        f"🧪 прошёл тест «{r['title'] or 'удалён'}»: верно {r['correct_answers'] or 0}, "
                        f"неверно {r['wrong_answers'] or 0}" + (f", баллы {r['score']}" if r["score"] else "")))
        else:
            out.append((r["start_time"] or r["created_at"], "тест", f"⏳ начал тест «{r['title'] or 'удалён'}» ({where})"))
    for r in db.fetchall("SELECT r.created_at, r.kind, r.score, r.max_score, r.percent, t.title, t.material FROM interactive_results r "
                         "LEFT JOIN interactive_tasks t ON t.id=r.task_id WHERE r.user_id=? ORDER BY r.id DESC LIMIT ?",
                         (uid, limit)):
        pct = f"{round(r['percent'])}%" if r["percent"] is not None else f"{r['score'] or 0} из {r['max_score'] or 0}"
        out.append((r["created_at"], "задание", f"🎮 выполнил задание «{r['title'] or 'удалено'}»: {pct}"
                                                 + (" (тренировка)" if r["kind"] == "practice" else "")))
    for r in db.fetchall("SELECT ts, event, device_label, ip FROM device_log WHERE user_tg=? AND event IN ('login','blocked') "
                         "ORDER BY id DESC LIMIT ?", (tg_id, limit)):
        out.append((r["ts"], "вход", ("🔐 вошёл: " if r["event"] == "login" else "⛔ не пущен (лимит): ")
                    + f"{r['device_label'] or ''} · {r['ip'] or ''}"))
    for r in db.fetchall("SELECT u.used_at, l.label, ls.title FROM lesson_link_uses u JOIN lesson_links l ON l.id=u.link_id "
                         "LEFT JOIN lessons ls ON ls.id=l.lesson_id WHERE u.user_tg=? ORDER BY u.id DESC LIMIT ?", (tg_id, limit)):
        out.append((r["used_at"], "ссылка", f"🔗 перешёл по ссылке «{r['label']}» на урок «{r['title'] or ''}»"))
    out = [(str(ts).replace(" ", "T"), kind, text) for ts, kind, text in out if ts]
    out.sort(key=lambda x: x[0], reverse=True)
    return [{"ts": _t(ts), "kind": kind, "text": text} for ts, kind, text in out[:limit]]
