"""
Live-режим: онлайн-викторина в реальном времени (стиль Kahoot).

Реалтайм — через короткий polling (клиент опрашивает /state каждую ~1с). Это
надёжнее WebSocket на Railway-прокси и «достаточно живо» для викторины. Все
расчёты (правильность, баллы по скорости, рейтинг) — ТОЛЬКО на сервере.

Ведущий (преподаватель) создаёт комнату из готового теста → код + ссылка + QR.
Ученики заходят по коду, ждут в лобби, отвечают синхронно, видят статистику и
итоговый топ-5.
"""
import asyncio
import json
import random
import string
from datetime import datetime, timezone

import aiohttp_jinja2
from aiohttp import web

import database as db
import utils
from webapp import auth


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ============ Хелперы комнаты ============

def _gen_code():
    for _ in range(20):
        code = "".join(random.choices(string.digits, k=6))
        if not db.fetchone("SELECT 1 FROM live_rooms WHERE code=?", (code,)):
            return code
    return "".join(random.choices(string.digits, k=8))


def _room_by_code(code):
    return db.fetchone("SELECT * FROM live_rooms WHERE code=?", (code,))


def _question_at(room, idx):
    try:
        qids = json.loads(room["question_order"] or "[]")
    except (ValueError, TypeError):
        qids = []
    if idx < 0 or idx >= len(qids):
        return None, len(qids)
    q = db.fetchone("SELECT * FROM questions WHERE id=?", (qids[idx],))
    return (dict(q) if q else None), len(qids)


def _options(qid, shuffle=False):
    opts = [dict(o) for o in db.fetchall(
        "SELECT id, text, is_correct FROM question_options WHERE question_id=? ORDER BY order_num, id",
        (qid,))]
    if shuffle:
        random.shuffle(opts)
    return opts


def _remaining_seconds(room):
    started = _parse(room["question_started_at"])
    if not started:
        return room["time_per_question"] or 20
    elapsed = (_now() - started).total_seconds()
    return max(0, (room["time_per_question"] or 20) - elapsed)


# ============ Скоринг (сервер) ============

def _score_for(room, correct, remaining_frac):
    """competitive: 500 + 500*доля_оставшегося_времени (мин 500 за правильный,
    макс 1000). study: 1 балл за правильный."""
    if not correct:
        return 0
    if room["mode"] == "study":
        return 1
    return int(round(500 + 500 * max(0.0, min(1.0, remaining_frac))))


# ============ Ученик: страницы ============

async def _login(request):
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        raise web.HTTPFound("/?error=login_required")
    return tg_id


async def live_join_page(request):
    """Страница ввода кода / автоподключение по /live/{code}."""
    await auth.get_logged_in_tg_id(request)
    ctx = await auth.nav_context(request)
    ctx["prefill_code"] = request.match_info.get("code", "")
    return aiohttp_jinja2.render_template("live_join.html", request, ctx)


async def live_play_page(request):
    tg_id = await _login(request)
    code = request.match_info["code"]
    room = await asyncio.to_thread(_room_by_code, code)
    if not room:
        raise web.HTTPFound("/live?error=notfound")
    ctx = await auth.nav_context(request)
    ctx["code"] = code
    ctx["room_mode"] = room["mode"]
    return aiohttp_jinja2.render_template("live_play.html", request, ctx)


# ============ Ученик: API ============

async def live_join(request):
    tg_id = await _login(request)
    code = request.match_info["code"]

    def _do():
        room = _room_by_code(code)
        if not room:
            return {"error": "notfound"}
        if room["status"] == "finished":
            return {"error": "finished"}
        if room["locked"] and not db.fetchone(
                "SELECT 1 FROM live_players WHERE room_id=? AND tg_id=?", (room["id"], tg_id)):
            return {"error": "locked"}
        u = utils.get_user_by_tg(tg_id)
        name = (u.get("username") and "@" + u["username"]) or (u.get("first_name") if u else None) or f"id{tg_id}"
        db.execute(
            "INSERT INTO live_players (room_id, tg_id, name, last_seen) VALUES (?,?,?,?) "
            "ON CONFLICT(room_id, tg_id) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen, kicked=0",
            (room["id"], tg_id, name, _iso(_now())))
        return {"ok": True}

    return web.json_response(await asyncio.to_thread(_do))


