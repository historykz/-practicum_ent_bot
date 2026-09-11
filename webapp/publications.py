"""
Ручной выбор вопросов для публикации в Telegram-канал.

Экраны админа:
  /admin/publications                — список подборок и черновиков
  /admin/publications/new            — предмет → раздел → урок → тест
  /admin/publications/pick/{test_id} — сами вопросы с чекбоксами
  /admin/publications/{id}           — предпросмотр перед отправкой

Ученик открывает подборку ссылкой вида ?startapp=pub_936 — и проходит ровно
те вопросы, что отметил админ, в заданном им порядке.
"""
import asyncio
import json
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web


def _safe_json(value) -> str:
    """JSON для вставки в <script>.

    json.dumps не экранирует "</script>", и вопрос с таким текстом закрывал бы
    тег раньше времени — страница ломалась, а на сайт попадала чужая разметка.
    """
    return (json.dumps(value, ensure_ascii=False)
            .replace("</", "<\\/")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))

import database as db
import utils
from services import publication_service as ps
from webapp import auth


async def _require_admin(request):
    from webapp import learning
    return await learning._require_admin(request)


def _back(url: str, msg: str = ""):
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return web.HTTPFound(url)


# ---------- Список подборок ----------

async def pub_index(request: web.Request) -> web.Response:
    await _require_admin(request)
    ctx = await auth.nav_context(request)
    items = await asyncio.to_thread(ps.all_publications)
    ctx["drafts"] = [p for p in items if p["status"] != "published"]
    ctx["published"] = [p for p in items if p["status"] == "published"]
    ctx["msg"] = request.query.get("msg", "")
    return aiohttp_jinja2.render_template("admin_publications.html", request, ctx)


# ---------- Выбор предмет → раздел → урок → тест ----------

def _tree_sync() -> list:
    """Предметы с разделами, уроками и их тестами — для выпадающих списков."""
    out = []
    for s in db.fetchall("SELECT id, title FROM subjects ORDER BY sort_order, id"):
        subj = {"id": s["id"], "title": s["title"], "sections": []}
        for sec in db.fetchall(
                "SELECT id, title FROM sections WHERE subject_id=? ORDER BY sort_order, id",
                (s["id"],)):
            block = {"id": sec["id"], "title": sec["title"], "lessons": []}
            for l in db.fetchall(
                    "SELECT l.id, l.title, l.test_id, l.original_id, "
                    "(SELECT COUNT(*) FROM questions q WHERE q.test_id = "
                    " COALESCE((SELECT test_id FROM lessons o WHERE o.id=l.original_id), l.test_id)"
                    ") AS qcount "
                    "FROM lessons l WHERE l.section_id=? ORDER BY l.sort_order, l.id",
                    (sec["id"],)):
                test_id = l["test_id"]
                if not test_id and l["original_id"]:
                    orig = db.fetchone("SELECT test_id FROM lessons WHERE id=?",
                                       (l["original_id"],))
                    test_id = orig["test_id"] if orig else None
                if not test_id:
                    continue            # уроки без теста публиковать нечем
                block["lessons"].append({
                    "id": l["id"], "title": l["title"],
                    "test_id": test_id, "qcount": l["qcount"] or 0,
                })
            if block["lessons"]:
                subj["sections"].append(block)
        if subj["sections"]:
            out.append(subj)
    return out


async def pub_new(request: web.Request) -> web.Response:
    await _require_admin(request)
    ctx = await auth.nav_context(request)
    ctx["tree"] = await asyncio.to_thread(_tree_sync)
    ctx["tree_json"] = _safe_json(ctx["tree"])
    return aiohttp_jinja2.render_template("admin_pub_new.html", request, ctx)


# ---------- Страница выбора вопросов ----------

