"""
Прохождение теста на сайте по персональной ссылке и общий учёт попыток.

Платформа (бот) и сайт ведут попытки в одной таблице test_attempts, поэтому
ответы, баллы, статус и число попыток у них общие. Этот модуль добавляет то,
чего не хватало:

  • переключатель теста «Разрешить прохождение на сайте» (tests.allow_web);
  • персональную ссылку: случайный токен, в базе только его хэш; ссылка привязана
    к ученику, тесту и попытке, а доступ всё равно проверяется на сервере;
  • «аренду» попытки (lease): открыли тест в двух вкладках — работает последняя,
    старая получает отказ и ничего не портит;
  • серверные дедлайны: время на вопрос и общее время не обнуляются от
    перезагрузки страницы и переходов между платформой и сайтом;
  • передачу начатой в боте попытки на сайт без новой попытки и потери ответов.

Всё, что решает доступ и принадлежность, решается здесь, на сервере. Ссылка
сама по себе доступа не даёт.
"""
import hashlib
import json
import logging
import random
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import database as db
import utils
from services import typed_answers as ta

log = logging.getLogger(__name__)

LINK_TTL_DAYS = 7           # срок жизни ссылки
LATE_GRACE_S = 3            # запас на сеть: ответ чуть позже дедлайна ещё принимаем
ACTIVE = ("in_progress", "idle", "paused")


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace(" ", "T"))
    except (TypeError, ValueError):
        return None


# ───────────────────────── переключатель «на сайте» ─────────────────────────

def linked_lessons(test_id: int) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT l.id, l.status FROM lessons l WHERE l.test_id=? ORDER BY l.id", (int(test_id),))]


def web_allowed(test: dict) -> bool:
    """Можно ли начинать новые попытки на сайте.

    Явное значение админа главнее. Если его нет: тест урока и личный тест на
    сайте открывались всегда (так и остаётся), остальные проходят только на платформе."""
    v = test.get("allow_web")
    if v is not None:
        return bool(v)
    return bool(test.get("is_private")) or bool(linked_lessons(test["id"]))


def web_explicit(test: dict) -> bool:
    """Админ сам включил переключатель. Только тогда карточка теста в боте
    показывает «Перейти на сайт»: у остальных тестов кнопки прежние."""
    return test.get("allow_web") == 1


def requires_web(test_id: int) -> bool:
    """В тесте есть вопросы, которые бот показать не может (ввод, сопоставление,
    последовательность). Такой тест проходят только на сайте."""
    row = db.fetchone(
        "SELECT 1 FROM questions WHERE test_id=? AND q_type IN ('text','matching','ordering') LIMIT 1",
        (int(test_id),))
    return bool(row)


# ───────────────────────── доступ к тесту ─────────────────────────

def access_state(user: dict, tg_id: int, test: dict) -> str:
    """'ok' или причина отказа: not_found, private, category, paid, banned.

    Правила те же, что у бота (карточка теста и кнопка «Пройти»)."""
    if not test or test.get("status") != "active":
        return "not_found"
    if utils.is_site_admin(tg_id):
        return "ok"
    try:
        from services import appeal_service
        banned, _until = appeal_service.is_user_banned(user["id"])
        if banned:
            return "banned"
    except Exception:
        pass
    if test.get("is_private"):
        from webapp import learning
        if not learning._has_private_test_access_sync(test["id"], tg_id):
            return "private"
    if test.get("category_id"):
        from services import category_service
        if not category_service.has_access(test["category_id"], tg_id):
            return "category"
    if test.get("is_paid") and not utils.has_paid_access(user["id"], test_id=test["id"]):
        from services import referral_service
        if not referral_service.user_can_unlock_paid_test(user["id"]):
            return "paid"
    return "ok"


