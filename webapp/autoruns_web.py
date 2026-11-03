"""
Журнал автозапусков тестов — в админке сайта (/admin/autoruns).

Каждая строка — один запланированный тест: название, предмет или раздел,
группа, запланированное и фактическое время, статус, причина ошибки.
Данные те же, что в боте (services/auto_schedule_service.journal).
"""
import asyncio

import aiohttp_jinja2
from aiohttp import web

from services import auto_schedule_service as ass
from webapp import auth


async def admin_autoruns(request: web.Request) -> web.Response:
    from webapp import learning
    await learning._require_admin(request)
    sid = request.query.get("schedule")
    sid = int(sid) if sid and sid.isdigit() else None
    rows, scheds = await asyncio.to_thread(lambda: (ass.journal(300, sid), ass.list_schedules()))
    data = await auth.nav_context(request)
    data.update({"rows": rows, "schedules": scheds, "selected": sid, "display_status": ass.display_status,
                 "counts": {k: sum(1 for r in rows if r.get("status") == k)
                            for k in ("queued", "announced", "launched", "finished", "cancelled", "error", "skipped")}})
    return aiohttp_jinja2.render_template("admin_autoruns.html", request, data)


def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/autoruns", admin_autoruns)
