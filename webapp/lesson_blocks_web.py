"""
Блоки урока на сайте (текст, интерактив, картинка, видео) — правка в админке урока.

  POST /admin/learn/lessons/<урок>/blocks/add       добавить блок в конец
  POST /admin/learn/lessons/<урок>/blocks/reorder   новый порядок (перетаскивание)
  POST /admin/learn/blocks/<блок>/save|delete|move|transfer
  GET  /uploads/blocks/<файл>                       картинки блоков (только вошедшим)

Показ ученику — в learning.learn_lesson (шаблон learn_lesson.html), только когда
урок ему открыт: те же правила оплаты и доступа, что у всего урока.
"""
import asyncio
from urllib.parse import quote

from aiohttp import web

import utils
from services import lesson_blocks as lb
from webapp import learning


def _back(lesson_id, message: str = "", block_id=None) -> web.HTTPFound:
    url = f"/admin/learn/lessons/{lesson_id}/edit"
    if message:
        url += "?message=" + quote(message)
    return web.HTTPFound(url + (f"#block{block_id}" if block_id else "#blocks"))


async def _admin(request: web.Request) -> int:
    return await learning._require_admin(request)


async def add_block(request: web.Request) -> web.Response:
    tg_id = await _admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    if not await asyncio.to_thread(lb.lesson_exists, lesson_id):
        raise web.HTTPNotFound(text="Урок не найден")
    post = await request.post()
    kind = post.get("kind") or ""
    title = post.get("title") or ""
    if kind == "text":
        bid, err = await asyncio.to_thread(lb.add_text, lesson_id, title, post.get("body") or "")
    elif kind == "interactive":
        if not await asyncio.to_thread(utils.can_manage_interactive, tg_id):
            raise _back(lesson_id, "Нет права работать с интерактивами: его выдаёт владелец.")
        try:
            task_id = int(post.get("task_id") or 0)
        except ValueError:
            task_id = 0
        bid, err = await asyncio.to_thread(lb.add_interactive, lesson_id, task_id, title)
    elif kind == "image":
        f = post.get("image")
        if f is None or not hasattr(f, "file"):
            bid, err = None, "Выберите картинку."
        else:
            bid, err = await asyncio.to_thread(lb.add_image, lesson_id, f.filename, f.file.read(), title,
                                               post.get("body") or "")
    elif kind == "video":
        bid, err = await asyncio.to_thread(lb.add_video, lesson_id, post.get("video_url") or "", title)
    else:
        bid, err = None, "Неизвестный тип блока."
    raise _back(lesson_id, err or "Блок добавлен в конец урока", bid)


async def save_block(request: web.Request) -> web.Response:
    await _admin(request)
    block_id = int(request.match_info["block_id"])
    post = await request.post()
    b = await asyncio.to_thread(lb.get_block, block_id)
    if not b:
        raise web.HTTPNotFound(text="Блок не найден")
    err = await asyncio.to_thread(lb.update_block, block_id, post.get("title") or "", post.get("body") or "")
    raise _back(post.get("lesson_id") or b["lesson_id"], err or "Блок сохранён", block_id)


async def delete_block(request: web.Request) -> web.Response:
    await _admin(request)
    post = await request.post()
    lesson_id = await asyncio.to_thread(lb.delete_block, int(request.match_info["block_id"]))
    if lesson_id is None:
        raise web.HTTPNotFound(text="Блок не найден")
    raise _back(post.get("lesson_id") or lesson_id, "Блок удалён из урока (само задание осталось в библиотеке)")


async def move_block(request: web.Request) -> web.Response:
    await _admin(request)
    post = await request.post()
    block_id = int(request.match_info["block_id"])
    lesson_id = await asyncio.to_thread(lb.move_block, block_id, "up" if post.get("dir") == "up" else "down")
    if lesson_id is None:
        raise web.HTTPNotFound(text="Блок не найден")
    raise _back(post.get("lesson_id") or lesson_id, "", block_id)


async def transfer_block(request: web.Request) -> web.Response:
    await _admin(request)
    post = await request.post()
    block_id = int(request.match_info["block_id"])
    b = await asyncio.to_thread(lb.get_block, block_id)
    if not b:
        raise web.HTTPNotFound(text="Блок не найден")
    try:
        target = int(post.get("target_lesson_id") or 0)
    except ValueError:
        target = 0
    copy = post.get("mode") == "copy"
    new_id, err = await asyncio.to_thread(lb.transfer, block_id, target, copy)
    if err:
        raise _back(post.get("lesson_id") or b["lesson_id"], err, block_id)
    raise _back(post.get("lesson_id") or b["lesson_id"],
                "Блок скопирован в другой урок" if copy else "Блок перенесён в другой урок")


async def reorder(request: web.Request) -> web.Response:
    await _admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    try:
        body = await request.json()
        order = body.get("order") or []
    except Exception:
        raise web.HTTPBadRequest(text="bad_request")
    if not isinstance(order, list):
        raise web.HTTPBadRequest(text="bad_request")
    await asyncio.to_thread(lb.reorder, lesson_id, order)
    return web.json_response({"ok": True})


async def block_image(request: web.Request) -> web.Response:
    await learning._require_login(request)
    name = request.match_info["name"]
    if "/" in name or ".." in name or "\\" in name:
        raise web.HTTPBadRequest()
    path = lb.images_dir() / name
    if not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_post("/admin/learn/lessons/{lesson_id:\\d+}/blocks/add", add_block)
    r.add_post("/admin/learn/lessons/{lesson_id:\\d+}/blocks/reorder", reorder)
    r.add_post("/admin/learn/blocks/{block_id:\\d+}/save", save_block)
    r.add_post("/admin/learn/blocks/{block_id:\\d+}/delete", delete_block)
    r.add_post("/admin/learn/blocks/{block_id:\\d+}/move", move_block)
    r.add_post("/admin/learn/blocks/{block_id:\\d+}/transfer", transfer_block)
    r.add_get("/uploads/blocks/{name}", block_image)
