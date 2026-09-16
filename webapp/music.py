"""
Веб-часть фоновой музыки: админка плейлиста, выдача плейлиста плееру и
раздача самих аудиофайлов.
"""
import asyncio

import aiohttp_jinja2
from aiohttp import web

from services import music_service as ms
from webapp import auth


async def _require_admin(request):
    from webapp import learning
    return await learning._require_admin(request)


def _back(msg: str):
    from urllib.parse import quote
    return web.HTTPFound(f"/admin/music?msg={quote(msg)}")


# ---------- Админка ----------

async def admin_music_page(request: web.Request) -> web.Response:
    await _require_admin(request)
    ctx = await auth.nav_context(request)
    ctx["tracks"] = await asyncio.to_thread(ms.all_tracks)
    ctx["music_enabled"] = await asyncio.to_thread(ms.is_enabled)
    ctx["max_mb"] = ms.MAX_TRACK_BYTES // 1024 // 1024
    ctx["msg"] = request.query.get("msg", "")
    return aiohttp_jinja2.render_template("admin_music.html", request, ctx)


async def admin_music_upload(request: web.Request) -> web.Response:
    admin_id = await _require_admin(request)
    if not (request.content_type or "").startswith("multipart/"):
        raise _back("Файл не получен — попробуйте ещё раз.")
    title = source = ""
    rights_ok = False
    payload = None
    fname = "track.mp3"
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "track" and part.filename:
            fname = part.filename
            payload = await part.read()
        elif part.name == "title":
            title = (await part.text()).strip()
        elif part.name == "source":
            source = (await part.text()).strip()
        elif part.name == "rights_ok":
            rights_ok = (await part.text()).strip() == "on"

    if not rights_ok:
        raise _back("Подтвердите, что трек разрешён к использованию.")
    if not payload:
        raise _back("Файл не выбран.")
    ok, msg = await asyncio.to_thread(
        ms.add_track, payload, fname, title, source, admin_id)
    raise _back(msg)


async def admin_music_action(request: web.Request) -> web.Response:
    await _require_admin(request)
    action = request.match_info["action"]
    data = await request.post()

    if action == "toggle-all":
        cur = await asyncio.to_thread(ms.is_enabled)
        await asyncio.to_thread(ms.set_enabled, not cur)
        raise _back("Фоновая музыка включена." if not cur
                    else "Фоновая музыка выключена — у учеников плеера не будет.")

    track_id = int(data.get("track_id") or 0)
    if not track_id:
        raise _back("Трек не выбран.")
    if action == "delete":
        await asyncio.to_thread(ms.delete_track, track_id)
        raise _back("Трек удалён.")
    if action == "toggle":
        await asyncio.to_thread(ms.toggle_track, track_id)
        raise _back("Готово.")
    if action == "move":
        await asyncio.to_thread(ms.move_track, track_id,
                                data.get("direction") or "up")
        raise _back("Порядок изменён.")
    if action == "rename":
        await asyncio.to_thread(ms.rename_track, track_id,
                                data.get("title") or "", data.get("source") or "")
        raise _back("Сохранено.")
    raise _back("Неизвестное действие.")


# ---------- Плеер ученика ----------

async def api_playlist(request: web.Request) -> web.Response:
    """Плейлист для страницы теста. Гостю тоже отдаём — музыка не платная."""
    tracks = await asyncio.to_thread(ms.playlist)
    return web.json_response({"enabled": bool(tracks), "tracks": tracks})


async def uploaded_music(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if "/" in filename or ".." in filename:
        raise web.HTTPBadRequest()
    path = ms.upload_dir() / filename
    if not path.exists():
        raise web.HTTPNotFound()
    # FileResponse сам отвечает на Range-запросы — это нужно, чтобы браузер
    # мог перематывать трек и не тянул файл целиком перед стартом.
    resp = web.FileResponse(path)
    resp.headers["Content-Type"] = ms.mime_for(filename)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


def register_routes(app: web.Application) -> None:
    app.router.add_get("/admin/music", admin_music_page)
    app.router.add_post("/admin/music/upload", admin_music_upload)
    app.router.add_post("/admin/music/{action}", admin_music_action)
    app.router.add_get("/api/music/playlist", api_playlist)
    app.router.add_get("/uploads/music/{filename}", uploaded_music)


setup_routes = register_routes   # синоним под общий стиль проекта