def _player_state(room, tg_id):
    """Состояние для ученика (без правильного ответа во время вопроса)."""
    me = db.fetchone("SELECT * FROM live_players WHERE room_id=? AND tg_id=?", (room["id"], tg_id))
    if me and me["kicked"]:
        return {"status": "kicked"}
    db.execute("UPDATE live_players SET last_seen=? WHERE room_id=? AND tg_id=?",
               (_iso(_now()), room["id"], tg_id))
    # рейтинг/место
    players = db.fetchall(
        "SELECT tg_id, name, score FROM live_players WHERE room_id=? AND kicked=0 "
        "ORDER BY score DESC, total_time_ms ASC", (room["id"],))
    total_players = len(players)
    place = next((i + 1 for i, p in enumerate(players) if p["tg_id"] == tg_id), None)
    my_score = me["score"] if me else 0

    out = {
        "status": room["status"],
        "mode": room["mode"],
        "place": place, "total_players": total_players, "my_score": my_score,
        "rating_visibility": room["rating_visibility"],
    }
    q, total = _question_at(room, room["current_index"])
    out["q_num"] = room["current_index"] + 1
    out["q_total"] = total

    if room["status"] == "question" and q:
        answered = db.fetchone(
            "SELECT option_id FROM live_answers WHERE room_id=? AND question_index=? AND tg_id=?",
            (room["id"], room["current_index"], tg_id))
        out["question"] = {
            "id": q["id"], "text": q["text"],
            "image": q.get("web_image_path"),
            "options": [{"id": o["id"], "text": o["text"]}
                        for o in _options(q["id"])],
        }
        out["remaining"] = round(_remaining_seconds(room), 1)
        out["time_total"] = room["time_per_question"]
        out["answered"] = bool(answered)
        out["answered_count"] = db.fetchone(
            "SELECT COUNT(*) c FROM live_answers WHERE room_id=? AND question_index=?",
            (room["id"], room["current_index"]))["c"]
    elif room["status"] == "stats" and q:
        out["stats"] = _question_stats(room, q)
        myans = db.fetchone(
            "SELECT option_id, is_correct, points FROM live_answers "
            "WHERE room_id=? AND question_index=? AND tg_id=?",
            (room["id"], room["current_index"], tg_id))
        out["my_answer"] = ({"option_id": myans["option_id"], "correct": bool(myans["is_correct"]),
                             "points": myans["points"]} if myans else None)
        out["explanation"] = q.get("explanation") or ""
    elif room["status"] == "finished":
        out["podium"] = _leaderboard(room, limit=5)
        out["me_final"] = _me_final(room, tg_id)
    return out


def _question_stats(room, q):
    opts = _options(q["id"])
    counts = {o["id"]: 0 for o in opts}
    rows = db.fetchall(
        "SELECT option_id, COUNT(*) c FROM live_answers WHERE room_id=? AND question_index=? "
        "GROUP BY option_id", (room["id"], room["current_index"]))
    answered = 0
    for r in rows:
        if r["option_id"] in counts:
            counts[r["option_id"]] = r["c"]
        answered += r["c"]
    total_players = db.fetchone(
        "SELECT COUNT(*) c FROM live_players WHERE room_id=? AND kicked=0", (room["id"],))["c"]
    return {
        "options": [{"id": o["id"], "text": o["text"], "is_correct": bool(o["is_correct"]),
                     "count": counts[o["id"]],
                     "percent": round(counts[o["id"]] / answered * 100) if answered else 0}
                    for o in opts],
        "answered": answered,
        "not_answered": max(0, total_players - answered),
        "total_players": total_players,
    }


def _leaderboard(room, limit=None):
    q = ("SELECT tg_id, name, score, correct, best_streak, total_time_ms "
         "FROM live_players WHERE room_id=? AND kicked=0 "
         "ORDER BY score DESC, total_time_ms ASC")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = db.fetchall(q, (room["id"],))
    return [{"tg_id": r["tg_id"], "name": r["name"], "score": r["score"],
             "correct": r["correct"], "best_streak": r["best_streak"]} for r in rows]