def required_channels(test: dict) -> list:
    """Каналы, на которые надо быть подписанным (глобальные и раздела), как в боте."""
    from services import subscription_service as ss
    chans = list(ss.get_global_channels())
    if test.get("category_id"):
        chans += ss.get_category_channels(test["category_id"])
    seen, out = set(), []
    for c in chans:
        key = (c or "").lstrip("@").lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def attempts_used(user_id: int, test_id: int) -> int:
    """Сколько попыток уже потрачено: так же считает бот (завершённые и брошенные)."""
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_attempts WHERE user_id=? AND test_id=? "
        "AND status IN ('finished','aborted') AND COALESCE(via,'')<>'preview' "
        "AND COALESCE(attempt_num,0)<>999", (int(user_id), int(test_id)))
    return row["c"] if row else 0


def attempts_left(user: dict, test: dict) -> Optional[int]:
    limit = int(test.get("attempts_limit") or 0)
    if not limit:
        return None
    return max(0, limit - attempts_used(user["id"], test["id"]))


# ───────────────────────── персональные ссылки ─────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_link(user_id: int, test_id: int, attempt_id: Optional[int] = None) -> str:
    """Выдать ученику новую ссылку на этот тест. Токен возвращается один раз."""
    token = secrets.token_urlsafe(24)
    db.execute(
        "INSERT INTO test_web_links (token_hash, user_id, test_id, attempt_id, expires_at) "
        "VALUES (?,?,?,?,?)",
        (hash_token(token), int(user_id), int(test_id), attempt_id,
         _iso(_now() + timedelta(days=LINK_TTL_DAYS))))
    return token


def resolve_link(token: str) -> Optional[dict]:
    """Ссылка по токену, если она действует. Иначе None."""
    if not token or len(token) > 100:
        return None
    row = db.fetchone("SELECT * FROM test_web_links WHERE token_hash=?", (hash_token(token),))
    if not row:
        return None
    row = dict(row)
    if row.get("revoked"):
        return None
    exp = _parse(row.get("expires_at"))
    if exp and exp < _now():
        return None
    return row


def touch_link(link_id: int, attempt_id: Optional[int] = None) -> None:
    if attempt_id:
        db.execute("UPDATE test_web_links SET attempt_id=?, uses=uses+1, last_used_at=? WHERE id=?",
                   (attempt_id, _iso(_now()), link_id))
    else:
        db.execute("UPDATE test_web_links SET uses=uses+1, last_used_at=? WHERE id=?",
                   (_iso(_now()), link_id))


def revoke_links(test_id: int) -> int:
    cur = db.execute("UPDATE test_web_links SET revoked=1 WHERE test_id=? AND revoked=0", (int(test_id),))
    return getattr(cur, "rowcount", 0) or 0


# ───────────────────────── попытки ─────────────────────────

def find_active_attempt(user_id: int, test_id: int) -> Optional[dict]:
    row = db.fetchone(
        "SELECT * FROM test_attempts WHERE user_id=? AND test_id=? AND status IN ('in_progress','idle','paused') "
        "AND COALESCE(publication_id,0)=0 AND COALESCE(via,'')<>'preview' "
        "AND COALESCE(attempt_num,0)<>999 ORDER BY id DESC LIMIT 1", (int(user_id), int(test_id)))
    return dict(row) if row else None


def usable_questions(test_id: int) -> list:
    """Вопросы, которые можно показать. Битые (нет вариантов, нет правильного
    ответа, нет данных) пропускаются и никогда не попадают в попытку."""
    out = []
    for q in db.fetchall("SELECT * FROM questions WHERE test_id=? ORDER BY order_num, id", (int(test_id),)):
        q = dict(q)
        if not (q.get("text") or "").strip():
            continue
        kind = ta.question_kind(q)
        if kind in ta.TYPED_TYPES:
            _p, err = ta.clean_payload(kind, ta.load_payload(q))
            if err:
                continue
        else:
            opts = db.fetchall("SELECT is_correct FROM question_options WHERE question_id=?", (q["id"],))
            if len(opts) < 2 or not any(o["is_correct"] for o in opts):
                continue
        out.append(q)
    return out