def _pick_data_sync(test_id: int, lesson_id, pub_id) -> dict:
    test = db.fetchone("SELECT id, title FROM tests WHERE id=?", (test_id,))
    if not test:
        return {}
    questions = ps.test_questions(test_id)
    lesson = db.fetchone("SELECT id, title FROM lessons WHERE id=?",
                         (lesson_id,)) if lesson_id else None
    subject, section = ps._titles_for(lesson_id)
    chosen = []
    pub = ps.get(pub_id) if pub_id else None
    if pub:
        chosen = ps.question_ids(pub)
    return {
        "test": dict(test), "questions": questions,
        "lesson": (dict(lesson) if lesson else None),
        "lesson_id": lesson_id,
        "subject_title": subject, "section_title": section,
        "chosen": chosen, "pub": pub,
    }


async def pub_pick(request: web.Request) -> web.Response:
    await _require_admin(request)
    test_id = int(request.match_info["test_id"])
    lesson_id = request.query.get("lesson")
    lesson_id = int(lesson_id) if (lesson_id or "").isdigit() else None
    pub_id = request.query.get("pub")
    pub_id = int(pub_id) if (pub_id or "").isdigit() else None

    data = await asyncio.to_thread(_pick_data_sync, test_id, lesson_id, pub_id)
    if not data:
        raise _back("/admin/publications", "Тест не найден.")
    ctx = await auth.nav_context(request)
    ctx.update(data)
    ctx["questions_json"] = _safe_json(data["questions"])
    ctx["chosen_json"] = _safe_json(data["chosen"])
    ctx["msg"] = request.query.get("msg", "")
    return aiohttp_jinja2.render_template("admin_pub_pick.html", request, ctx)


async def pub_save(request: web.Request) -> web.Response:
    """Сохранить выбор: как черновик или сразу перейти к предпросмотру."""
    admin_id = await _require_admin(request)
    data = await request.post()
    test_id = int(data.get("test_id") or 0)
    lesson_id = data.get("lesson_id")
    lesson_id = int(lesson_id) if (lesson_id or "").isdigit() else None
    pub_id = data.get("pub_id")
    pub_id = int(pub_id) if (pub_id or "").isdigit() else None

    # Порядок пришёл с фронта строкой id через запятую — ровно как отметил админ
    raw_ids = (data.get("ordered_ids") or "").strip()
    ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
    if not test_id or not ids:
        target = f"/admin/publications/pick/{test_id}" if test_id else "/admin/publications"
        raise _back(target, "Не выбрано ни одного вопроса.")

    title = (data.get("title") or "").strip()
    intro = (data.get("intro") or "").strip()

    if pub_id:
        await asyncio.to_thread(ps.update_questions, pub_id, ids)
        await asyncio.to_thread(ps.update_meta, pub_id, title, intro)
    else:
        pub_id = await asyncio.to_thread(
            ps.create, test_id, lesson_id, ids, title, admin_id, intro)

    if (data.get("action") or "") == "draft":
        raise _back("/admin/publications",
                    f"Черновик №{pub_id} сохранён — опубликуете, когда будете готовы.")
    raise web.HTTPFound(f"/admin/publications/{pub_id}")


# ---------- Предпросмотр ----------

async def pub_preview(request: web.Request) -> web.Response:
    await _require_admin(request)
    pub_id = int(request.match_info["pub_id"])
    pub = await asyncio.to_thread(ps.get, pub_id)
    if not pub:
        raise _back("/admin/publications", "Публикация не найдена.")
    info = await asyncio.to_thread(ps.describe, pub)
    ctx = await auth.nav_context(request)
    ctx.update(info)
    ctx["link"] = ps.publication_link(pub_id)
    raw_text = ps.channel_text(pub)
    ctx["channel_text"] = raw_text
    # В канал уходит HTML-разметка, а на странице показываем чистый текст:
    # так подпись админа не превращается в разметку сайта.
    ctx["channel_text_plain"] = (raw_text.replace("<b>", "").replace("</b>", "")
                                 .replace("<i>", "").replace("</i>", ""))
    ctx["channels"] = await asyncio.to_thread(_channels_sync)
    ctx["msg"] = request.query.get("msg", "")
    return aiohttp_jinja2.render_template("admin_pub_preview.html", request, ctx)


