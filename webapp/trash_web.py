"""Страница «Корзина» и журнал удалений (v92). Логика — в services/trash.py, общая с ботом."""
import asyncio
import logging
from urllib.parse import quote, urlencode

import aiohttp_jinja2
from aiohttp import web

import utils
from services import trash as tr
from webapp import auth, learning

log = logging.getLogger(__name__)
PURGE_EVERY_SECONDS = 600
CONFIRM_WORD = "УДАЛИТЬ"


async def _perm(request: web.Request, need: str = "trash_view") -> tuple:
    tg_id = await learning._require_admin(request)
    p = await asyncio.to_thread(tr.perms, tg_id)
    if not p.get(need):
        raise web.HTTPForbidden(text="Нет прав для этого действия в корзине.")
    return tg_id, p


def _to(message: str = "", error: str = "", query: str = "") -> web.HTTPFound:
    q = []
    if message:
        q.append("message=" + quote(message))
    if error:
        q.append("error=" + quote(error))
    return web.HTTPFound("/admin/trash" + ("?" + "&".join(q) if q else "") + (("&" if q else "?") + query if query else ""))


def _ids(values) -> list:
    out = []
    for v in values:
        if str(v).isdigit():
            out.append(int(v))
    return sorted(set(out))


async def trash_page(request: web.Request) -> web.Response:
    tg_id, p = await _perm(request)
    q = request.query
    flt = {k: (q.get(k) or "").strip() for k in ("q", "kind", "subject", "who", "day")}
    items = await asyncio.to_thread(tr.list_items, tg_id, **flt)
    ctx = await auth.nav_context(request)
    ctx.update(await asyncio.to_thread(tr.filters_ctx))
    ctx.update({"items": items, "flt": flt, "perms": p, "message": q.get("message"), "error": q.get("error"),
                "report_ok": q.get("ok"), "report_err": request.get("report_err"), "tg_id": tg_id,
                "total": await asyncio.to_thread(tr.count_trashed, tg_id), "tab": "trash"})
    return aiohttp_jinja2.render_template("admin_trash.html", request, ctx)


def _restore_one_sync(item_id: int, tg_id: int, p: dict, new_parent, source: str) -> dict:
    item = tr._load(item_id)
    if not item or not tr.may_restore(item, tg_id, p):
        raise web.HTTPForbidden(text="Нет права восстановить этот объект.")
    return tr.restore(item_id, tg_id, new_parent=new_parent, source=source)


async def restore_one(request: web.Request) -> web.Response:
    tg_id, p = await _perm(request)
    item_id = int(request.match_info["item_id"])
    post = await request.post()
    np = post.get("new_parent")
    new_parent = int(np) if str(np or "").isdigit() else None
    source = (await learning._delete_source(request))
    try:
        res = await asyncio.to_thread(_restore_one_sync, item_id, tg_id, p, new_parent, source)
    except tr.RestoreError as e:
        if e.code == "parent_missing":
            raise web.HTTPFound(f"/admin/trash/{item_id}/place")
        raise _to(error=str(e))
    msg = "Этот объект уже восстановлен." if res.get("already") else f"Восстановлено: {res.get('title')}."
    if res.get("parents"):
        msg += " Вместе с ним вернулось то, где он лежал: " + ", ".join(x for x in res["parents"] if x) + "."
    if res.get("conflicts"):
        msg += " Внимание: " + res["conflicts"][0] + ", поэтому старое содержимое не поставлено поверх."
    raise _to(message=msg)


async def place_page(request: web.Request) -> web.Response:
    """Прежнее место удалено навсегда: админ сам выбирает новое, произвольно не кладём."""
    tg_id, p = await _perm(request)
    item_id = int(request.match_info["item_id"])
    item = await asyncio.to_thread(tr._load, item_id)
    if not item or item["status"] != "trashed" or not tr.may_restore(item, tg_id, p):
        raise web.HTTPNotFound(text="Запись корзины не найдена.")
    places = await asyncio.to_thread(tr.places_for, item.get("root_table") or "")
    ctx = await auth.nav_context(request)
    ctx.update({"item": item, "places": places, "kind_title": tr.KINDS.get(item["kind"], item["kind"])})
    return aiohttp_jinja2.render_template("admin_trash_place.html", request, ctx)


def _visible_sync(ids: list, tg_id: int, p: dict) -> tuple:
    ok, bad = [], []
    for i in ids:
        it = tr._load(i)
        if not it or it["status"] != "trashed":
            continue
        (ok if tr.may_restore(it, tg_id, p) else bad).append(it)
    return ok, bad