def create_attempt(user: dict, test: dict, via: str = "link") -> Optional[int]:
    """Новая попытка на сайте. Лимит попыток и бан проверяет вызывающий."""
    questions = usable_questions(test["id"])
    if not questions:
        return None
    if test.get("shuffle_questions"):
        random.shuffle(questions)
    qids = [q["id"] for q in questions]
    options_order = {}
    for q in questions:
        kind = ta.question_kind(q)
        if kind in ta.TYPED_TYPES:
            order = ta.shuffled_order(q)
            if order:
                options_order[str(q["id"])] = order
        elif test.get("shuffle_options"):
            ids = [o["id"] for o in db.fetchall(
                "SELECT id FROM question_options WHERE question_id=? ORDER BY order_num, id", (q["id"],))]
            random.shuffle(ids)
            options_order[str(q["id"])] = ids
    preview = via == "preview"
    used = 0 if preview else attempts_used(user["id"], test["id"])
    is_first = used == 0
    is_counted = 0 if preview else (0 if (test.get("first_attempt_only") and not is_first) else 1)
    now = _now()
    limit_min = int(test.get("time_limit_min") or 0)
    cur = db.execute(
        "INSERT INTO test_attempts (user_id, test_id, current_question_index, question_order, options_order, "
        "start_time, status, language, attempt_num, is_first_attempt, is_counted, via, deadline_at, max_score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], test["id"], 0, json.dumps(qids), json.dumps(options_order), _iso(now),
         "in_progress", user.get("language") or test.get("language") or "ru",
         998 if preview else used + 1, 1 if is_first else 0, is_counted, via,
         _iso(now + timedelta(minutes=limit_min)) if limit_min else None,
         sum(ta.points_of(q) for q in questions)))
    return cur.lastrowid


def resume_if_paused(attempt: dict) -> dict:
    """Пауза (в боте или из-за серии пропусков) на сайте не держит: продолжаем с места остановки."""
    if attempt["status"] in ("paused", "idle"):
        db.execute("UPDATE test_attempts SET status='in_progress', pause_time=NULL, "
                   "missed_questions_counter=0 WHERE id=?", (attempt["id"],))
        attempt = dict(attempt)
        attempt["status"] = "in_progress"
    return attempt


# ───────────────────────── «аренда» попытки: две вкладки ─────────────────────────

def issue_lease(attempt_id: int) -> str:
    token = secrets.token_hex(16)
    db.execute("UPDATE test_attempts SET lease_token=?, lease_at=? WHERE id=?",
               (token, _iso(_now()), attempt_id))
    return token


def lease_required(attempt: dict) -> bool:
    return (attempt.get("via") or "") in ("link", "preview")


def lease_ok(attempt: dict, token: Optional[str]) -> bool:
    """Попытка с сайта по ссылке принимает ответы только от самой свежей вкладки.
    Старые попытки (уроки, бот) аренды не знают и работают как раньше."""
    if not lease_required(attempt):
        return True
    have = attempt.get("lease_token") or ""
    return bool(have) and secrets.compare_digest(have, token or "")


# ───────────────────────── серверные дедлайны ─────────────────────────

def seconds_per_question(test: dict) -> int:
    try:
        return max(0, int(test.get("time_per_question") or 0))
    except (TypeError, ValueError):
        return 0


def question_clock(attempt: dict, test: dict, question_id: int) -> Optional[int]:
    """Сколько миллисекунд осталось на этот вопрос. Время запускается один раз,
    при первом показе, и дальше только идёт: перезагрузка страницы, возврат на
    сайт или переход с платформы его не обнуляют. None — на вопрос нет лимита."""
    tpq = seconds_per_question(test)
    if not tpq:
        return None
    now = _now()
    dl = _parse(attempt.get("q_deadline_at"))
    if attempt.get("q_deadline_qid") == question_id and dl:
        return int((dl - now).total_seconds() * 1000)
    db.execute("UPDATE test_attempts SET q_deadline_qid=?, q_deadline_at=? WHERE id=?",
               (question_id, _iso(now + timedelta(seconds=tpq)), attempt["id"]))
    return tpq * 1000


