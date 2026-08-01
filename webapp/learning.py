"""
Раздел "Начать обучение": предметы -> разделы -> уроки -> тест.

Студенческая и админская части в одном файле — держим раздел
компактным и самодостаточным, не размазывая по многим файлам.

Импорт теста (текст/ZIP) двухшаговый: сначала парсим и показываем
администратору предпросмотр (черновик в lesson_test_drafts), реальный
тест создаётся в базе только после нажатия "Подтвердить".
"""
import asyncio
import glob
import json
import shutil
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp_jinja2
from aiohttp import web

import config
import database as db
import utils
from webapp import auth, lesson_import, video, watermark

logger = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))


# === Хелперы доступа ===

async def _require_login(request: web.Request):
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        raise web.HTTPFound("/?error=login_required")
    return tg_id


def _setting_sync(key: str, default: str = "") -> str:
    row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] else default


def _paywall_context_sync() -> dict:
    return {
        "site_contact_username": _setting_sync("site_contact_username", "").lstrip("@"),
        "premium_benefits_text": _setting_sync(
            "premium_benefits_text",
            "Доступ ко всем тестам, карточкам, режиму заучивания и всем платным конспектам.",
        ),
    }


def get_ent_countdown_context_sync() -> dict:
    """Дата ЕНТ для живого отсчёта (админ задаёт в настройках, время Астаны)."""
    return {"ent_exam_date": _setting_sync("ent_exam_date", "")}


def _has_lesson_paid_access_sync(lesson: dict, tg_id: Optional[int]) -> bool:
    if not lesson.get("is_paid"):
        return True
    if tg_id is None:
        return False
    user = utils.get_user_by_tg(tg_id)
    if user and utils.is_premium(user["id"]):
        return True
    row = db.fetchone(
        "SELECT id FROM lesson_access WHERE lesson_id=? AND user_tg_id=?",
        (lesson["id"], tg_id),
    )
    return row is not None


def _watermark_svg_sync(tg_id: int) -> str:
    user = utils.get_user_by_tg(tg_id)
    username = user.get("username") if user else None
    return watermark.build_watermark_data_uri(tg_id, username)


async def _require_admin(request: web.Request):
    tg_id = await _require_login(request)
    is_adm = await asyncio.to_thread(utils.is_site_admin, tg_id)
    if not is_adm:
        raise web.HTTPForbidden(text="Доступ только для администраторов")
    return tg_id


def _has_subject_access_sync(subject_id: int, tg_id: int) -> bool:
    subj = db.fetchone("SELECT is_open FROM subjects WHERE id=?", (subject_id,))
    if not subj:
        return False
    if subj["is_open"]:
        return True
    row = db.fetchone(
        "SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
        (subject_id, tg_id),
    )
    if not row:
        return False
    if not row["expires_at"]:
        return True
    try:
        return datetime.fromisoformat(row["expires_at"]) > datetime.utcnow()
    except ValueError:
        return False


# === Студент: список предметов ===

def _list_subjects_public_sync(tg_id: Optional[int]) -> list:
    subjects = db.fetchall(
        "SELECT * FROM subjects WHERE status='active' ORDER BY sort_order, id"
    )
    is_adm = bool(tg_id) and utils.is_site_admin(tg_id)
    user = utils.get_user_by_tg(tg_id) if tg_id else None
    user_id = user["id"] if user else None
    result = []
    for s in subjects:
        s = dict(s)
        s["has_access"] = bool(tg_id) and _has_subject_access_sync(s["id"], tg_id)
        if s.get("is_private") and not s["has_access"] and not is_adm:
            continue  # приватный предмет — не показываем в каталоге тем, у кого нет доступа
        # Прогресс: сколько уроков предмета пройдено (открыт+просмотрен / тест сдан)
        lessons = _flatten_subject_lessons_sync(s["id"])
        total = len(lessons)
        done = 0
        if user_id and total:
            pass_pct = s.get("pass_percent") or 0
            for l in lessons:
                if _lesson_completed_sync(user_id, tg_id, l, pass_pct):
                    done += 1
        s["lessons_total"] = total
        s["lessons_done"] = done
        s["progress_percent"] = round(done / total * 100) if total else 0
        result.append(s)
    return result


