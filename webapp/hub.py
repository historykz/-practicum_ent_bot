"""
Разделы нижнего меню: «Карточки» и «Тесты».

Раньше карточки и тесты жили только внутри урока — попасть в них можно было,
лишь вспомнив, в каком уроке они лежат. В нижнем меню появились свои вкладки,
и им нужны собственные страницы: список всего, что доступно ученику прямо
сейчас, с понятной кнопкой у каждой строки.
"""
import asyncio

import aiohttp_jinja2
from aiohttp import web

import database as db
import utils
from webapp import auth, learning
from webapp import shortcuts as sc


def _available_lessons_sync(tg_id, need_test: bool) -> list:
    """Предметы с уроками, которые ученик может открыть прямо сейчас.

    need_test=True — только уроки с тестом (для карточек и тестов он нужен).
    Платные уроки без доступа не прячем, а помечаем: пусть видно, что есть
    дальше, иначе непонятно, за что платить.
    """
    is_adm = bool(tg_id) and utils.is_site_admin(tg_id)
    out = []
    for subj in learning._list_subjects_public_sync(tg_id):
        items = []
        for lesson in learning._flatten_subject_lessons_sync(subj["id"]):
            if lesson.get("status") != "open":
                continue
            if need_test and not lesson.get("test_id"):
                continue
            locked = bool(lesson.get("is_paid")) and not (
                is_adm or learning._has_lesson_paid_access_sync(lesson, tg_id))
            items.append({
                "id": sc.lesson_url_id(lesson),
                "title": lesson.get("title") or "",
                "locked": locked,
                "qcount": (db.fetchone(
                    "SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                    (lesson["test_id"],))["c"] if lesson.get("test_id") else 0),
            })
        if items:
            out.append({"subject": subj, "lessons": items})
    return out


async def cards_page(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        raise web.HTTPFound("/?error=login_required")
    data = await auth.nav_context(request)
    data["groups"] = await asyncio.to_thread(_available_lessons_sync, tg_id, True)
    data["mode"] = "cards"
    return aiohttp_jinja2.render_template("hub_cards.html", request, data)


async def tests_page(request: web.Request) -> web.Response:
    tg_id = await auth.get_logged_in_tg_id(request)
    if tg_id is None:
        raise web.HTTPFound("/?error=login_required")
    data = await auth.nav_context(request)
    data["groups"] = await asyncio.to_thread(_available_lessons_sync, tg_id, True)
    data["mode"] = "tests"
    return aiohttp_jinja2.render_template("hub_tests.html", request, data)


async def premium_page(request: web.Request) -> web.Response:
    """Раздел «Premium»: что даёт подписка и как её купить."""
    tg_id = await auth.get_logged_in_tg_id(request)
    data = await auth.nav_context(request)
    data.update(await asyncio.to_thread(learning._paywall_context_sync))

    def _state():
        user = utils.get_user_by_tg(tg_id) if tg_id else None
        info = utils.get_premium_info(user["id"]) if user else None
        active = bool(user and utils.is_premium(user["id"]))
        until = ""
        if info and info.get("expires_at"):
            until = str(info["expires_at"])[:10]
        # Сколько всего платного контента открывает подписка — цифры убеждают
        # лучше обещаний, поэтому считаем по факту.
        paid = db.fetchone(
            "SELECT COUNT(*) AS c FROM lessons WHERE COALESCE(is_paid,0)=1 "
            "AND status='open'")["c"]
        subs = db.fetchone(
            "SELECT COUNT(DISTINCT s.id) AS c FROM subjects s "
            "JOIN sections sec ON sec.subject_id=s.id "
            "JOIN lessons l ON l.section_id=sec.id "
            "WHERE COALESCE(l.is_paid,0)=1 AND s.status='active'")["c"]
        qs = db.fetchone(
            "SELECT COUNT(*) AS c FROM questions q JOIN lessons l ON l.test_id=q.test_id "
            "WHERE COALESCE(l.is_paid,0)=1")["c"]
        return {"premium_active": active, "premium_until": until,
                "paid_lessons": paid, "paid_subjects": subs, "paid_questions": qs}

    data.update(await asyncio.to_thread(_state))
    return aiohttp_jinja2.render_template("premium.html", request, data)


def register_routes(app):
    app.router.add_get("/premium", premium_page)
    app.router.add_get("/cards", cards_page)
    app.router.add_get("/tests", tests_page)