def _me_final(room, tg_id):
    me = db.fetchone("SELECT * FROM live_players WHERE room_id=? AND tg_id=?", (room["id"], tg_id))
    if not me:
        return None
    _, total = _question_at(room, 0)
    total_q = total
    answered = db.fetchone(
        "SELECT COUNT(*) c FROM live_answers WHERE room_id=? AND tg_id=?", (room["id"], tg_id))["c"]
    players = db.fetchall(
        "SELECT tg_id FROM live_players WHERE room_id=? AND kicked=0 "
        "ORDER BY score DESC, total_time_ms ASC", (room["id"],))
    place = next((i + 1 for i, p in enumerate(players) if p["tg_id"] == tg_id), None)
    avg_ms = round(me["total_time_ms"] / answered) if answered else 0
    return {
        "place": place, "total": len(players), "score": me["score"],
        "correct": me["correct"], "wrong": max(0, answered - me["correct"]),
        "skipped": max(0, total_q - answered),
        "percent": round(me["correct"] / total_q * 100) if total_q else 0,
        "best_streak": me["best_streak"], "avg_sec": round(avg_ms / 1000, 1),
    }


async def live_state(request):
    tg_id = await _login(request)
    code = request.match_info["code"]

    def _do():
        room = _room_by_code(code)
        if not room:
            return {"error": "notfound"}
        return _player_state(room, tg_id)

    return web.json_response(await asyncio.to_thread(_do))


