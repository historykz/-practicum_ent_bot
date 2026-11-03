"""Админка сайта: «Анонсы и автотесты» и «Реклама после тестов» (v95).
Та же логика, что в боте: services/announce_service.py."""
import asyncio
import logging
import os
import time
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web

import database as db
import utils
from services import announce_service as an
from services import auto_schedule_service as ass
from services import trash
from webapp import auth, learning

log = logging.getLogger(__name__)
AD_EXT = {"photo": (".jpg", ".jpeg", ".png", ".webp"), "video": (".mp4", ".mov"), "animation": (".gif", ".mp4")}
AD_MAX = 19 * 1024 * 1024          # чуть меньше лимита загрузки сайта


def _go(path: str, message: str = "", error: str = "") -> web.HTTPFound:
    q = ("message=" + quote(message)) if message else ("error=" + quote(error) if error else "")
    return web.HTTPFound(path + ("?" + q if q else ""))


def _int(v, d=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return d


def _page_sync() -> dict:
    scheds = ass.list_schedules()
    for s in scheds:
        s["link_effective"] = an.chat_link(s)
        s["next"] = s.get("next_run_at") or ass.refresh_next_run(s["id"]) or ""
    cats = [dict(r) for r in db.fetchall("SELECT id, name FROM test_categories ORDER BY sort_order, id")]
    return {"scheds": scheds, "templates": an.templates(), "kinds": an.KINDS, "placeholders": an.PLACEHOLDERS,
            "cats": cats, "cat_names": {c["id"]: c["name"] for c in cats}, "defaults": an.DEFAULT_TEXTS,
            "tz": an.tz_name(), "tz_choices": an.TZ_CHOICES, "journal": an.journal(60),
            "ads": an.ads(), "ad_kinds": an.AD_KINDS, "ads_cfg": an.ads_settings(), "now": utils.now_iso()}


async def page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    ctx = await auth.nav_context(request)
    ctx.update(await asyncio.to_thread(_page_sync))
    ctx.update({"message": request.query.get("message"), "error": request.query.get("error")})
    return aiohttp_jinja2.render_template("admin_announce.html", request, ctx)


async def sched_save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    sid = int(request.match_info["sid"])
    post = await request.post()
    link = (post.get("chat_link") or "").strip()
    if link and not an.valid_link(link):
        raise _go("/admin/announce", error="Ссылка на чат должна быть вида https://t.me/+AbCd или https://t.me/имя_чата.")
    fields = {"chat_link": link, "pre_announce": 1 if post.get("pre_announce") else 0,
              "pre_minutes": max(1, min(_int(post.get("pre_minutes"), 60), 24 * 60)),
              "start_announce": 1 if post.get("start_announce") else 0,
              "ads_enabled": 1 if post.get("ads_enabled") else 0,
              "pre_template_id": _int(post.get("pre_template_id")) or None,
              "start_template_id": _int(post.get("start_template_id")) or None}
    channel = (post.get("channel_id") or "").strip()
    if channel and channel.lstrip("-").isdigit():
        fields["channel_id"] = channel
    sets = ", ".join(f"{k}=?" for k in fields)
    await asyncio.to_thread(db.execute, f"UPDATE auto_schedule SET {sets} WHERE id=?", list(fields.values()) + [sid])
    await asyncio.to_thread(trash.audit, actor, "", "site", "settings", "", "Анонсы расписания", sid, str(fields))
    raise _go("/admin/announce", message="Настройки анонсов сохранены.")


async def tz_save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    if not an.set_tz(post.get("tz") or ""):
        raise _go("/admin/announce", error="Неизвестный часовой пояс.")
    for s in await asyncio.to_thread(ass.list_schedules):
        await asyncio.to_thread(ass.refresh_next_run, s["id"])
    await asyncio.to_thread(trash.audit, actor, "", "site", "settings", "", "Часовой пояс расписаний", None, post.get("tz"))
    raise _go("/admin/announce", message="Часовой пояс сохранён.")


async def tpl_save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    if post.get("kind") not in an.KINDS:
        raise _go("/admin/announce", error="Выберите вид анонса.")
    tid = await asyncio.to_thread(an.save_template, _int(post.get("id")) or None, post.get("title") or "", post.get("kind"),
                                  post.get("body") or "", post.get("category_id"), (post.get("sticker") or "").strip())
    await asyncio.to_thread(trash.audit, actor, "", "site", "save", "", "Шаблон анонса", tid, post.get("title") or "")
    raise _go("/admin/announce", message="Шаблон сохранён.")


async def tpl_delete(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    tid = int(request.match_info["tid"])
    await asyncio.to_thread(an.delete_template, tid)
    await asyncio.to_thread(trash.audit, actor, "", "site", "delete", "", "Шаблон анонса", tid, "")
    raise _go("/admin/announce", message="Шаблон удалён.")


def _new_bot():
    from webapp import admin_hub
    return admin_hub._new_bot()


def _slot_tests_sync(s: dict) -> tuple:
    from handlers import announce_admin as aa
    day, hm = aa._next_slot(s)
    return day, hm, aa._slot_tests(s, day, hm)


async def sched_send(request: web.Request) -> web.Response:
    """Предпросмотр админу в Telegram или публикация в канал (после подтверждения на странице)."""
    actor = await learning._require_owner(request)
    sid = int(request.match_info["sid"])
    post = await request.post()
    kind, what = post.get("kind"), post.get("what")
    s = await asyncio.to_thread(ass.get_schedule, sid)
    if not s or kind not in an.KINDS or what not in ("preview", "publish"):
        raise web.HTTPBadRequest(text="bad_request")
    if what == "publish" and post.get("confirm") != "1":
        raise _go("/admin/announce", error="Отметьте подтверждение публикации.")
    day, hm, tests = await asyncio.to_thread(_slot_tests_sync, s)
    if not tests:
        raise _go("/admin/announce", error="У расписания нет тестов для анонса.")
    bot = _new_bot()
    try:
        if what == "preview":
            res = await an.preview(bot, actor, kind, s, tests, hm, day)
            msg = "Предпросмотр отправлен вам в Telegram." if res.get("ok") else ""
        else:
            key = f"pre:{sid}:{day} {hm}" if kind == "pre" else ""
            res = await an.announce(bot, kind, s, tests, hm, day, key=key, manual=True)
            msg = ("Этот анонс уже был опубликован." if res.get("already") else "Анонс опубликован в канале.") \
                if res.get("ok") else ""
            await asyncio.to_thread(trash.audit, actor, "", "site", "announce", "", f"Анонс вручную ({an.KINDS[kind]})",
                                    sid, msg or res.get("error") or "")
    finally:
        await bot.session.close()
    raise _go("/admin/announce", message=msg) if msg else _go("/admin/announce", error=res.get("error") or "Не отправлено.")


async def retry(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    bot = _new_bot()
    try:
        res = await an.retry(bot, int(request.match_info["lid"]))
    finally:
        await bot.session.close()
    await asyncio.to_thread(trash.audit, actor, "", "site", "announce", "", "Повтор отправки", int(request.match_info["lid"]),
                            "отправлено" if res.get("ok") else res.get("error") or "")
    raise _go("/admin/announce#log", message="Отправлено.") if res.get("ok") else \
        _go("/admin/announce#log", error=res.get("error") or "Не отправлено.")


# ───────────────────────── реклама ─────────────────────────

async def ads_settings(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    await asyncio.to_thread(an.save_ads_settings, bool(post.get("enabled")), post.get("mode") or "sequence",
                            _int(post.get("per_test"), 1), _int(post.get("every_n"), 1))
    await asyncio.to_thread(trash.audit, actor, "", "site", "settings", "", "Реклама после тестов", None, "")
    raise _go("/admin/announce#ads", message="Настройки рекламы сохранены.")


async def ad_save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    kind = post.get("kind")
    if kind not in an.AD_KINDS:
        raise _go("/admin/announce#ads", error="Выберите вид рекламы.")
    ad_id = _int(post.get("id")) or None
    file_name = None
    up = post.get("file")
    if up is not None and hasattr(up, "file") and getattr(up, "filename", ""):
        ext = os.path.splitext(up.filename)[1].lower()
        if kind == "text" or ext not in AD_EXT.get(kind, ()):
            raise _go("/admin/announce#ads", error="Файл не подходит к выбранному виду рекламы.")
        blob = up.file.read()
        if len(blob) > AD_MAX:
            raise _go("/admin/announce#ads", error="Файл больше 19 МБ. Большое видео загрузите через бота.")
        file_name = f"ad{int(time.time() * 1000)}{ext}"
        (an.ads_dir() / file_name).write_bytes(blob)
    elif kind != "text" and not ad_id:
        raise _go("/admin/announce#ads", error="Прикрепите файл.")
    new_id = await asyncio.to_thread(an.save_ad, ad_id, title=post.get("title") or "", kind=kind,
                                     caption=post.get("caption") or "", button_text=post.get("button_text") or "",
                                     button_url=(post.get("button_url") or "").strip(), file_name=file_name,
                                     expires_at=post.get("expires_at") or "", enabled=bool(post.get("enabled")),
                                     actor=actor)
    await asyncio.to_thread(trash.audit, actor, "", "site", "save" if ad_id else "create", "", "Реклама после тестов",
                            new_id, post.get("title") or "")
    raise _go("/admin/announce#ads", message="Реклама сохранена.")


async def ad_action(request: web.Request) -> web.Response:
    actor, source = await learning._delete_actor(request)
    ad_id = int(request.match_info["aid"])
    post = await request.post()
    ad = await asyncio.to_thread(an.get_ad, ad_id)
    if not ad:
        raise web.HTTPNotFound(text="Реклама не найдена")
    if post.get("action") == "toggle":
        await asyncio.to_thread(an.toggle_ad, ad_id)
        raise _go("/admin/announce#ads", message="Готово.")
    if post.get("action") == "delete":
        await asyncio.to_thread(trash.move, "ad", ad["title"] or "Реклама", actor, source, [("ads", ad_id)],
                                meta={"location": "Реклама после тестов"})
        raise _go("/admin/announce#ads", message=trash.MOVED_MSG)
    if post.get("action") == "preview":
        bot = _new_bot()
        try:
            await an.send_ad(bot, actor, ad)
            msg, err = "Реклама отправлена вам в Telegram для проверки.", ""
        except Exception as e:
            msg, err = "", an._human_error(e)
        finally:
            await bot.session.close()
        raise _go("/admin/announce#ads", msg, err)
    raise web.HTTPBadRequest(text="bad_request")


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/admin/announce", page)
    r.add_post("/admin/announce/tz", tz_save)
    r.add_post("/admin/announce/sched/{sid:\\d+}", sched_save)
    r.add_post("/admin/announce/sched/{sid:\\d+}/send", sched_send)
    r.add_post("/admin/announce/templates", tpl_save)
    r.add_post("/admin/announce/templates/{tid:\\d+}/delete", tpl_delete)
    r.add_post("/admin/announce/log/{lid:\\d+}/retry", retry)
    r.add_post("/admin/announce/ads/settings", ads_settings)
    r.add_post("/admin/announce/ads", ad_save)
    r.add_post("/admin/announce/ads/{aid:\\d+}", ad_action)