async def learn_index(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    subjects = await asyncio.to_thread(_list_subjects_public_sync, tg_id)
    context = await auth.nav_context(request)
    context["subjects"] = subjects
    context.update(await asyncio.to_thread(get_ent_countdown_context_sync))
    return aiohttp_jinja2.render_template("learn_subjects.html", request, context)


# === Студент: предмет (разделы + уроки) ===

# === Стоп-уроки: прогрессия обучения ===

def _flatten_subject_lessons_sync(subject_id: int) -> list:
    """Все уроки предмета по порядку (секции по sort_order, внутри — уроки).
    Именно этот порядок определяет последовательность прохождения."""
    rows = db.fetchall(
        "SELECT l.* FROM lessons l JOIN sections s ON s.id = l.section_id "
        "WHERE s.subject_id=? ORDER BY s.sort_order, s.id, l.sort_order, l.id",
        (subject_id,),
    )
    return [dict(r) for r in rows]


def _lesson_best_percent_sync(user_id: int, test_id: int):
    """Лучший % за завершённую попытку теста урока (None если не проходил)."""
    row = db.fetchone(
        "SELECT correct_answers, wrong_answers FROM test_attempts "
        "WHERE user_id=? AND test_id=? AND status='finished' "
        "ORDER BY (CASE WHEN (correct_answers+wrong_answers)>0 "
        "THEN correct_answers*1.0/(correct_answers+wrong_answers) ELSE 0 END) DESC LIMIT 1",
        (user_id, test_id),
    )
    if not row:
        return None
    total = row["correct_answers"] + row["wrong_answers"]
    return round(row["correct_answers"] / total * 100, 1) if total else 0.0


def _lesson_completed_sync(user_id: int, tg_id: int, lesson: dict, pass_percent: int) -> bool:
    """Урок считается пройденным (для разблокировки следующего):
    - зачёт → сдан на свой проходной балл;
    - есть тест → сдан с процентом >= порога (0 = любой завершённый результат);
    - нет теста → урок просто просмотрен."""
    if lesson.get("is_zachet"):
        row = db.fetchone(
            "SELECT 1 FROM zachet_attempts WHERE lesson_id=? AND user_tg_id=? AND passed=1 LIMIT 1",
            (lesson["id"], tg_id))
        return bool(row)
    if lesson.get("test_id"):
        pct = _lesson_best_percent_sync(user_id, lesson["test_id"])
        if pct is None:
            return False
        return pct >= (pass_percent or 0)
    viewed = db.fetchone(
        "SELECT 1 FROM lesson_progress WHERE user_tg_id=? AND lesson_id=?",
        (tg_id, lesson["id"]))
    return bool(viewed)


def _prev_lesson_gate_sync(subject: dict, lesson_id: int, user_id: int, tg_id: int) -> Optional[dict]:
    """Если у предмета включена последовательность и предыдущий урок не пройден —
    возвращает инфо о блокировке, иначе None."""
    if not subject.get("require_sequential"):
        return None
    ordered = _flatten_subject_lessons_sync(subject["id"])
    idx = next((i for i, l in enumerate(ordered) if l["id"] == lesson_id), None)
    if idx is None or idx == 0:
        return None  # первый урок всегда доступен
    prev = ordered[idx - 1]
    if _lesson_completed_sync(user_id, tg_id, prev, subject.get("pass_percent") or 0):
        return None
    return {
        "prev_title": prev["title"],
        "pass_percent": subject.get("pass_percent") or 0,
        "prev_has_test": bool(prev.get("test_id")),
    }


def _subject_detail_sync(subject_id: int, tg_id: Optional[int]) -> Optional[dict]:
    subject = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'", (subject_id,))
    if not subject:
        return None
    subject = dict(subject)
    has_access = bool(tg_id) and _has_subject_access_sync(subject_id, tg_id)
    is_adm = bool(tg_id) and utils.is_site_admin(tg_id)
    if subject["is_private"] and not has_access and not is_adm:
        return None  # приватный предмет полностью скрыт от тех, у кого нет доступа
    sections = db.fetchall(
        "SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (subject_id,)
    )
    viewed = set()
    user_id = None
    if tg_id:
        viewed = {r["lesson_id"] for r in db.fetchall(
            "SELECT lesson_id FROM lesson_progress WHERE user_tg_id=?", (tg_id,)
        )}
        u = utils.get_user_by_tg(tg_id)
        user_id = u["id"] if u else None

    # Прогрессия: считаем какой урок заблокирован (предыдущий не пройден)
    sequential = bool(subject.get("require_sequential")) and not is_adm
    ordered = _flatten_subject_lessons_sync(subject_id) if sequential else []
    locked_ids = set()
    if sequential and user_id:
        pass_pct = subject.get("pass_percent") or 0
        prev_done = True
        for l in ordered:
            if not prev_done:
                locked_ids.add(l["id"])
            prev_done = _lesson_completed_sync(user_id, tg_id, l, pass_pct)
    elif sequential and not user_id:
        for i, l in enumerate(ordered):
            if i > 0:
                locked_ids.add(l["id"])

    result_sections = []
    for sec in sections:
        lessons = db.fetchall(
            "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id", (sec["id"],)
        )
        lessons_out = []
        for lesson in lessons:
            lesson = dict(lesson)
            lesson["viewed"] = lesson["id"] in viewed
            lesson["locked"] = lesson["id"] in locked_ids
            lessons_out.append(lesson)
        result_sections.append({"section": dict(sec), "lessons": lessons_out})
    return {"subject": subject, "sections": result_sections, "has_access": has_access}


# === Обязательная подписка на каналы (глобальные + по предмету) ===

# Кэш проверок подписки: (tg_id, channel) -> (подписан, ts). Чтобы не дёргать
# Telegram API на каждый просмотр страницы.
_sub_cache: dict = {}
SUB_CACHE_TTL = 300  # 5 минут


def _subject_required_channels_sync(subject_id: Optional[int]) -> list:
    """Каналы, обязательные для предмета: глобальные (is_global=1) + привязанные
    к этому предмету (subject_id). Только активные."""
    rows = db.fetchall(
        "SELECT * FROM required_channels "
        "WHERE COALESCE(is_active,1)=1 AND (is_global=1 OR subject_id=?) "
        "ORDER BY is_global DESC, id",
        (subject_id,),
    )
    seen = set()
    out = []
    for r in rows:
        uname = (r["channel_username"] or "").lstrip("@").strip()
        if not uname or uname.lower() in seen:
            continue
        seen.add(uname.lower())
        r = dict(r)
        r["channel_username"] = uname
        out.append(r)
    return out


async def _is_subscribed(tg_id: int, channel_username: str, force: bool = False) -> bool:
    """Проверка подписки через Bot API getChatMember. Бот должен быть админом канала.
    При сбое проверки (бот не в канале и т.п.) доступ НЕ блокируем — только логируем."""
    import time as _time
    import aiohttp as _aiohttp
    key = (tg_id, channel_username.lower())
    now = _time.time()
    if not force:
        cached = _sub_cache.get(key)
        if cached and now - cached[1] < SUB_CACHE_TTL:
            return cached[0]
    ok = True
    try:
        url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getChatMember"
        params = {"chat_id": f"@{channel_username}", "user_id": tg_id}
        async with _aiohttp.ClientSession() as sess:
            async with sess.get(url, params=params,
                                 timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        if data.get("ok"):
            status = ((data.get("result") or {}).get("status")) or ""
            ok = status in ("member", "administrator", "creator")
        else:
            # Бот не может проверить канал (не добавлен админом туда) — пропускаем,
            # иначе заблокировали бы всех. Админу нужно добавить бота в канал.
            logger.warning("getChatMember @%s не удался: %s", channel_username,
                            data.get("description"))
            ok = True
    except Exception as e:
        logger.warning("Проверка подписки @%s: %s", channel_username, e)
        ok = True
    _sub_cache[key] = (ok, now)
    return ok


async def _check_subject_subscription(request: web.Request, subject_id: int,
                                       tg_id: Optional[int]):
    """Если у предмета есть обязательные каналы и юзер не подписан — возвращает
    Response со страницей подписки. Иначе None (можно продолжать)."""
    if tg_id is None:
        return None
    if await asyncio.to_thread(utils.is_site_admin, tg_id):
        return None
    channels = await asyncio.to_thread(_subject_required_channels_sync, subject_id)
    if not channels:
        return None
    force = request.query.get("recheck") == "1"
    missing = []
    for ch in channels:
        if not await _is_subscribed(tg_id, ch["channel_username"], force=force):
            missing.append(ch)
    if not missing:
        return None
    context = await auth.nav_context(request)
    context["channels"] = missing
    context["recheck_url"] = f"{request.path}?recheck=1"
    return aiohttp_jinja2.render_template("subject_subscribe.html", request, context)


async def learn_subject(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    subject_id = int(request.match_info["subject_id"])
    data = await asyncio.to_thread(_subject_detail_sync, subject_id, tg_id)
    if data is None:
        raise web.HTTPNotFound(text="Предмет не найден")
    sub_page = await _check_subject_subscription(request, subject_id, tg_id)
    if sub_page is not None:
        return sub_page
    data.update(await auth.nav_context(request))
    return aiohttp_jinja2.render_template("learn_subject.html", request, data)


# === Студент: урок ===

def _lesson_detail_sync(lesson_id: int, tg_id: Optional[int]) -> Optional[dict]:
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson or lesson["status"] != "open":
        return None
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section:
        return None
    subject = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'", (section["subject_id"],))
    if not subject:
        return None
    is_adm = bool(tg_id) and utils.is_site_admin(tg_id)
    if subject["is_private"] and not is_adm:
        if tg_id is None or not _has_subject_access_sync(section["subject_id"], tg_id):
            return None  # приватный урок полностью скрыт от тех, у кого нет доступа
    lesson = dict(lesson)

    base = {"lesson": lesson, "section": dict(section), "subject": dict(subject)}
    base.update(_paywall_context_sync())

    if tg_id is None:
        base["access_state"] = "need_login"
        return base

    if not _has_subject_access_sync(section["subject_id"], tg_id):
        base["access_state"] = "need_subject_access"
        return base

    if not _has_lesson_paid_access_sync(lesson, tg_id):
        qcount = 0
        if lesson["test_id"]:
            qcount = db.fetchone(
                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (lesson["test_id"],)
            )["c"]
        base["access_state"] = "need_payment"
        base["paid_questions_count"] = qcount
        return base

    base["access_state"] = "full"

    db.execute(
        "INSERT OR IGNORE INTO lesson_progress (user_tg_id, lesson_id) VALUES (?, ?)",
        (tg_id, lesson_id),
    )
    # Стоп-урок: засекаем момент первого открытия для отсчёта времени чтения
    if not is_adm:
        db.execute(
            "UPDATE lesson_progress SET read_started_at=? "
            "WHERE user_tg_id=? AND lesson_id=? AND read_started_at IS NULL",
            (datetime.utcnow().isoformat(timespec="seconds"), tg_id, lesson_id),
        )

    user = utils.get_user_by_tg(tg_id)
    user_id = user["id"] if user else None

    # 1) Последовательность: предыдущий урок должен быть пройден
    base["prev_gate"] = None
    if not is_adm and user_id:
        gate = _prev_lesson_gate_sync(dict(subject), lesson_id, user_id, tg_id)
        base["prev_gate"] = gate

    # 2) Время чтения до открытия теста
    min_read = (subject["min_read_min"] or 0) if not is_adm else 0
    base["min_read_min"] = min_read
    read_left = 0
    if min_read and lesson["test_id"]:
        row = db.fetchone(
            "SELECT read_started_at FROM lesson_progress WHERE user_tg_id=? AND lesson_id=?",
            (tg_id, lesson_id))
        if row and row["read_started_at"]:
            try:
                started = datetime.fromisoformat(row["read_started_at"])
                elapsed = (datetime.utcnow() - started).total_seconds()
                read_left = max(0, int(min_read * 60 - elapsed))
            except ValueError:
                read_left = min_read * 60
        else:
            read_left = min_read * 60
    base["read_seconds_left"] = read_left
    base["pass_percent"] = (subject["pass_percent"] or 0) if not is_adm else 0

    test_info = None
    if lesson["test_id"]:
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (lesson["test_id"],))
        qcount = db.fetchone(
            "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (lesson["test_id"],)
        )["c"]
        if test:
            user = utils.get_user_by_tg(tg_id)
            attempts_used = db.fetchone(
                "SELECT COUNT(*) AS c FROM test_attempts "
                "WHERE user_id=? AND test_id=? AND status='finished'",
                (user["id"], lesson["test_id"]),
            )["c"]
            limit = test["attempts_limit"] or 0
            test_info = {
                "test_id": lesson["test_id"],
                "questions_count": qcount,
                "attempts_used": attempts_used,
                "attempts_limit": limit,
                "attempts_left": (limit - attempts_used) if limit else None,
                "blocked": bool(limit) and attempts_used >= limit,
            }
    base["test_info"] = test_info
    base["lesson_images"] = _lesson_images_sync(lesson_id)
    return base


async def learn_lesson(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    lesson_id = int(request.match_info["lesson_id"])
    # Урок-зачёт открывается на своей странице
    is_z = await asyncio.to_thread(
        db.fetchone, "SELECT is_zachet FROM lessons WHERE id=?", (lesson_id,))
    if is_z and is_z["is_zachet"]:
        raise web.HTTPFound(f"/learn/lesson/{lesson_id}/zachet")
    data = await asyncio.to_thread(_lesson_detail_sync, lesson_id, tg_id)
    if data is None:
        raise web.HTTPNotFound(text="Урок не найден")
    sub_page = await _check_subject_subscription(
        request, data["subject"]["id"], tg_id)
    if sub_page is not None:
        return sub_page
    data.update(await auth.nav_context(request))
    data["error"] = request.query.get("error")
    if data["access_state"] == "full":
        data["watermark_svg"] = await asyncio.to_thread(_watermark_svg_sync, tg_id)
    return aiohttp_jinja2.render_template("learn_lesson.html", request, data)


# === Студент: прохождение теста ===

def _build_questions_out(q_ids: list, options_order: dict) -> list:
    """Собирает вопросы с вариантами в зафиксированном для попытки порядке.
    Битые вопросы (удалён, нет текста, нет вариантов) пропускаются."""
    out = []
    for qid in q_ids:
        q = db.fetchone("SELECT id, text, web_image_path FROM questions WHERE id=?", (qid,))
        if not q or not (q["text"] or "").strip():
            logger.warning("Пропущен битый вопрос id=%s (нет текста/удалён)", qid)
            continue
        opts = db.fetchall(
            "SELECT id, text FROM question_options WHERE question_id=? ORDER BY order_num, id",
            (qid,),
        )
        opts = [dict(o) for o in opts]
        order = options_order.get(str(qid))
        if order:
            by_id = {o["id"]: o for o in opts}
            opts = [by_id[i] for i in order if i in by_id]
        if len(opts) < 2:
            logger.warning("Пропущен вопрос id=%s: вариантов меньше двух", qid)
            continue
        out.append({
            "id": q["id"], "text": q["text"], "web_image_path": q["web_image_path"],
            "options": opts,
        })
    return out


def _start_attempt_sync(lesson_id: int, tg_id: int, restart: bool = False) -> Optional[dict]:
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson or not lesson["test_id"]:
        return None
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section or not _has_subject_access_sync(section["subject_id"], tg_id):
        return None

    user = utils.get_user_by_tg(tg_id)
    user_id = user["id"]
    test_id = lesson["test_id"]
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        return None

    # === Стоп-урок: серверная защита (нельзя обойти с фронта) ===
    subject = db.fetchone("SELECT * FROM subjects WHERE id=?", (section["subject_id"],))
    is_adm = utils.is_site_admin(tg_id)
    if subject and not is_adm:
        # a) последовательность: предыдущий урок должен быть пройден
        gate = _prev_lesson_gate_sync(dict(subject), lesson_id, user_id, tg_id)
        if gate:
            return {"gate_prev": gate}
        # b) минимальное время чтения
        min_read = subject["min_read_min"] or 0
        if min_read:
            row = db.fetchone(
                "SELECT read_started_at FROM lesson_progress WHERE user_tg_id=? AND lesson_id=?",
                (tg_id, lesson_id))
            started_at = row["read_started_at"] if row else None
            elapsed = -1
            if started_at:
                try:
                    elapsed = (datetime.utcnow() - datetime.fromisoformat(started_at)).total_seconds()
                except ValueError:
                    elapsed = min_read * 60
            if elapsed < min_read * 60:
                left = max(0, int(min_read * 60 - max(0, elapsed)))
                return {"gate_read": {"min_read_min": min_read, "seconds_left": left}}

    # === Автосохранение: незавершённая попытка ===
    existing = db.fetchone(
        "SELECT * FROM test_attempts WHERE user_id=? AND test_id=? AND status='in_progress' "
        "ORDER BY id DESC LIMIT 1",
        (user_id, test_id),
    )
    if existing and restart:
        db.execute(
            "UPDATE test_attempts SET status='aborted', end_time=? WHERE id=?",
            (datetime.utcnow().isoformat(timespec="seconds"), existing["id"]),
        )
        existing = None

    if existing:
        # Продолжаем с того же места: тот же порядок вопросов и вариантов
        try:
            q_ids = json.loads(existing["question_order"] or "[]")
        except (ValueError, TypeError):
            q_ids = []
        try:
            options_order = json.loads(existing["options_order"] or "{}")
        except (ValueError, TypeError):
            options_order = {}
        questions_out = _build_questions_out(q_ids, options_order)
        answered_rows = db.fetchall(
            "SELECT question_id, selected_option_id, is_correct, skipped "
            "FROM attempt_answers WHERE attempt_id=?",
            (existing["id"],),
        )
        show_correct = bool(test["show_correct"])
        answered = {}
        for r in answered_rows:
            entry = {
                "selected_option_id": r["selected_option_id"],
                "correct": bool(r["is_correct"]) if show_correct else None,
                "skipped": bool(r["skipped"]),
                "correct_option_id": None,
            }
            if show_correct:
                co = db.fetchone(
                    "SELECT id FROM question_options WHERE question_id=? AND is_correct=1",
                    (r["question_id"],))
                entry["correct_option_id"] = co["id"] if co else None
            answered[str(r["question_id"])] = entry
        return {
            "attempt_id": existing["id"],
            "lesson": dict(lesson),
            "questions": questions_out,
            "time_per_question": test["time_per_question"] or 0,
            "show_correct": show_correct,
            "resume": True,
            "answered": answered,
        }

    limit = test["attempts_limit"] or 0
    if limit:
        used = db.fetchone(
            "SELECT COUNT(*) AS c FROM test_attempts "
            "WHERE user_id=? AND test_id=? AND status='finished'",
            (user_id, test_id),
        )["c"]
        if used >= limit:
            return {"blocked": True}

    questions = db.fetchall(
        "SELECT id, text, web_image_path FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,),
    )
    questions = [dict(q) for q in questions]
    if test["shuffle_questions"]:
        random.shuffle(questions)
    q_ids = [q["id"] for q in questions]

    # Порядок вариантов фиксируем в попытке — при продолжении не перемешается заново
    options_order = {}
    if test["shuffle_options"]:
        for qid in q_ids:
            opts = db.fetchall(
                "SELECT id FROM question_options WHERE question_id=? ORDER BY order_num, id",
                (qid,))
            ids = [o["id"] for o in opts]
            random.shuffle(ids)
            options_order[str(qid)] = ids

    db.execute(
        "INSERT INTO test_attempts (user_id, test_id, question_order, options_order, "
        "status, is_counted) VALUES (?, ?, ?, ?, 'in_progress', 1)",
        (user_id, test_id, json.dumps(q_ids), json.dumps(options_order)),
    )
    attempt_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]

    questions_out = _build_questions_out(q_ids, options_order)

    return {
        "attempt_id": attempt_id,
        "lesson": dict(lesson),
        "questions": questions_out,
        "time_per_question": test["time_per_question"] or 0,
        "show_correct": bool(test["show_correct"]),
        "resume": False,
        "answered": {},
    }


async def learn_test_start(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    lesson_id = int(request.match_info["lesson_id"])
    restart = request.query.get("restart") == "1"
    data = await asyncio.to_thread(_start_attempt_sync, lesson_id, tg_id, restart)
    if data is None:
        raise web.HTTPNotFound(text="Тест не найден или нет доступа")
    if data.get("blocked"):
        raise web.HTTPFound(f"/learn/lesson/{lesson_id}?error=attempts_exceeded")
    # Стоп-урок: не пущён из-за времени чтения или незавершённого предыдущего урока
    if data.get("gate_read"):
        raise web.HTTPFound(f"/learn/lesson/{lesson_id}?error=need_read")
    if data.get("gate_prev"):
        raise web.HTTPFound(f"/learn/lesson/{lesson_id}?error=need_prev")
    data.update(await auth.nav_context(request))
    data["questions_json"] = json.dumps(data["questions"], ensure_ascii=False)
    data["answered_json"] = json.dumps(data.get("answered") or {}, ensure_ascii=False)
    data["is_resume"] = bool(data.get("resume"))
    data["watermark_svg"] = await asyncio.to_thread(_watermark_svg_sync, tg_id)
    return aiohttp_jinja2.render_template("learn_test.html", request, data)


def _answer_sync(attempt_id: int, tg_id: int, question_id: int, option_id: Optional[int]) -> dict:
    attempt = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not attempt:
        return {"error": "attempt_not_found"}
    user = utils.get_user_by_tg(tg_id)
    if not user or attempt["user_id"] != user["id"]:
        return {"error": "forbidden"}

    test = db.fetchone("SELECT show_correct FROM tests WHERE id=?", (attempt["test_id"],))
    show_correct = bool(test["show_correct"]) if test else True

    def _correct_opt_id():
        co = db.fetchone(
            "SELECT id FROM question_options WHERE question_id=? AND is_correct=1",
            (question_id,))
        return co["id"] if co else None

    # === Идемпотентность: повторная отправка того же ответа (ретрай сети,
    # двойной клик) НЕ должна второй раз менять счётчики — возвращаем прежний
    # результат как ни в чём не бывало.
    existing = db.fetchone(
        "SELECT selected_option_id, is_correct, skipped FROM attempt_answers "
        "WHERE attempt_id=? AND question_id=?",
        (attempt_id, question_id),
    )
    if existing:
        return {
            "correct": (bool(existing["is_correct"]) if not existing["skipped"] else False)
                       if show_correct else None,
            "correct_option_id": _correct_opt_id() if show_correct else None,
            "skipped": bool(existing["skipped"]),
            "duplicate": True,
        }

    if option_id is None:
        # Вышло время (таймер) — засчитываем как пропуск
        try:
            db.execute(
                "INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, is_correct, skipped) "
                "VALUES (?, ?, NULL, 0, 1)",
                (attempt_id, question_id),
            )
        except Exception:
            return {"correct": False if show_correct else None,
                    "correct_option_id": _correct_opt_id() if show_correct else None,
                    "skipped": True, "duplicate": True}
        db.execute("UPDATE test_attempts SET skipped_answers = skipped_answers + 1 WHERE id=?",
                    (attempt_id,))
        return {
            "correct": False if show_correct else None,
            "correct_option_id": _correct_opt_id() if show_correct else None,
            "skipped": True,
        }

    opt = db.fetchone(
        "SELECT id, is_correct FROM question_options WHERE id=? AND question_id=?",
        (option_id, question_id),
    )
    if not opt:
        return {"error": "bad_option"}
    is_correct = bool(opt["is_correct"])

    try:
        db.execute(
            "INSERT INTO attempt_answers (attempt_id, question_id, selected_option_id, is_correct) "
            "VALUES (?, ?, ?, ?)",
            (attempt_id, question_id, option_id, 1 if is_correct else 0),
        )
    except Exception:
        # Гонка: параллельный запрос успел первым — счётчики уже учтены им
        return {"correct": is_correct if show_correct else None,
                "correct_option_id": _correct_opt_id() if show_correct else None,
                "duplicate": True}

    if is_correct:
        db.execute("UPDATE test_attempts SET correct_answers = correct_answers + 1 WHERE id=?",
                    (attempt_id,))
    else:
        db.execute("UPDATE test_attempts SET wrong_answers = wrong_answers + 1 WHERE id=?",
                    (attempt_id,))

    return {
        "correct": is_correct if show_correct else None,
        "correct_option_id": _correct_opt_id() if show_correct else None,
    }


async def learn_test_answer(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    body = await request.json()
    question_id = int(body["question_id"])
    option_id = body.get("option_id")
    option_id = int(option_id) if option_id is not None else None
    result = await asyncio.to_thread(_answer_sync, attempt_id, tg_id, question_id, option_id)
    return web.json_response(result)


def _finish_attempt_sync(attempt_id: int, tg_id: int) -> dict:
    attempt = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    user = utils.get_user_by_tg(tg_id)
    if not attempt or not user or attempt["user_id"] != user["id"]:
        return {"error": "forbidden"}

    test = db.fetchone("SELECT show_results FROM tests WHERE id=?", (attempt["test_id"],))
    show_results = bool(test["show_results"]) if test else True

    correct = attempt["correct_answers"]
    wrong = attempt["wrong_answers"]
    total = correct + wrong
    percent = round(correct / total * 100, 1) if total else 0

    # Идемпотентно: повторный finish (ретрай сети) ничего не ломает
    if attempt["status"] != "finished":
        db.execute(
            "UPDATE test_attempts SET status='finished', score=?, end_time=? WHERE id=?",
            (correct, datetime.utcnow().isoformat(timespec="seconds"), attempt_id),
        )
    if not show_results:
        return {"show_results": False}
    return {"show_results": True, "correct": correct, "wrong": wrong, "total": total, "percent": percent}


async def learn_test_finish(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    result = await asyncio.to_thread(_finish_attempt_sync, attempt_id, tg_id)
    return web.json_response(result)


def _attempt_result_sync(attempt_id: int, tg_id: int) -> Optional[dict]:
    attempt = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    user = utils.get_user_by_tg(tg_id)
    if not attempt or not user or attempt["user_id"] != user["id"]:
        return None
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (attempt["test_id"],))
    lesson = db.fetchone("SELECT * FROM lessons WHERE test_id=?", (attempt["test_id"],))
    total = attempt["correct_answers"] + attempt["wrong_answers"]
    percent = round(attempt["correct_answers"] / total * 100, 1) if total else 0
    return {
        "attempt": dict(attempt), "test": dict(test) if test else None,
        "lesson": dict(lesson) if lesson else None, "percent": percent, "total": total,
        "show_results": bool(test["show_results"]) if test else True,
    }


async def learn_test_result(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    attempt_id = int(request.match_info["attempt_id"])
    data = await asyncio.to_thread(_attempt_result_sync, attempt_id, tg_id)
    if data is None:
        raise web.HTTPNotFound()
    data.update(await auth.nav_context(request))
    return aiohttp_jinja2.render_template("learn_test_result.html", request, data)


# === Раздача загруженных картинок вопросов и видео (не через static/, хранится рядом с БД) ===

async def uploaded_question_image(request: web.Request) -> web.Response:
    await _require_login(request)
    filename = request.match_info["filename"]
    if "/" in filename or ".." in filename:
        raise web.HTTPBadRequest()
    path = lesson_import.upload_dir() / filename
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def uploaded_video(request: web.Request) -> web.Response:
    await _require_login(request)
    filename = request.match_info["filename"]
    if "/" in filename or ".." in filename:
        raise web.HTTPBadRequest()
    path = video.upload_dir() / filename
    if not path.exists():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Content-Disposition"] = "inline"
    return resp


async def uploaded_lesson_image(request: web.Request) -> web.Response:
    await _require_login(request)
    filename = request.match_info["filename"]
    if "/" in filename or ".." in filename:
        raise web.HTTPBadRequest()
    path = _lessons_image_dir() / filename
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def admin_delete_lesson_image(request: web.Request) -> web.Response:
    await _require_admin(request)
    image_id = int(request.match_info["image_id"])

    def _del():
        row = db.fetchone("SELECT lesson_id, image_path FROM lesson_images WHERE id=?", (image_id,))
        if not row:
            return None
        db.execute("DELETE FROM lesson_images WHERE id=?", (image_id,))
        try:
            name = (row["image_path"] or "").rsplit("/", 1)[-1]
            p = _lessons_image_dir() / name
            if p.exists():
                p.unlink()
        except Exception:
            pass
        return row["lesson_id"]

    lesson_id = await asyncio.to_thread(_del)
    if lesson_id:
        raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Фото удалено")
    raise web.HTTPFound("/admin/learn")


# === Админка: обзор ===

def _admin_overview_sync() -> list:
    subjects = db.fetchall("SELECT * FROM subjects ORDER BY sort_order, id")
    result = []
    for s in subjects:
        s = dict(s)
        s["sections"] = []
        secs = db.fetchall("SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id", (s["id"],))
        for sec in secs:
            sec = dict(sec)
            sec["lessons"] = [dict(l) for l in db.fetchall(
                "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id", (sec["id"],))]
            s["sections"].append(sec)
        if not s["is_open"]:
            rows = db.fetchall(
                "SELECT sa.user_tg_id, sa.expires_at, u.username, u.first_name "
                "FROM subject_access sa LEFT JOIN users u ON u.tg_id = sa.user_tg_id "
                "WHERE sa.subject_id=? ORDER BY sa.id DESC",
                (s["id"],),
            )
            s["access_list"] = [dict(r) for r in rows]
            pend = db.fetchall(
                "SELECT username, days, created_at FROM pending_access "
                "WHERE kind='subject' AND test_id=? AND fulfilled=0 ORDER BY id DESC",
                (s["id"],),
            )
            s["pending_list"] = [dict(r) for r in pend]
        else:
            s["access_list"] = []
            s["pending_list"] = []
        result.append(s)
    return result


def _site_admins_ctx_sync(tg_id: int) -> dict:
    rows = db.fetchall("SELECT * FROM site_admins ORDER BY created_at DESC")
    return {
        "site_admins": [dict(r) for r in rows],
        "is_owner": utils.is_owner(tg_id),
    }


async def admin_learn_index(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    subjects = await asyncio.to_thread(_admin_overview_sync)
    context = await auth.nav_context(request)
    context["subjects"] = subjects
    context["message"] = request.query.get("message")
    context["edit_subject"] = request.query.get("edit_subject")
    context["edit_section"] = request.query.get("edit_section")
    context.update(await asyncio.to_thread(_paywall_context_sync))
    context.update(await asyncio.to_thread(get_channels_context_sync))
    context.update(await asyncio.to_thread(_required_channels_admin_sync))
    context.update(await asyncio.to_thread(get_ent_countdown_context_sync))
    context.update(await asyncio.to_thread(_site_admins_ctx_sync, tg_id))
    return aiohttp_jinja2.render_template("admin_learn.html", request, context)


async def _require_owner(request: web.Request):
    tg_id = await _require_admin(request)
    if not await asyncio.to_thread(utils.is_owner, tg_id):
        raise web.HTTPForbidden(text="Только для главного администратора")
    return tg_id


async def admin_add_site_admin(request: web.Request) -> web.Response:
    tg_id = await _require_owner(request)
    data = await request.post()
    ident = (data.get("ident") or "").strip()

    def _add():
        user = utils.find_user_by_arg(ident)
        if not user:
            return "not_found"
        db.execute(
            "INSERT INTO site_admins (tg_id, username, granted_by) VALUES (?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username",
            (user["tg_id"], user.get("username"), tg_id),
        )
        return "ok"

    res = await asyncio.to_thread(_add)
    from urllib.parse import quote
    msg = "Сайт-админ добавлен" if res == "ok" else \
        f"Не найден пользователь {ident} (должен был хоть раз запустить бота)"
    raise web.HTTPFound(f"/admin/learn?message={quote(msg)}")


async def admin_remove_site_admin(request: web.Request) -> web.Response:
    await _require_owner(request)
    target = int(request.match_info["tg_id"])
    await asyncio.to_thread(db.execute, "DELETE FROM site_admins WHERE tg_id=?", (target,))
    raise web.HTTPFound("/admin/learn?message=Сайт-админ удалён")


# === Админка: предметы ===

def _parse_progress_settings(data):
    """Читает настройки стоп-уроков из формы: минуты чтения, последовательность, порог %."""
    def _int(name, lo, hi):
        raw = (data.get(name) or "").strip()
        try:
            return max(lo, min(hi, int(raw)))
        except (ValueError, TypeError):
            return lo
    min_read = _int("min_read_min", 0, 120)
    seq = 1 if data.get("require_sequential") == "on" else 0
    pass_pct = _int("pass_percent", 0, 100)
    return min_read, seq, pass_pct


async def admin_create_subject(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    is_private = 1 if data.get("is_private") == "on" else 0
    is_open = 0 if is_private else (1 if data.get("is_open") == "on" else 0)
    min_read, seq, pass_pct = _parse_progress_settings(data)
    if title:
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO subjects (title, description, is_open, is_private, created_by, "
            "min_read_min, require_sequential, pass_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, description, is_open, is_private, tg_id, min_read, seq, pass_pct),
        )
    raise web.HTTPFound("/admin/learn?message=Предмет создан")


async def admin_edit_subject(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    is_private = 1 if data.get("is_private") == "on" else 0
    is_open = 0 if is_private else (1 if data.get("is_open") == "on" else 0)
    min_read, seq, pass_pct = _parse_progress_settings(data)
    if title:
        await asyncio.to_thread(
            db.execute,
            "UPDATE subjects SET title=?, description=?, is_open=?, is_private=?, "
            "min_read_min=?, require_sequential=?, pass_percent=? WHERE id=?",
            (title, description, is_open, is_private, min_read, seq, pass_pct, subject_id),
        )
        # Тесты уроков приватного предмета автоматически приватны и в боте
        await asyncio.to_thread(_sync_subject_tests_privacy_sync, subject_id)
    raise web.HTTPFound("/admin/learn?message=Предмет обновлён")


async def admin_reorder_lessons(request: web.Request) -> web.Response:
    """Сохранить новый порядок уроков раздела (drag&drop). JSON: {order: [id, ...]}."""
    await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    try:
        body = await request.json()
        order = [int(x) for x in (body.get("order") or [])]
    except Exception:
        return web.json_response({"ok": False}, status=400)

    def _save():
        for pos, lid in enumerate(order):
            db.execute(
                "UPDATE lessons SET sort_order=? WHERE id=? AND section_id=?",
                (pos, lid, section_id))
    await asyncio.to_thread(_save)
    return web.json_response({"ok": True})


def _delete_subject_cascade_sync(subject_id: int) -> None:
    lessons = db.fetchall(
        "SELECT l.id, l.test_id FROM lessons l JOIN sections s ON s.id=l.section_id "
        "WHERE s.subject_id=? AND l.test_id IS NOT NULL", (subject_id,)
    )
    for l in lessons:
        db.execute("DELETE FROM tests WHERE id=?", (l["test_id"],))
    db.execute("DELETE FROM subjects WHERE id=?", (subject_id,))  # каскадом уйдут sections/lessons/access


async def admin_delete_subject(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    await asyncio.to_thread(_delete_subject_cascade_sync, subject_id)
    raise web.HTTPFound("/admin/learn?message=Предмет удалён")


async def admin_toggle_subject(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])

    def _toggle():
        row = db.fetchone("SELECT status FROM subjects WHERE id=?", (subject_id,))
        new_status = "hidden" if row["status"] == "active" else "active"
        db.execute("UPDATE subjects SET status=? WHERE id=?", (new_status, subject_id))

    await asyncio.to_thread(_toggle)
    raise web.HTTPFound("/admin/learn?message=Статус предмета изменён")


def _sync_subject_tests_privacy_sync(subject_id: int) -> None:
    """Приватность предмета → приватность тестов его уроков в боте.
    Тест приватного предмета (или платного урока) не должен светиться в общем
    каталоге бота — помечаем is_private=1. Публичный предмет: приватность
    остаётся только у тестов платных уроков."""
    subject = db.fetchone("SELECT is_private FROM subjects WHERE id=?", (subject_id,))
    if not subject:
        return
    rows = db.fetchall(
        "SELECT l.test_id, l.is_paid FROM lessons l "
        "JOIN sections s ON s.id = l.section_id "
        "WHERE s.subject_id=? AND l.test_id IS NOT NULL",
        (subject_id,),
    )
    for r in rows:
        private = 1 if (subject["is_private"] or r["is_paid"]) else 0
        db.execute("UPDATE tests SET is_private=? WHERE id=?", (private, r["test_id"]))


def _parse_bulk_idents(text: str, limit: int = 100) -> list:
    """@username / tg_id через запятую, пробел или перенос строки. Максимум limit штук."""
    import re
    raw = re.split(r"[,\s\n]+", text or "")
    seen = set()
    out = []
    for r in raw:
        r = r.strip()
        if not r:
            continue
        key = r.lower().lstrip("@")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _grant_subject_access_bulk_sync(subject_id: int, idents: list, days: int, granted_by: int) -> dict:
    expires_at = None
    if days > 0:
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")
    granted, pending = [], []
    from services import pending_access_service as pas
    for ident in idents:
        user = utils.find_user_by_arg(ident)
        if not user:
            # Не запускал бота — кладём в резерв: доступ применится сам при /start
            uname = ident.lstrip("@").strip()
            if uname and not uname.isdigit():
                pas.add_pending(uname, "subject", subject_id, days, granted_by)
                pending.append(ident)
            continue
        db.execute(
            "INSERT INTO subject_access (subject_id, user_tg_id, expires_at, granted_by_admin) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subject_id, user_tg_id) DO UPDATE SET expires_at=excluded.expires_at",
            (subject_id, user["tg_id"], expires_at, granted_by),
        )
        granted.append(ident)
    return {"granted": granted, "pending": pending, "not_found": []}


async def admin_revoke_access(request: web.Request) -> web.Response:
    """Забрать доступ у отмеченных галочками пользователей."""
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    tg_ids = [v for v in data.getall("revoke_tg_id", []) if str(v).isdigit()]
    if not tg_ids:
        raise web.HTTPFound("/admin/learn?message=Никто не выбран")

    def _revoke():
        for tid in tg_ids:
            db.execute(
                "DELETE FROM subject_access WHERE subject_id=? AND user_tg_id=?",
                (subject_id, int(tid)),
            )

    await asyncio.to_thread(_revoke)
    from urllib.parse import quote
    raise web.HTTPFound(f"/admin/learn?message={quote(f'Доступ забран: {len(tg_ids)}')}")


async def admin_grant_access(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    target_raw = (data.get("user_tg_id") or "").strip()
    days_raw = (data.get("days") or "0").strip()

    idents = _parse_bulk_idents(target_raw, limit=100)
    if not idents:
        raise web.HTTPFound("/admin/learn?message=Не указан ни один @username или ID")

    days = int(days_raw) if days_raw.isdigit() else 0
    result = await asyncio.to_thread(
        _grant_subject_access_bulk_sync, subject_id, idents, days, tg_id
    )
    msg = f"Доступ выдан: {len(result['granted'])}"
    if result.get("pending"):
        msg += (f"; в резерве до /start: {len(result['pending'])} "
                f"(получат доступ автоматически при первом запуске бота)")
    from urllib.parse import quote
    raise web.HTTPFound(f"/admin/learn?message={quote(msg)}")


# === Админка: разделы ===

async def admin_create_section(request: web.Request) -> web.Response:
    await _require_admin(request)
    subject_id = int(request.match_info["subject_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    if title:
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO sections (subject_id, title) VALUES (?, ?)",
            (subject_id, title),
        )
    raise web.HTTPFound("/admin/learn?message=Раздел создан")


async def admin_edit_section(request: web.Request) -> web.Response:
    await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    if title:
        await asyncio.to_thread(
            db.execute, "UPDATE sections SET title=? WHERE id=?", (title, section_id)
        )
    raise web.HTTPFound("/admin/learn?message=Раздел обновлён")


def _delete_section_cascade_sync(section_id: int) -> None:
    lessons = db.fetchall(
        "SELECT id, test_id FROM lessons WHERE section_id=? AND test_id IS NOT NULL", (section_id,)
    )
    for l in lessons:
        db.execute("DELETE FROM tests WHERE id=?", (l["test_id"],))
    db.execute("DELETE FROM sections WHERE id=?", (section_id,))  # каскадом уйдут lessons


async def admin_delete_section(request: web.Request) -> web.Response:
    await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    await asyncio.to_thread(_delete_section_cascade_sync, section_id)
    raise web.HTTPFound("/admin/learn?message=Раздел удалён")


# === Админка: уроки (создание -> либо сразу, либо через превью теста) ===

def _video_from_post_sync(data) -> tuple:
    """(video_url, youtube_id) из полей формы video_mode/video_file/video_youtube_url."""
    video_mode = data.get("video_mode") or "none"
    if video_mode == "youtube":
        yt_id = video.extract_youtube_id((data.get("video_youtube_url") or "").strip())
        return None, yt_id
    if video_mode == "upload":
        field = data.get("video_file")
        if field is not None and hasattr(field, "file"):
            raw = field.file.read()
            if raw:
                return video.save_uploaded_video(raw, field.filename or "video.mp4"), None
    return None, None


def _test_text_from_upload_sync(data) -> Optional[str]:
    """Файл .txt или .pdf с тестом вместо вставки текста вручную — тот же формат вопросов."""
    field = data.get("test_file")
    if field is None or not hasattr(field, "file"):
        return None
    file_bytes = field.file.read()
    if not file_bytes:
        return None
    ext = (field.filename or "").rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return lesson_import.extract_text_from_pdf(file_bytes)
    return file_bytes.decode("utf-8", errors="replace")


def _content_from_upload_sync(data) -> Optional[str]:
    """Если в форме приложен .docx/.pdf с конспектом — вытаскивает из него текст."""
    field = data.get("content_file")
    if field is None or not hasattr(field, "file"):
        return None
    file_bytes = field.file.read()
    if not file_bytes:
        return None
    return lesson_import.extract_lesson_content_from_upload(field.filename, file_bytes)


def _lessons_image_dir():
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "lessons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_lesson_images_sync(lesson_id: int, data, max_files: int = 40) -> int:
    """Сохраняет приложенные к уроку фото (несколько) в uploads/lessons и БД.
    Возвращает число сохранённых."""
    import uuid as _uuid
    fields = [f for f in data.getall("lesson_images", []) if hasattr(f, "file")]
    if not fields:
        return 0
    last = db.fetchone(
        "SELECT COALESCE(MAX(sort_order), 0) AS m FROM lesson_images WHERE lesson_id=?",
        (lesson_id,))
    order = (last["m"] if last else 0) or 0
    saved = 0
    for field in fields[:max_files]:
        raw = field.file.read()
        if not raw:
            continue
        ext = Path(field.filename or "").suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        name = f"{_uuid.uuid4().hex}{ext}"
        (_lessons_image_dir() / name).write_bytes(raw)
        order += 1
        db.execute(
            "INSERT INTO lesson_images (lesson_id, image_path, sort_order) VALUES (?, ?, ?)",
            (lesson_id, f"/uploads/lessons/{name}", order))
        saved += 1
    return saved


def _lesson_images_sync(lesson_id: int) -> list:
    rows = db.fetchall(
        "SELECT * FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
        (lesson_id,))
    return [dict(r) for r in rows]


async def admin_create_lesson(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    section_id = int(request.match_info["section_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    content_html = (data.get("content") or "").strip()
    try:
        extracted = await asyncio.to_thread(_content_from_upload_sync, data)
        if extracted:
            content_html = extracted
    except Exception as e:
        raise web.HTTPFound(
            f"/admin/learn?message=Не удалось прочитать файл конспекта: {e}"
        )
    test_mode = data.get("test_mode") or "none"
    test_text = data.get("test_text") or ""
    zip_bytes = None
    zip_field = data.get("test_zip")
    if zip_field is not None and hasattr(zip_field, "file"):
        zip_bytes = zip_field.file.read()
    if test_mode == "file":
        uploaded_text = await asyncio.to_thread(_test_text_from_upload_sync, data)
        if uploaded_text:
            test_text = uploaded_text
        test_mode = "text"
    video_url, youtube_id = await asyncio.to_thread(_video_from_post_sync, data)
    is_paid = 1 if data.get("is_paid") == "on" else 0
    is_zachet = 1 if data.get("is_zachet") == "on" else 0

    if not title:
        raise web.HTTPFound("/admin/learn?message=Не указано название урока")

    # Зачёт — особый урок без теста/конспекта, банк вопросов настраивается отдельно
    if is_zachet:
        def _insert_zachet():
            db.execute(
                "INSERT INTO lessons (section_id, title, description, is_paid, is_zachet) "
                "VALUES (?, ?, ?, ?, 1)",
                (section_id, title, description, is_paid))
            return db.fetchone("SELECT last_insert_rowid() AS id")["id"]
        lid = await asyncio.to_thread(_insert_zachet)
        raise web.HTTPFound(f"/admin/learn/lessons/{lid}/zachet?message=Зачёт создан — импортируйте вопросы")

    if test_mode == "none" or (test_mode == "text" and not test_text.strip()) or \
            (test_mode == "zip" and not zip_bytes):
        def _insert_and_images():
            db.execute(
                "INSERT INTO lessons (section_id, title, description, content_html, video_url, "
                "youtube_id, is_paid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (section_id, title, description, content_html, video_url, youtube_id, is_paid))
            lid = db.fetchone("SELECT last_insert_rowid() AS id")["id"]
            _save_lesson_images_sync(lid, data)
        await asyncio.to_thread(_insert_and_images)
        raise web.HTTPFound("/admin/learn?message=Урок создан")

    draft_id = await asyncio.to_thread(
        _create_draft_sync, tg_id, section_id, None, title, description, content_html,
        test_mode, test_text, zip_bytes, video_url, youtube_id, is_paid,
    )
    raise web.HTTPFound(f"/admin/learn/drafts/{draft_id}")


def _create_draft_sync(admin_tg_id: int, section_id: int, lesson_id: Optional[int],
                        title: str, description: str, content: str,
                        test_mode: str, test_text: str, zip_bytes,
                        video_url: Optional[str] = None, youtube_id: Optional[str] = None,
                        is_paid: int = 0) -> int:
    if test_mode == "text":
        questions, errors = lesson_import.parse_draft_from_text(test_text)
    else:
        questions, errors = lesson_import.parse_draft_from_zip(zip_bytes)

    db.execute(
        "INSERT INTO lesson_test_drafts (admin_tg_id, section_id, lesson_id, lesson_title, "
        "lesson_description, lesson_content, questions_json, errors_json, video_url, youtube_id, "
        "is_paid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (admin_tg_id, section_id, lesson_id, title, description, content,
         json.dumps(questions, ensure_ascii=False), json.dumps(errors, ensure_ascii=False),
         video_url, youtube_id, is_paid),
    )
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def _get_draft_sync(draft_id: int) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM lesson_test_drafts WHERE id=?", (draft_id,))
    if not row:
        return None
    row = dict(row)
    row["questions"] = json.loads(row["questions_json"])
    row["errors"] = json.loads(row["errors_json"])
    return row


async def admin_view_draft(request: web.Request) -> web.Response:
    await _require_admin(request)
    draft_id = int(request.match_info["draft_id"])
    draft = await asyncio.to_thread(_get_draft_sync, draft_id)
    if draft is None:
        raise web.HTTPNotFound()
    context = await auth.nav_context(request)
    context["draft"] = draft
    context["questions_count"] = len(draft["questions"])
    context["preview_questions"] = draft["questions"][:15]
    return aiohttp_jinja2.render_template("admin_import_preview.html", request, context)


def _confirm_draft_sync(draft_id: int, admin_tg_id: int, settings: dict) -> str:
    draft = _get_draft_sync(draft_id)
    if not draft:
        return "Черновик не найден (возможно, уже подтверждён)"
    if not draft["questions"]:
        return "Нельзя подтвердить: не распознано ни одного вопроса"

    test_id = lesson_import.finalize_test(
        f"Тест: {draft['lesson_title']}", admin_tg_id, draft["questions"], settings
    )

    if draft["lesson_id"]:
        old = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (draft["lesson_id"],))
        if old and old["test_id"]:
            db.execute("DELETE FROM tests WHERE id=?", (old["test_id"],))
        db.execute("UPDATE lessons SET test_id=? WHERE id=?", (test_id, draft["lesson_id"]))
    else:
        db.execute(
            "INSERT INTO lessons (section_id, title, description, content_html, test_id, "
            "video_url, youtube_id, is_paid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (draft["section_id"], draft["lesson_title"], draft["lesson_description"],
             draft["lesson_content"], test_id, draft.get("video_url"), draft.get("youtube_id"),
             draft.get("is_paid") or 0),
        )

    db.execute("DELETE FROM lesson_test_drafts WHERE id=?", (draft_id,))
    # Новый тест урока наследует приватность предмета/платность урока в боте
    sec = db.fetchone("SELECT subject_id FROM sections WHERE id=?", (draft["section_id"],))
    if sec:
        _sync_subject_tests_privacy_sync(sec["subject_id"])
    added = len(draft["questions"])
    errs = len(draft["errors"])
    return f"Урок сохранён. Вопросов добавлено: {added}" + (f", ошибок формата: {errs}" if errs else "")


async def admin_confirm_draft(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    draft_id = int(request.match_info["draft_id"])
    data = await request.post()
    settings = {
        "show_correct": data.get("show_correct") == "on",
        "show_results": data.get("show_results") == "on",
        "attempts_limit": data.get("attempts_limit") or 0,
        "time_per_question": data.get("time_per_question") or 0,
        "shuffle_questions": data.get("shuffle_questions") == "on",
    }
    message = await asyncio.to_thread(_confirm_draft_sync, draft_id, tg_id, settings)
    raise web.HTTPFound(f"/admin/learn?message={message}")


async def admin_cancel_draft(request: web.Request) -> web.Response:
    await _require_admin(request)
    draft_id = int(request.match_info["draft_id"])
    await asyncio.to_thread(db.execute, "DELETE FROM lesson_test_drafts WHERE id=?", (draft_id,))
    raise web.HTTPFound("/admin/learn?message=Импорт отменён")


# === Админка: страница редактирования урока ===

def _lesson_edit_data_sync(lesson_id: int) -> Optional[dict]:
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        return None
    test = None
    if lesson["test_id"]:
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (lesson["test_id"],))
        if test:
            test = dict(test)
            test["questions_count"] = db.fetchone(
                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (lesson["test_id"],))["c"]
    return {"lesson": dict(lesson), "test": test,
            "lesson_images": _lesson_images_sync(lesson_id)}


async def admin_lesson_edit_page(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await asyncio.to_thread(_lesson_edit_data_sync, lesson_id)
    if data is None:
        raise web.HTTPNotFound()
    data.update(await auth.nav_context(request))
    data["message"] = request.query.get("message")
    return aiohttp_jinja2.render_template("admin_lesson_edit.html", request, data)


async def admin_edit_lesson(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    content_html = (data.get("content") or "").strip()
    try:
        extracted = await asyncio.to_thread(_content_from_upload_sync, data)
        if extracted:
            content_html = extracted
    except Exception as e:
        raise web.HTTPFound(
            f"/admin/learn/lessons/{lesson_id}/edit?message=Не удалось прочитать файл конспекта: {e}"
        )
    status = "open" if data.get("status") == "on" else "closed"
    is_paid = 1 if data.get("is_paid") == "on" else 0

    video_mode = data.get("video_mode") or "keep"
    if video_mode == "keep":
        video_url, youtube_id = None, None
        update_video = False
    elif video_mode == "remove":
        video_url, youtube_id = None, None
        update_video = True
    else:
        video_url, youtube_id = await asyncio.to_thread(_video_from_post_sync, data)
        update_video = True

    if title:
        if update_video:
            await asyncio.to_thread(
                db.execute,
                "UPDATE lessons SET title=?, description=?, content_html=?, status=?, is_paid=?, "
                "video_url=?, youtube_id=? WHERE id=?",
                (title, description, content_html, status, is_paid, video_url, youtube_id, lesson_id),
            )
        else:
            await asyncio.to_thread(
                db.execute,
                "UPDATE lessons SET title=?, description=?, content_html=?, status=?, is_paid=? WHERE id=?",
                (title, description, content_html, status, is_paid, lesson_id),
            )
        # Приложенные фото конспекта (несколько, до 40) — добавляем к уроку
        await asyncio.to_thread(_save_lesson_images_sync, lesson_id, data)

        def _sync_privacy():
            row = db.fetchone(
                "SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id WHERE l.id=?",
                (lesson_id,))
            if row:
                _sync_subject_tests_privacy_sync(row["subject_id"])
        await asyncio.to_thread(_sync_privacy)
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Урок обновлён")


async def admin_grant_lesson_access(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    target_raw = (data.get("user_tg_id") or "").strip()
    if not target_raw.isdigit():
        raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Некорректный Telegram ID")
    target_tg_id = int(target_raw)
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO lesson_access (lesson_id, user_tg_id, granted_by_admin) VALUES (?, ?, ?) "
        "ON CONFLICT(lesson_id, user_tg_id) DO NOTHING",
        (lesson_id, target_tg_id, tg_id),
    )
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Доступ к уроку выдан")


async def admin_save_settings(request: web.Request) -> web.Response:
    await _require_admin(request)
    data = await request.post()
    contact = (data.get("site_contact_username") or "").strip().lstrip("@")
    benefits = (data.get("premium_benefits_text") or "").strip()
    chat_url = (data.get("applicant_chat_url") or "").strip()
    ent_date = (data.get("ent_exam_date") or "").strip()

    def _save():
        for key, value in (
            ("site_contact_username", contact),
            ("premium_benefits_text", benefits),
            ("applicant_chat_url", chat_url),
            ("ent_exam_date", ent_date),
        ):
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    await asyncio.to_thread(_save)
    raise web.HTTPFound("/admin/learn?message=Настройки сохранены")


# === Приватные тесты (отдельная вкладка, доступ не связан с оплатой) ===
# Переиспользуем схему бота: tests.is_private=1 + таблица private_test_access
# (test_id, user_tg_id, expires_at) — доступ туда выдаёт админ и в боте, и тут.

def _has_private_test_access_sync(test_id: int, tg_id: int) -> bool:
    row = db.fetchone(
        "SELECT expires_at FROM private_test_access WHERE test_id=? AND user_tg_id=?",
        (test_id, tg_id),
    )
    if not row:
        return False
    if row["expires_at"]:
        try:
            return datetime.fromisoformat(row["expires_at"]) > datetime.utcnow()
        except ValueError:
            return True
    return True


def _list_private_tests_sync(tg_id: int) -> list:
    rows = db.fetchall(
        """SELECT t.*, pta.expires_at AS access_expires_at
           FROM tests t JOIN private_test_access pta ON pta.test_id = t.id
           WHERE t.is_private=1 AND t.status='active' AND pta.user_tg_id=?
           ORDER BY t.id DESC""",
        (tg_id,),
    )
    result = []
    for r in rows:
        r = dict(r)
        if r["access_expires_at"]:
            try:
                if datetime.fromisoformat(r["access_expires_at"]) <= datetime.utcnow():
                    continue  # доступ истёк
            except ValueError:
                pass
        result.append(r)
    return result


async def private_tests_index(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    tests = await asyncio.to_thread(_list_private_tests_sync, tg_id)
    context = await auth.nav_context(request)
    context["tests"] = tests
    return aiohttp_jinja2.render_template("private_tests.html", request, context)


def _start_private_test_attempt_sync(test_id: int, tg_id: int) -> Optional[dict]:
    test = db.fetchone("SELECT * FROM tests WHERE id=? AND is_private=1 AND status='active'", (test_id,))
    if not test or not _has_private_test_access_sync(test_id, tg_id):
        return None

    user = utils.get_user_by_tg(tg_id)
    user_id = user["id"]

    limit = test["attempts_limit"] or 0
    if limit:
        used = db.fetchone(
            "SELECT COUNT(*) AS c FROM test_attempts "
            "WHERE user_id=? AND test_id=? AND status='finished'",
            (user_id, test_id),
        )["c"]
        if used >= limit:
            return {"blocked": True}

    questions = db.fetchall(
        "SELECT id, text, web_image_path FROM questions WHERE test_id=? ORDER BY order_num, id",
        (test_id,),
    )
    questions = [dict(q) for q in questions]
    if test["shuffle_questions"]:
        random.shuffle(questions)
    q_ids = [q["id"] for q in questions]

    db.execute(
        "INSERT INTO test_attempts (user_id, test_id, question_order, status, is_counted) "
        "VALUES (?, ?, ?, 'in_progress', 1)",
        (user_id, test_id, json.dumps(q_ids)),
    )
    attempt_id = db.fetchone("SELECT last_insert_rowid() AS id")["id"]

    questions_out = []
    for q in questions:
        opts = db.fetchall(
            "SELECT id, text FROM question_options WHERE question_id=? ORDER BY order_num, id",
            (q["id"],),
        )
        opts = [dict(o) for o in opts]
        if test["shuffle_options"]:
            random.shuffle(opts)
        questions_out.append({
            "id": q["id"], "text": q["text"], "web_image_path": q["web_image_path"],
            "options": opts,
        })

    return {
        "attempt_id": attempt_id,
        "lesson": {"id": None, "title": test["title"]},
        "questions": questions_out,
        "time_per_question": test["time_per_question"] or 0,
        "show_correct": bool(test["show_correct"]),
    }


async def private_test_start(request: web.Request) -> web.Response:
    tg_id = await _require_login(request)
    test_id = int(request.match_info["test_id"])
    data = await asyncio.to_thread(_start_private_test_attempt_sync, test_id, tg_id)
    if data is None:
        raise web.HTTPNotFound(text="Тест не найден или нет доступа")
    if data.get("blocked"):
        raise web.HTTPFound("/private-tests?error=attempts_exceeded")
    data.update(await auth.nav_context(request))
    data["questions_json"] = json.dumps(data["questions"], ensure_ascii=False)
    data["answered_json"] = "{}"
    data["is_resume"] = False
    data["watermark_svg"] = await asyncio.to_thread(_watermark_svg_sync, tg_id)
    return aiohttp_jinja2.render_template("learn_test.html", request, data)


# === Бэкап: скачать полную резервную копию из админки сайта ===

def _build_backup_zip_sync() -> str:
    """Полный бэкап: сам файл базы (через sqlite backup API — безопасно при
    работающем боте) + JSON-выгрузки (совместимы с восстановлением в боте)
    + загруженные на сайт файлы (картинки вопросов, видео уроков)."""
    import sqlite3
    import tempfile
    import zipfile
    from services import backup_service as bs

    ts = datetime.now(ALMATY).strftime("%Y-%m-%d_%H%M")
    tmpdir = tempfile.mkdtemp(prefix="site_backup_")
    db_copy = os.path.join(tmpdir, "bot.db")

    # Быстрый снимок файла БД под коротким локом (не поблочный backup)
    db.snapshot_to(db_copy)

    zip_path = os.path.join(tmpdir, f"backup_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_copy, "bot.db")
        try:
            zf.writestr("backup.json",
                        json.dumps(bs.collect_backup_data(), ensure_ascii=False, indent=2))
            zf.writestr("users_access.json",
                        json.dumps(bs.collect_access_data(), ensure_ascii=False, indent=2))
            zf.writestr("settings.json",
                        json.dumps(bs.collect_settings_data(), ensure_ascii=False, indent=2))
            zf.writestr("users.json",
                        json.dumps(bs.collect_users_data(), ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning("site backup json: %s", e)
        uploads_root = Path(config.DB_PATH).resolve().parent / "uploads"
        if uploads_root.exists():
            for p in uploads_root.rglob("*"):
                if p.is_file():
                    zf.write(p, f"uploads/{p.relative_to(uploads_root)}")
    os.remove(db_copy)
    return zip_path


async def admin_download_backup(request: web.Request) -> web.Response:
    await _require_admin(request)
    zip_path = await asyncio.to_thread(_build_backup_zip_sync)
    # octet-stream + attachment: Safari не считает файл «безопасным» и НЕ
    # распаковывает его автоматически в папку — скачивается именно .zip
    return web.FileResponse(zip_path, headers={
        "Content-Type": "application/octet-stream",
        "Content-Disposition":
            f'attachment; filename="{os.path.basename(zip_path)}"',
        "X-Content-Type-Options": "nosniff",
    })


# === Восстановление из бэкапа (заливка кусками ≤15МБ — обходит лимит запроса) ===

def _restore_upload_path(tg_id: int) -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"smartent_restore_{tg_id}.zip")


async def admin_backup_upload_chunk(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    index = int(request.query.get("index", "0"))
    total = int(request.query.get("total", "1"))
    body = await request.read()
    base = _restore_upload_path(tg_id)

    def _write():
        # Каждый кусок пишется в СВОЙ part-файл (wb). Повторная отправка того же
        # куска (ретрай на слабой сети) просто перезаписывает его — без задвоения,
        # из-за которого архив становился битым («Bad magic number»).
        if index == 0:
            for old in glob.glob(base + ".part*"):
                try:
                    os.remove(old)
                except OSError:
                    pass
            try:
                os.remove(base)
            except OSError:
                pass
        with open(f"{base}.part{index}", "wb") as f:
            f.write(body)
        # Когда пришёл последний кусок — склеиваем строго по индексам
        if index == total - 1:
            with open(base, "wb") as out:
                for i in range(total):
                    part = f"{base}.part{i}"
                    if not os.path.exists(part):
                        raise IOError(f"пропущен кусок {i}")
                    with open(part, "rb") as pf:
                        shutil.copyfileobj(pf, out)
                    os.remove(part)
            return os.path.getsize(base)
        return os.path.getsize(f"{base}.part{index}")

    try:
        size = await asyncio.to_thread(_write)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True, "received": size})


def _restore_backup_sync(zip_path: str) -> str:
    """Полное восстановление: база через sqlite backup API (безопасно в живом
    процессе — атомарно замещает всё содержимое) + файлы uploads."""
    import sqlite3
    import tempfile
    import zipfile

    import shutil

    if not zipfile.is_zipfile(zip_path):
        return ("Файл повреждён или это не ZIP. Скачайте бэкап заново кнопкой "
                "«Скачать полный бэкап» и загрузите его целиком, не распаковывая.")
    tmp = tempfile.mkdtemp(prefix="restore_")
    try:
        zf = zipfile.ZipFile(zip_path)
        bad = zf.testzip()  # проверяем целостность ДО замены базы
        if bad is not None:
            zf.close()
            return ("Архив повреждён при загрузке (не докачался). Проверьте интернет "
                    "и попробуйте восстановить ещё раз — файл зальётся заново.")
    except zipfile.BadZipFile:
        return ("Архив повреждён («bad magic number»). Скачайте бэкап заново и "
                "загрузите целиком — вероятно, файл не докачался.")
    with zf:
        names = zf.namelist()
        # bot.db ищем на любой глубине: Finder при сжатии папки кладёт файлы
        # во вложенную папку (backup_.../bot.db) — принимаем и такой архив
        db_name = None
        for n in names:
            if n.endswith("bot.db") and ".." not in n and "__MACOSX" not in n:
                db_name = n
                break
        if not db_name:
            return "В архиве нет bot.db — это не полный бэкап сайта"
        with zf.open(db_name) as src_f, open(os.path.join(tmp, "bot.db"), "wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f)
        data_root = Path(config.DB_PATH).resolve().parent
        for n in names:
            if ".." in n or n.endswith("/") or "__MACOSX" in n:
                continue
            idx = n.find("uploads/")
            if idx == -1:
                continue
            rel = n[idx:]  # uploads/... независимо от вложенной папки
            target = data_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(n) as src_f, open(target, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)

    # Быстрая атомарная замена файла БД (не поблочный backup под локом —
    # именно из-за него все запросы висли и прокси отдавал 502).
    db.replace_database(os.path.join(tmp, "bot.db"))
    # Восстановленная база может быть СТАРОЙ схемы (сделана до появления новых
    # таблиц/колонок) — прогоняем миграции, иначе сайт отдаёт 500.
    try:
        db.init_db()
    except Exception as e:
        logger.warning("init_db после восстановления: %s", e)
    try:
        os.remove(os.path.join(tmp, "bot.db"))
    except OSError:
        pass
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return ""


# Статус фонового восстановления: tg_id -> {"state": idle|running|done|error}
# Восстановление идёт в фоне, потому что на Railway долгий HTTP-запрос
# обрывается прокси (502), хотя сама работа продолжается.
_restore_status: dict = {}


async def admin_backup_restore(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    path = _restore_upload_path(tg_id)
    if not os.path.exists(path):
        return web.json_response({"ok": False, "error": "Сначала загрузите файл бэкапа"})
    if (_restore_status.get(tg_id) or {}).get("state") == "running":
        return web.json_response({"ok": True, "already_running": True})
    _restore_status[tg_id] = {"state": "running", "error": ""}

    async def _run():
        try:
            error = await asyncio.to_thread(_restore_backup_sync, path)
            if error:
                _restore_status[tg_id] = {"state": "error", "error": error}
            else:
                _sub_cache.clear()
                _restore_status[tg_id] = {"state": "done", "error": ""}
        except Exception as e:
            logger.exception("Восстановление бэкапа упало")
            _restore_status[tg_id] = {"state": "error", "error": str(e)}

    asyncio.create_task(_run())
    return web.json_response({"ok": True, "started": True})


async def admin_backup_restore_status(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    st = _restore_status.get(tg_id) or {"state": "idle", "error": ""}
    return web.json_response(st)


# === Наши каналы (личный кабинет) ===

DEFAULT_APPLICANT_CHAT_URL = "https://t.me/+fo17_e1XrBAzZTEy"


def get_channels_context_sync() -> dict:
    channels = db.fetchall("SELECT * FROM site_channels ORDER BY sort_order, id")
    return {
        "channels": [dict(c) for c in channels],
        "applicant_chat_url": _setting_sync("applicant_chat_url", DEFAULT_APPLICANT_CHAT_URL),
    }


async def admin_add_required_channel(request: web.Request) -> web.Response:
    """Привязать обязательный канал: к предмету (subject_id) или глобально."""
    await _require_admin(request)
    data = await request.post()
    uname = (data.get("channel_username") or "").strip().lstrip("@")
    title = (data.get("title") or "").strip()
    subject_id_raw = (data.get("subject_id") or "").strip()
    subject_id = int(subject_id_raw) if subject_id_raw.isdigit() else None
    if uname:
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO required_channels (channel_username, title, is_global, subject_id, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (uname, title, 0 if subject_id else 1, subject_id),
        )
    _sub_cache.clear()  # сбросить кэш проверок — новые требования
    raise web.HTTPFound("/admin/learn?message=Обязательный канал добавлен")


async def admin_delete_required_channel(request: web.Request) -> web.Response:
    await _require_admin(request)
    channel_id = int(request.match_info["channel_id"])
    await asyncio.to_thread(
        db.execute, "DELETE FROM required_channels WHERE id=?", (channel_id,))
    _sub_cache.clear()
    raise web.HTTPFound("/admin/learn?message=Обязательный канал удалён")


def _required_channels_admin_sync() -> dict:
    rows = db.fetchall(
        "SELECT * FROM required_channels WHERE COALESCE(is_active,1)=1 "
        "AND (is_global=1 OR subject_id IS NOT NULL) ORDER BY id")
    global_channels, by_subject = [], {}
    for r in rows:
        r = dict(r)
        if r.get("is_global"):
            global_channels.append(r)
        elif r.get("subject_id"):
            by_subject.setdefault(r["subject_id"], []).append(r)
    return {"global_required_channels": global_channels,
            "subject_required_channels": by_subject}


async def admin_add_channel(request: web.Request) -> web.Response:
    await _require_admin(request)
    data = await request.post()
    title = (data.get("title") or "").strip()
    url = (data.get("url") or "").strip()
    if title and url:
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO site_channels (title, url) VALUES (?, ?)",
            (title, url),
        )
    raise web.HTTPFound("/admin/learn?message=Канал добавлен")


async def admin_delete_channel(request: web.Request) -> web.Response:
    await _require_admin(request)
    channel_id = int(request.match_info["channel_id"])
    await asyncio.to_thread(db.execute, "DELETE FROM site_channels WHERE id=?", (channel_id,))
    raise web.HTTPFound("/admin/learn?message=Канал удалён")


async def admin_toggle_lesson(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])

    def _toggle():
        row = db.fetchone("SELECT status FROM lessons WHERE id=?", (lesson_id,))
        new_status = "closed" if row["status"] == "open" else "open"
        db.execute("UPDATE lessons SET status=? WHERE id=?", (new_status, lesson_id))

    await asyncio.to_thread(_toggle)
    raise web.HTTPFound("/admin/learn?message=Статус урока изменён")


def _delete_lesson_sync(lesson_id: int) -> None:
    row = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (lesson_id,))
    if row and row["test_id"]:
        db.execute("DELETE FROM tests WHERE id=?", (row["test_id"],))
    db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))


async def admin_delete_lesson(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(_delete_lesson_sync, lesson_id)
    raise web.HTTPFound("/admin/learn?message=Урок удалён")


async def admin_delete_lesson_content(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(
        db.execute, "UPDATE lessons SET content_html='' WHERE id=?", (lesson_id,)
    )
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Конспект удалён")


def _delete_lesson_test_sync(lesson_id: int) -> None:
    row = db.fetchone("SELECT test_id FROM lessons WHERE id=?", (lesson_id,))
    if row and row["test_id"]:
        db.execute("DELETE FROM tests WHERE id=?", (row["test_id"],))
        db.execute("UPDATE lessons SET test_id=NULL WHERE id=?", (lesson_id,))


async def admin_delete_lesson_test(request: web.Request) -> web.Response:
    await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    await asyncio.to_thread(_delete_lesson_test_sync, lesson_id)
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Тест удалён")


async def admin_replace_lesson_test(request: web.Request) -> web.Response:
    tg_id = await _require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    data = await request.post()
    test_mode = data.get("test_mode") or "text"
    test_text = data.get("test_text") or ""
    zip_bytes = None
    zip_field = data.get("test_zip")
    if zip_field is not None and hasattr(zip_field, "file"):
        zip_bytes = zip_field.file.read()
    if test_mode == "file":
        uploaded_text = await asyncio.to_thread(_test_text_from_upload_sync, data)
        if uploaded_text:
            test_text = uploaded_text
        test_mode = "text"

    lesson = await asyncio.to_thread(db.fetchone, "SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        raise web.HTTPNotFound()

    if (test_mode == "text" and not test_text.strip()) or (test_mode == "zip" and not zip_bytes):
        raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Не выбран файл/текст теста")

    draft_id = await asyncio.to_thread(
        _create_draft_sync, tg_id, lesson["section_id"], lesson_id,
        lesson["title"], lesson["description"], lesson["content_html"],
        test_mode, test_text, zip_bytes,
    )
    raise web.HTTPFound(f"/admin/learn/drafts/{draft_id}")


async def admin_test_settings(request: web.Request) -> web.Response:
    await _require_admin(request)
    test_id = int(request.match_info["test_id"])
    data = await request.post()
    lesson_id = data.get("lesson_id")

    settings = (
        1 if data.get("show_correct") == "on" else 0,
        1 if data.get("show_correct") == "on" else 0,
        1 if data.get("show_results") == "on" else 0,
        int(data.get("attempts_limit") or 0),
        int(data.get("time_per_question") or 0),
        1 if data.get("shuffle_questions") == "on" else 0,
        test_id,
    )
    await asyncio.to_thread(
        db.execute,
        "UPDATE tests SET show_correct=?, show_explanation=?, show_results=?, "
        "attempts_limit=?, time_per_question=?, shuffle_questions=? WHERE id=?",
        settings,
    )
    raise web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit?message=Настройки теста сохранены")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/learn", learn_index)
    app.router.add_get("/private-tests", private_tests_index)
    app.router.add_get("/private-tests/{test_id:\\d+}/start", private_test_start)
    app.router.add_get("/learn/{subject_id:\\d+}", learn_subject)
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}", learn_lesson)
    app.router.add_get("/learn/lesson/{lesson_id:\\d+}/test", learn_test_start)
    app.router.add_post("/learn/api/test/{attempt_id:\\d+}/answer", learn_test_answer)
    app.router.add_post("/learn/api/test/{attempt_id:\\d+}/finish", learn_test_finish)
    app.router.add_get("/learn/test/{attempt_id:\\d+}/result", learn_test_result)
    app.router.add_get("/uploads/questions/{filename}", uploaded_question_image)
    app.router.add_get("/uploads/videos/{filename}", uploaded_video)
    app.router.add_get("/uploads/lessons/{filename}", uploaded_lesson_image)
    app.router.add_post("/admin/learn/images/{image_id:\\d+}/delete", admin_delete_lesson_image)

    app.router.add_get("/admin/learn", admin_learn_index)
    app.router.add_post("/admin/learn/settings", admin_save_settings)

    app.router.add_post("/admin/learn/subjects/create", admin_create_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/edit", admin_edit_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/delete", admin_delete_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/toggle", admin_toggle_subject)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/access/grant", admin_grant_access)
    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/access/revoke", admin_revoke_access)

    app.router.add_post("/admin/learn/subjects/{subject_id:\\d+}/sections/create", admin_create_section)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/edit", admin_edit_section)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/delete", admin_delete_section)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/reorder", admin_reorder_lessons)
    app.router.add_post("/admin/learn/sections/{section_id:\\d+}/lessons/create", admin_create_lesson)

    app.router.add_get("/admin/learn/lessons/{lesson_id:\\d+}/edit", admin_lesson_edit_page)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/edit", admin_edit_lesson)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/toggle", admin_toggle_lesson)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/delete", admin_delete_lesson)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/delete_content", admin_delete_lesson_content)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/delete_test", admin_delete_lesson_test)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/test/replace", admin_replace_lesson_test)
    app.router.add_post("/admin/learn/lessons/{lesson_id:\\d+}/access/grant", admin_grant_lesson_access)

    app.router.add_post("/admin/learn/tests/{test_id:\\d+}/settings", admin_test_settings)

    app.router.add_get("/admin/learn/drafts/{draft_id:\\d+}", admin_view_draft)
    app.router.add_post("/admin/learn/drafts/{draft_id:\\d+}/confirm", admin_confirm_draft)
    app.router.add_post("/admin/learn/drafts/{draft_id:\\d+}/cancel", admin_cancel_draft)
    app.router.add_post("/admin/learn/channels/create", admin_add_channel)
    app.router.add_post("/admin/learn/channels/{channel_id:\\d+}/delete", admin_delete_channel)
    app.router.add_post("/admin/learn/reqchannels/create", admin_add_required_channel)
    app.router.add_post("/admin/learn/reqchannels/{channel_id:\\d+}/delete", admin_delete_required_channel)
    app.router.add_get("/admin/backup.zip", admin_download_backup)
    app.router.add_post("/admin/backup/upload-chunk", admin_backup_upload_chunk)
    app.router.add_post("/admin/backup/restore", admin_backup_restore)
    app.router.add_get("/admin/backup/restore-status", admin_backup_restore_status)
    app.router.add_post("/admin/site-admins/add", admin_add_site_admin)
    app.router.add_post("/admin/site-admins/{tg_id:\\d+}/remove", admin_remove_site_admin)