async def live_answer(request):
    tg_id = await _login(request)
    code = request.match_info["code"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    option_id = body.get("option_id")

    def _do():
        room = _room_by_code(code)
        if not room or room["status"] != "question":
            return {"error": "closed"}
        idx = room["current_index"]
        q, _ = _question_at(room, idx)
        if not q:
            return {"error": "closed"}
        # анти-дабл: один ответ на вопрос
        if db.fetchone("SELECT 1 FROM live_answers WHERE room_id=? AND question_index=? AND tg_id=?",
                       (room["id"], idx, tg_id)):
            return {"ok": True, "duplicate": True}
        remaining = _remaining_seconds(room)
        if remaining <= 0:
            return {"error": "timeout"}
        opt = db.fetchone("SELECT is_correct FROM question_options WHERE id=? AND question_id=?",
                          (option_id, q["id"]))
        if not opt:
            return {"error": "bad_option"}
        is_correct = bool(opt["is_correct"])
        tpq = room["time_per_question"] or 20
        frac = remaining / tpq if tpq else 0
        answer_ms = int((tpq - remaining) * 1000)
        points = _score_for(room, is_correct, frac)
        try:
            db.execute(
                "INSERT INTO live_answers (room_id, question_index, question_id, tg_id, option_id, "
                "is_correct, answer_ms, points) VALUES (?,?,?,?,?,?,?,?)",
                (room["id"], idx, q["id"], tg_id, option_id, 1 if is_correct else 0,
                 answer_ms, points))
        except Exception:
            return {"ok": True, "duplicate": True}  # гонка
        # обновляем игрока
        if is_correct:
            db.execute(
                "UPDATE live_players SET score=score+?, correct=correct+1, streak=streak+1, "
                "best_streak=MAX(best_streak, streak+1), total_time_ms=total_time_ms+? "
                "WHERE room_id=? AND tg_id=?",
                (points, answer_ms, room["id"], tg_id))
        else:
            db.execute(
                "UPDATE live_players SET streak=0, total_time_ms=total_time_ms+? "
                "WHERE room_id=? AND tg_id=?", (answer_ms, room["id"], tg_id))
        return {"ok": True, "accepted": True}

    return web.json_response(await asyncio.to_thread(_do))


# ============ Ведущий: страницы и API ============

async def _require_admin(request):
    from webapp import learning
    return await learning._require_admin(request)


async def live_create(request):
    tg_id = await _require_admin(request)
    test_id = int(request.match_info["test_id"])

    def _do():
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
        if not test:
            return None
        qs = db.fetchall("SELECT id FROM questions WHERE test_id=? ORDER BY order_num, id", (test_id,))
        if not qs:
            return "no_questions"
        qids = [q["id"] for q in qs]
        if test["shuffle_questions"]:
            random.shuffle(qids)
        code = _gen_code()
        db.execute(
            "INSERT INTO live_rooms (code, test_id, host_tg_id, question_order, time_per_question) "
            "VALUES (?,?,?,?,?)",
            (code, test_id, tg_id, json.dumps(qids), test["time_per_question"] or 20))
        return code

    res = await asyncio.to_thread(_do)
    if res is None:
        raise web.HTTPNotFound(text="Тест не найден")
    if res == "no_questions":
        raise web.HTTPFound("/admin/live?error=no_questions")
    raise web.HTTPFound(f"/live/host/{res}")


async def live_host_page(request):
    tg_id = await _require_admin(request)
    code = request.match_info["code"]
    room = await asyncio.to_thread(_room_by_code, code)
    if not room or room["host_tg_id"] != tg_id:
        raise web.HTTPForbidden(text="Это не ваша комната")
    ctx = await auth.nav_context(request)
    ctx["code"] = code
    ctx["bot_username"] = None
    test = await asyncio.to_thread(db.fetchone, "SELECT title FROM tests WHERE id=?", (room["test_id"],))
    ctx["test_title"] = test["title"] if test else ""
    return aiohttp_jinja2.render_template("live_host.html", request, ctx)


async def live_pick_page(request):
    """Иерархия для запуска Live: предмет → раздел → тест урока.
    Плюс отдельный список прочих тестов (не привязанных к урокам)."""
    tg_id = await _require_admin(request)

    def _data():
        subjects = []
        for s in db.fetchall("SELECT * FROM subjects WHERE status='active' ORDER BY sort_order, id"):
            secs = []
            for sec in db.fetchall(
                    "SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (s["id"],)):
                lessons = db.fetchall(
                    "SELECT l.id lid, l.title ltitle, l.test_id, "
                    "(SELECT COUNT(*) FROM questions WHERE test_id=l.test_id) qn "
                    "FROM lessons l WHERE l.section_id=? AND l.test_id IS NOT NULL AND COALESCE(l.is_zachet,0)=0 "
                    "ORDER BY l.sort_order, l.id", (sec["id"],))
                tests = [dict(l) for l in lessons if l["qn"]]
                if tests:
                    secs.append({"title": sec["title"], "tests": tests})
            if secs:
                subjects.append({"title": s["title"], "sections": secs})
        # прочие тесты (созданные в боте, не в уроках)
        others = db.fetchall(
            "SELECT id, title, (SELECT COUNT(*) FROM questions WHERE test_id=t.id) qn "
            "FROM tests t WHERE (created_by=? OR ?=1) "
            "AND id NOT IN (SELECT test_id FROM lessons WHERE test_id IS NOT NULL) "
            "ORDER BY id DESC LIMIT 60",
            (tg_id, 1 if utils.is_owner(tg_id) else 0))
        others = [dict(o) for o in others if o["qn"]]
        return subjects, others

    subjects, others = await asyncio.to_thread(_data)
    ctx = await auth.nav_context(request)
    ctx["subjects"] = subjects
    ctx["others"] = others
    ctx["error"] = request.query.get("error")
    return aiohttp_jinja2.render_template("live_pick.html", request, ctx)


def _host_guard(code, tg_id):
    room = _room_by_code(code)
    if not room or room["host_tg_id"] != tg_id:
        return None
    return room


async def live_host_state(request):
    tg_id = await _require_admin(request)
    code = request.match_info["code"]

    def _do():
        room = _host_guard(code, tg_id)
        if not room:
            return {"error": "forbidden"}
        players = db.fetchall(
            "SELECT tg_id, name, score, correct, best_streak, last_seen FROM live_players "
            "WHERE room_id=? AND kicked=0 ORDER BY score DESC, total_time_ms ASC", (room["id"],))
        now = _now()
        plist = []
        online = 0
        for p in players:
            seen = _parse(p["last_seen"])
            is_online = seen and (now - seen).total_seconds() < 10
            if is_online:
                online += 1
            plist.append({"tg_id": p["tg_id"], "name": p["name"], "score": p["score"],
                          "correct": p["correct"], "online": bool(is_online)})
        out = {"status": room["status"], "code": code, "mode": room["mode"],
               "locked": bool(room["locked"]), "players": plist,
               "player_count": len(plist), "online": online,
               "time_per_question": room["time_per_question"],
               "rating_visibility": room["rating_visibility"]}
        q, total = _question_at(room, room["current_index"])
        out["q_num"] = room["current_index"] + 1
        out["q_total"] = total
        if room["status"] == "question" and q:
            out["question_text"] = q["text"]
            out["remaining"] = round(_remaining_seconds(room), 1)
            out["time_total"] = room["time_per_question"]
            out["answered_count"] = db.fetchone(
                "SELECT COUNT(*) c FROM live_answers WHERE room_id=? AND question_index=?",
                (room["id"], room["current_index"]))["c"]
            # раскладка по вариантам (для ведущего — во время вопроса, без выделения верного ученикам)
            out["live_counts"] = _question_stats(room, q)["options"]
        elif room["status"] == "stats" and q:
            out["stats"] = _question_stats(room, q)
            out["question_text"] = q["text"]
        elif room["status"] == "finished":
            out["podium"] = _leaderboard(room, limit=5)
        return out

    return web.json_response(await asyncio.to_thread(_do))


def _advance_to_question(room, idx):
    db.execute(
        "UPDATE live_rooms SET status='question', current_index=?, question_started_at=? WHERE id=?",
        (idx, _iso(_now()), room["id"]))


async def live_host_action(request):
    tg_id = await _require_admin(request)
    code = request.match_info["code"]
    action = request.match_info["action"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    def _do():
        room = _host_guard(code, tg_id)
        if not room:
            return {"error": "forbidden"}
        idx = room["current_index"]
        _, total = _question_at(room, 0)

        if action == "start":
            _advance_to_question(room, 0)
        elif action == "skip" or action == "reveal":
            # завершить приём — показать статистику
            db.execute("UPDATE live_rooms SET status='stats' WHERE id=?", (room["id"],))
        elif action == "next":
            nxt = idx + 1
            if nxt >= total:
                db.execute("UPDATE live_rooms SET status='finished' WHERE id=?", (room["id"],))
            else:
                _advance_to_question(room, nxt)
        elif action == "finish":
            db.execute("UPDATE live_rooms SET status='finished' WHERE id=?", (room["id"],))
        elif action == "lock":
            db.execute("UPDATE live_rooms SET locked=1-locked WHERE id=?", (room["id"],))
        elif action == "kick":
            target = body.get("tg_id")
            if target:
                db.execute("UPDATE live_players SET kicked=1 WHERE room_id=? AND tg_id=?",
                           (room["id"], int(target)))
        elif action == "settings":
            mode = body.get("mode")
            vis = body.get("rating_visibility")
            tpq = body.get("time_per_question")
            if mode in ("competitive", "study"):
                db.execute("UPDATE live_rooms SET mode=? WHERE id=?", (mode, room["id"]))
            if vis in ("full", "top5", "self", "hidden"):
                db.execute("UPDATE live_rooms SET rating_visibility=? WHERE id=?", (vis, room["id"]))
            if isinstance(tpq, int) and 5 <= tpq <= 300:
                db.execute("UPDATE live_rooms SET time_per_question=? WHERE id=?", (tpq, room["id"]))
        return {"ok": True}

    return web.json_response(await asyncio.to_thread(_do))


def register_routes(app):
    # ученик
    app.router.add_get("/live", live_join_page)
    app.router.add_get("/live/join/{code:\\d+}", live_join_page)
    app.router.add_get("/live/{code:\\d+}", live_play_page)
    app.router.add_post("/api/live/{code:\\d+}/join", live_join)
    app.router.add_get("/api/live/{code:\\d+}/state", live_state)
    app.router.add_post("/api/live/{code:\\d+}/answer", live_answer)
    # ведущий
    app.router.add_get("/admin/live", live_pick_page)
    app.router.add_post("/admin/live/create/{test_id:\\d+}", live_create)
    app.router.add_get("/live/host/{code:\\d+}", live_host_page)
    app.router.add_get("/api/live/{code:\\d+}/host/state", live_host_state)
    app.router.add_post("/api/live/{code:\\d+}/host/{action}", live_host_action)