def _channels_sync() -> list:
    from services import autopub_service
    try:
        return autopub_service.get_channels()
    except Exception:
        return []


# ---------- Действия ----------

async def pub_action(request: web.Request) -> web.Response:
    admin_id = await _require_admin(request)
    action = request.match_info["action"]
    pub_id = int(request.match_info["pub_id"])
    data = await request.post()
    pub = await asyncio.to_thread(ps.get, pub_id)
    if not pub:
        raise _back("/admin/publications", "Публикация не найдена.")

    if action == "delete":
        await asyncio.to_thread(ps.delete, pub_id)
        raise _back("/admin/publications", "Публикация удалена.")

    if action == "copy":
        new_id = await asyncio.to_thread(ps.duplicate, pub_id, admin_id)
        raise _back(f"/admin/publications/{new_id}",
                    "Создана копия — можно менять вопросы и публиковать заново.")

    if action == "publish":
        if pub["status"] == "published":
            # Повторное нажатие (двойной клик, F5) не должно слать второй пост
            raise _back(f"/admin/publications/{pub_id}",
                        "Эта подборка уже опубликована. Нужен новый пост — "
                        "сделайте копию.")
        channel_id = (data.get("channel_id") or "").strip()
        ok, msg = await _publish_to_channel(pub, channel_id, admin_id)
        raise _back(f"/admin/publications/{pub_id}", msg)

    raise _back(f"/admin/publications/{pub_id}", "Неизвестное действие.")


