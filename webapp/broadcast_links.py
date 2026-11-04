"""
Счётчик нажатий на кнопки глобальной рассылки.

Кнопка-ссылка на сайт ведёт на /r/<рассылка>/<кнопка>/<tg_id>/<подпись>:
здесь нажатие записывается, и человек сразу уходит по настоящей ссылке.
Куда вести, берём из базы, а не из адреса, поэтому подменить адрес
назначения нельзя. Без верной подписи нажатие не считается, но переход
всё равно работает — человек не должен упираться в ошибку.
"""
import asyncio
import hmac
import logging

from aiohttp import web

from services import broadcast_service as bs

log = logging.getLogger(__name__)


async def track_click(request: web.Request) -> web.Response:
    try:
        bid = int(request.match_info["bid"])
        idx = int(request.match_info["idx"])
        tg_id = int(request.match_info["tg"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()
    btn = await asyncio.to_thread(bs.button_at, bid, idx)
    if not btn:
        raise web.HTTPNotFound(text="Ссылка устарела")
    if hmac.compare_digest(request.match_info.get("sig", ""), bs.click_sig(bid, idx, tg_id)):
        try:
            await asyncio.to_thread(bs.record_click, bid, idx, tg_id)
        except Exception as e:
            log.warning("счётчик рассылки №%s: %s", bid, e)
    raise web.HTTPFound(btn["url"])


def register_routes(app) -> None:
    app.router.add_get(r"/r/{bid:\d+}/{idx:\d+}/{tg:\d+}/{sig:[0-9a-f]{16}}", track_click)