async def _report(request: web.Request, tg_id: int, res: dict, denied: list) -> web.Response:
    ctx = await auth.nav_context(request)
    errs = list(res.get("errors") or []) + [{"title": d["title"], "error": "Нет права восстановить этот объект."} for d in denied]
    ctx.update({"ok_list": res.get("ok") or [], "errors": errs, "already": res.get("already") or 0})
    return aiohttp_jinja2.render_template("admin_trash_report.html", request, ctx)


async def restore_selected(request: web.Request) -> web.Response:
    tg_id, p = await _perm(request)
    post = await request.post()
    ids = _ids(post.getall("ids", []))
    if not ids:
        raise _to(error="Ничего не выбрано.")
    if len(ids) > 1 and not p.get("trash_mass"):
        raise web.HTTPForbidden(text="Нет права на массовое восстановление.")
    ok, bad = await asyncio.to_thread(_visible_sync, ids, tg_id, p)
    source = await learning._delete_source(request)
    res = await asyncio.to_thread(tr.restore_many, [i["id"] for i in ok], tg_id, source)
    return await _report(request, tg_id, res, bad)


async def restore_all(request: web.Request) -> web.Response:
    tg_id, p = await _perm(request, "trash_mass")
    items = await asyncio.to_thread(tr.list_items, tg_id, limit=5000)
    ok, bad = await asyncio.to_thread(_visible_sync, [i["id"] for i in items], tg_id, p)
    source = await learning._delete_source(request)
    res = await asyncio.to_thread(tr.restore_many, [i["id"] for i in ok], tg_id, source)
    return await _report(request, tg_id, res, bad)


async def purge_confirm(request: web.Request) -> web.Response:
    """Отдельный шаг: показать, что будет стёрто, и попросить ввести слово."""
    tg_id, p = await _perm(request, "trash_purge")
    post = await request.post()
    ids = _ids(post.getall("ids", []))
    if not ids:
        raise _to(error="Ничего не выбрано.")
    items = [i for i in await asyncio.to_thread(tr.list_items, tg_id, limit=5000) if i["id"] in ids]
    ctx = await auth.nav_context(request)
    ctx.update({"items": items, "word": CONFIRM_WORD})
    return aiohttp_jinja2.render_template("admin_trash_purge.html", request, ctx)


async def purge_do(request: web.Request) -> web.Response:
    tg_id, p = await _perm(request, "trash_purge")
    post = await request.post()
    if (post.get("word") or "").strip() != CONFIRM_WORD:
        raise _to(error=f"Не удалено: нужно было ввести слово {CONFIRM_WORD}.")
    allowed = {i["id"] for i in await asyncio.to_thread(tr.list_items, tg_id, limit=5000)}
    ids = [i for i in _ids(post.getall("ids", [])) if i in allowed]
    source = await learning._delete_source(request)
    n = await asyncio.to_thread(tr.purge_many, ids, tg_id, source)
    raise _to(message=f"Удалено навсегда: {n}.")


async def journal_page(request: web.Request) -> web.Response:
    tg_id, p = await _perm(request, "trash_history")
    rows = await asyncio.to_thread(tr.journal, 500)
    ctx = await auth.nav_context(request)
    ctx.update({"rows": rows})
    return aiohttp_jinja2.render_template("admin_trash_journal.html", request, ctx)


async def _purge_loop() -> None:
    """Каждый объект живёт в корзине свои 48 часов. Первый проход — сразу при запуске,
    чтобы всё просроченное за время простоя сервера тоже было стёрто."""
    while True:
        try:
            n = await asyncio.to_thread(tr.purge_expired)
            if n:
                log.info("корзина: автоочистка удалила %s", n)
        except Exception as e:
            log.warning("корзина: автоочистка: %s", e)
        await asyncio.sleep(PURGE_EVERY_SECONDS)


async def start_purge_loop(app: web.Application) -> None:
    app["trash_purge_task"] = asyncio.create_task(_purge_loop())


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/admin/trash", trash_page)
    r.add_get("/admin/trash/journal", journal_page)
    r.add_post("/admin/trash/restore-selected", restore_selected)
    r.add_post("/admin/trash/restore-all", restore_all)
    r.add_post("/admin/trash/purge-confirm", purge_confirm)
    r.add_post("/admin/trash/purge", purge_do)
    r.add_post("/admin/trash/{item_id:\\d+}/restore", restore_one)
    r.add_get("/admin/trash/{item_id:\\d+}/place", place_page)