def overall_left_ms(attempt: dict) -> Optional[int]:
    dl = _parse(attempt.get("deadline_at"))
    if not dl:
        return None
    return int((dl - _now()).total_seconds() * 1000)


def answer_is_late(attempt: dict, test: dict, question_id: int) -> bool:
    """Ответ пришёл после дедлайна вопроса (с запасом на сеть) — это пропуск."""
    if (attempt.get("via") or "") not in ("link", "preview"):
        return False
    dl = _parse(attempt.get("q_deadline_at"))
    if seconds_per_question(test) and attempt.get("q_deadline_qid") == question_id and dl:
        if (_now() - dl).total_seconds() > LATE_GRACE_S:
            return True
    return False


def overall_over(attempt: dict) -> bool:
    left = overall_left_ms(attempt)
    return left is not None and left < -LATE_GRACE_S * 1000


# ───────────────────────── передача начатой в боте попытки на сайт ─────────────────────────

def bot_question_remaining_ms(attempt: dict, test: dict) -> Optional[tuple]:
    """(question_id, остаток мс) для вопроса, который сейчас висит в боте."""
    try:
        from services import test_runner as tr
    except Exception:
        return None
    seconds = max(5, int(test.get("time_per_question") or 0)) if test.get("time_per_question") else 0
    for info in list(tr._poll_map.values()):
        if info.get("attempt_id") == attempt["id"]:
            if not seconds:
                return info.get("question_id"), None
            left = seconds * 1000 - int((time.time() - info.get("sent_at", time.time())) * 1000)
            return info.get("question_id"), max(0, left)
    return None


async def handoff_to_web(bot, attempt: dict, test: dict) -> dict:
    """Остановить попытку в боте и отдать её сайту. Ответы и порядок вопросов
    остаются те же, таймер текущего вопроса продолжается с остатка, новая попытка
    не создаётся."""
    from services import test_runner as tr
    remaining = bot_question_remaining_ms(attempt, test)
    tr.cancel_timer(attempt["id"])
    await tr._stop_active_poll(bot, attempt["id"])
    fields, args = ["via='link'", "handoff_at=?", "status='in_progress'", "pause_time=NULL"], [_iso(_now())]
    if remaining and remaining[0]:
        qid, left = remaining
        if left is not None:
            fields += ["q_deadline_qid=?", "q_deadline_at=?"]
            args += [qid, _iso(_now() + timedelta(milliseconds=left))]
    limit_min = int(test.get("time_limit_min") or 0)
    if limit_min and not attempt.get("deadline_at"):
        start = _parse(attempt.get("start_time")) or _now()
        fields.append("deadline_at=?")
        args.append(_iso(start + timedelta(minutes=limit_min)))
    if not attempt.get("max_score"):
        qs = usable_questions(test["id"])
        fields.append("max_score=?")
        args.append(sum(ta.points_of(q) for q in qs))
    args.append(attempt["id"])
    db.execute(f"UPDATE test_attempts SET {', '.join(fields)} WHERE id=?", tuple(args))
    return dict(db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt["id"],)))


# ───────────────────────── разбор попытки после завершения ─────────────────────────