async def _publish_to_channel(pub: dict, channel_id: str, admin_id: int) -> tuple:
    """Отправляем в канал один аккуратный пост с кнопкой «НАЧАТЬ ТЕСТ»."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from services import autopub_service

    if not channel_id:
        chans = await asyncio.to_thread(_channels_sync)
        if chans:
            channel_id = str(chans[0]["id"])
        else:
            cfg = await asyncio.to_thread(autopub_service.get_autopub_config)
            channel_id = str(cfg.get("channel_id") or "")
    if not channel_id:
        return False, "Канал не задан. Добавьте его в настройках автопубликации."

    text = ps.channel_text(pub)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 НАЧАТЬ ТЕСТ",
                             url=ps.publication_link(pub["id"]))
    ]])

    # Сайт и бот — разные процессы, поэтому поднимаем отдельного клиента
    # по токену, как это уже сделано для картинок из Telegram.
    from aiogram import Bot
    import config as _cfg
    bot = Bot(token=_cfg.BOT_TOKEN)
    try:
        sent = await bot.send_message(int(channel_id), text,
                                      parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        return False, f"Не удалось отправить в канал: {e}"
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass

    await asyncio.to_thread(ps.mark_published, pub["id"], channel_id,
                            getattr(sent, "message_id", None))
    return True, "Опубликовано в канале ✅"


# ---------- Экран ученика ----------

async def _is_admin(request) -> bool:
    """Админ ли смотрит — ему видны и черновики (для проверки перед отправкой)."""
    try:
        tg_id = await auth.get_logged_in_tg_id(request)
    except Exception:
        return False
    if tg_id is None:
        return False
    try:
        return bool(await asyncio.to_thread(utils.is_site_admin, tg_id))
    except Exception:
        return False


async def pub_landing(request: web.Request) -> web.Response:
    """Куда приводит ссылка из канала: что за тест и кнопка «Начать тест»."""
    pub_id = int(request.match_info["pub_id"])
    pub = await asyncio.to_thread(ps.get, pub_id)
    if not pub:
        raise web.HTTPFound("/?error=pub_not_found")
    # Черновик — внутренняя кухня: посторонним не показываем даже название урока
    if pub["status"] != "published" and not await _is_admin(request):
        raise web.HTTPFound("/?error=pub_not_found")
    info = await asyncio.to_thread(ps.describe, pub)
    ctx = await auth.nav_context(request)
    ctx.update({
        "pub": pub,
        "subject_title": info["subject_title"],
        "section_title": info["section_title"],
        "lesson_title": info["lesson_title"],
        "test_title": info["test_title"],
        "count": info["count"],
        "error": request.query.get("error", ""),
    })
    return aiohttp_jinja2.render_template("pub_landing.html", request, ctx)


async def pub_run(request: web.Request) -> web.Response:
    """Прохождение подборки: ровно выбранные вопросы, в заданном порядке."""
    from webapp import learning
    tg_id = await learning._require_login(request)
    pub_id = int(request.match_info["pub_id"])

    pub_check = await asyncio.to_thread(ps.get, pub_id)
    if not pub_check:
        raise web.HTTPFound("/?error=pub_not_found")
    if pub_check["status"] != "published" and not await _is_admin(request):
        raise web.HTTPFound("/?error=pub_not_found")

    # «Начать заново» с баннера — та же кнопка, что и в обычном тесте
    if request.query.get("restart") == "1":
        await asyncio.to_thread(ps.abort_attempt, pub_id, tg_id)

    started = await asyncio.to_thread(ps.start_attempt, pub_id, tg_id)
    if not started:
        raise web.HTTPFound(f"/pub/{pub_id}?error=empty")

    pub = await asyncio.to_thread(ps.get, pub_id)
    info = await asyncio.to_thread(ps.describe, pub)
    # Опубликованная подборка идёт по снимку — она заморожена на момент отправки
    questions = await asyncio.to_thread(
        ps.questions_for_run, pub, started["q_ids"], started["options_order"])
    if not questions:
        questions = await asyncio.to_thread(
            learning._build_questions_out, started["q_ids"], started["options_order"])

    answered = {}
    if started["resume"]:
        answered = await asyncio.to_thread(_answered_sync, started["attempt_id"],
                                           started["test"])

    ctx = await auth.nav_context(request)
    ctx.update({
        "attempt_id": started["attempt_id"],
        "lesson": {"id": pub.get("lesson_id") or 0,
                   "title": info["lesson_title"] or info["test_title"] or "Тест"},
        "questions": questions,
        "questions_json": _safe_json(questions),
        "answered_json": _safe_json(answered),
        "answered": answered,
        "time_per_question": started["test"].get("time_per_question") or 0,
        "show_correct": bool(started["test"].get("show_correct")),
        "is_resume": started["resume"],
        "has_note": False,
        "watermark_svg": await asyncio.to_thread(learning._watermark_svg_sync, tg_id),
        "publication_id": pub_id,
    })
    return aiohttp_jinja2.render_template("learn_test.html", request, ctx)


def _answered_sync(attempt_id: int, test: dict) -> dict:
    """Уже данные ответы — чтобы продолжить с того же места."""
    rows = db.fetchall(
        "SELECT question_id, selected_option_id, is_correct, skipped "
        "FROM attempt_answers WHERE attempt_id=?", (attempt_id,))
    show_correct = bool(test.get("show_correct"))
    out = {}
    for r in rows:
        entry = {"selected_option_id": r["selected_option_id"],
                 "correct": bool(r["is_correct"]) if show_correct else None,
                 "skipped": bool(r["skipped"]), "correct_option_id": None}
        if show_correct:
            co = db.fetchone(
                "SELECT id FROM question_options WHERE question_id=? AND is_correct=1",
                (r["question_id"],))
            entry["correct_option_id"] = co["id"] if co else None
        out[str(r["question_id"])] = entry
    return out


def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/publications", pub_index)
    app.router.add_get("/admin/publications/new", pub_new)
    app.router.add_get(r"/admin/publications/pick/{test_id:\d+}", pub_pick)
    app.router.add_post("/admin/publications/save", pub_save)
    app.router.add_get(r"/admin/publications/{pub_id:\d+}", pub_preview)
    app.router.add_post(r"/admin/publications/{pub_id:\d+}/{action}", pub_action)
    # Ученик: ссылка из канала
    app.router.add_get(r"/pub/{pub_id:\d+}", pub_landing)
    app.router.add_get(r"/pub/{pub_id:\d+}/test", pub_run)
