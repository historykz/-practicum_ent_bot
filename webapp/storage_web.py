"""«Память и место» в админке сайта (v99): /admin/storage."""
import asyncio

from aiohttp import web

import utils
from services import storage_info as si
from webapp import admin_hub, learning


async def page(request: web.Request) -> web.Response:
    tg_id = await learning._require_admin(request)
    rep = await asyncio.to_thread(si.report)
    return await admin_hub._render(request, "admin_storage.html", {"r": rep, "h": si.human, "owner": utils.is_owner(tg_id)})


async def clean(request: web.Request) -> web.Response:
    tg_id = await learning._require_admin(request)
    if not utils.is_owner(tg_id):
        raise admin_hub._go("/admin/storage", error="Убирать может только владелец.")
    post = await request.post()
    what = post.get("what")
    if what == "temp":
        freed = await asyncio.to_thread(si.clean_temp)
        raise admin_hub._go("/admin/storage", message=f"Временные файлы удалены: освобождено {si.human(freed)}.")
    if what == "versions":
        res = await asyncio.to_thread(si.clean_old_versions)
        raise admin_hub._go("/admin/storage", message=f"Удалено папок: {res['folders']}, освобождено {si.human(res['bytes'])}.")
    raise admin_hub._go("/admin/storage", error="Неизвестное действие.")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/storage", page)
    app.router.add_post("/admin/storage/clean", clean)