def _given_text(q: dict, ans: dict, kind: str) -> str:
    """Что ответил ученик, человеческим языком."""
    p = ta.load_payload(q)
    if kind == ta.TEXT:
        return (ans.get("text") or "") if ans else ""
    if kind == ta.MATCHING:
        pairs, extra = p.get("pairs") or [], p.get("extra") or []
        rights = [x["r"] for x in pairs] + list(extra)
        m = (ans or {}).get("map") or {}
        parts = []
        for li, pair in enumerate(pairs):
            ri = m.get(str(li))
            parts.append(f"{pair['l']} → {rights[ri] if ri is not None and ri < len(rights) else '—'}")
        return "; ".join(parts)
    if kind == ta.ORDERING:
        items = p.get("items") or []
        return " → ".join(items[i] for i in (ans or {}).get("order") or [] if i < len(items))
    return ""


def _correct_text(q: dict, kind: str) -> str:
    p = ta.load_payload(q)
    if kind == ta.TEXT:
        return " / ".join(p.get("answers") or [])
    if kind == ta.MATCHING:
        return "; ".join(f"{x['l']} → {x['r']}" for x in p.get("pairs") or [])
    if kind == ta.ORDERING:
        return " → ".join(p.get("items") or [])
    return ""


def build_review(attempt: dict, test: dict) -> list:
    """Разбор для страницы результата: вопрос, ответ ученика, правильный ответ,
    пояснение. Правильные ответы показываем, только если админ это разрешил."""
    show_correct = bool(test.get("show_correct"))
    show_expl = bool(test.get("show_explanation"))
    try:
        qids = json.loads(attempt.get("question_order") or "[]")
    except (ValueError, TypeError):
        qids = []
    answers = {r["question_id"]: dict(r) for r in db.fetchall(
        "SELECT * FROM attempt_answers WHERE attempt_id=?", (attempt["id"],))}
    out = []
    for n, qid in enumerate(qids, 1):
        q = db.fetchone("SELECT * FROM questions WHERE id=?", (qid,))
        if not q:
            continue
        q = dict(q)
        a = answers.get(qid) or {}
        kind = ta.question_kind(q)
        if kind in ta.TYPED_TYPES:
            try:
                given_raw = json.loads(a["answer_json"]) if a.get("answer_json") else {}
            except (ValueError, TypeError):
                given_raw = {}
            given = _given_text(q, given_raw, kind)
            right = _correct_text(q, kind)
        else:
            opts = {o["id"]: o["text"] for o in db.fetchall(
                "SELECT id, text FROM question_options WHERE question_id=?", (qid,))}
            from services import answer_check as _ac
            picked = _ac.load(a.get("selected_option_ids")) or ([a["selected_option_id"]] if a.get("selected_option_id") else [])
            given = "; ".join(opts.get(i, "") for i in picked)
            right = "; ".join(opts.get(i, "") for i in _ac.correct_option_ids(qid))
        out.append({
            "n": n, "text": q.get("text") or "", "given": given or "—",
            "skipped": bool(a.get("skipped")), "correct": bool(a.get("is_correct")) and not a.get("skipped"),
            "right": right if show_correct else None,
            "explanation": (q.get("explanation") or "") if (show_correct and show_expl) else "",
            "points": ta.points_of(q),
        })
    return out


# ───────────────────────── каталог «Тесты на сайте» ─────────────────────────

def web_catalog(user: Optional[dict], tg_id: Optional[int]) -> list:
    """Опубликованные тесты, для которых админ включил прохождение на сайте и к
    которым у ученика есть доступ. Тесты уроков сюда не входят: они в уроках."""
    if not user or not tg_id:
        return []
    rows = db.fetchall(
        "SELECT t.*, (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS q_count FROM tests t "
        "WHERE t.status='active' AND t.allow_web=1 "
        "AND NOT EXISTS (SELECT 1 FROM lessons l WHERE l.test_id=t.id) ORDER BY t.id DESC LIMIT 100")
    out = []
    for r in rows:
        t = dict(r)
        if access_state(user, tg_id, t) != "ok":
            continue
        active = find_active_attempt(user["id"], t["id"])
        t["in_progress"] = bool(active)
        t["attempts_left"] = attempts_left(user, t)
        out.append(t)
    return out
