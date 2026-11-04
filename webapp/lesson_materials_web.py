"""Материалы урока (v97): конспекты и видео-анимации кодом (ZIP или HTML), специальные
ссылки на урок и защищённое видео.

Конспект и видео кодом — это те же ZIP-пакеты, что у интерактивных заданий
(services/interactive_pkg.py: проверка архива, изолированное окно, версии, замена),
только с пометкой material. Загруженный материал сразу встаёт блоком в урок.
"""
import asyncio
import hashlib
import hmac
import io
import logging
import time
import zipfile
from urllib.parse import quote

from aiohttp import web

import database as db
import utils
from services import interactive as it
from services import interactive_pkg as pkg
from services import lesson_blocks as lb
from services import lesson_links as ll
from services import trash
from webapp import auth, learning

log = logging.getLogger(__name__)
VIDEO_TOKEN_TTL = 3 * 3600


def _back(lesson_id: int, message: str = "", error: str = "", anchor: str = "materials") -> web.HTTPFound:
    q = ("message=" + quote(message)) if message else ("error=" + quote(error) if error else "")
    return web.HTTPFound(f"/admin/learn/lessons/{lesson_id}/edit" + ("?" + q if q else "") + f"#{anchor}")


async def _require_ia(request: web.Request) -> int:
    from webapp import interactive_web
    return await interactive_web._require_ia(request)


def _wrap_html(name: str, blob: bytes) -> bytes:
    """Один HTML-файл от ИИ — тот же пакет, что ZIP с одной страницей."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", blob)
    return buf.getvalue()


def _attach_sync(lesson_id: int, blob: bytes, file_name: str, material: str, publish: bool, actor: int) -> dict:
    if not lb.lesson_exists(lesson_id):
        return {"error": "Урок не найден."}
    res = pkg.import_archive(blob, file_name, actor)
    ids = res.get("task_ids") or []
    if not ids:
        why = [p for t in (res.get("report") or {}).get("tasks") or [] for p in (t.get("problems") or [])][:3]
        return {"error": (res.get("error") or "Архив не прошёл проверку.") + (" " + "; ".join(why) if why else ""),
                "report": res.get("report")}
    published, problems = 0, []
    from webapp import interactive_web as iw
    for tid in ids:
        # Вид материала, выбранный в уроке, главнее отметки в архиве
        db.execute("UPDATE interactive_tasks SET material=?, access='lesson', visibility='lessons' WHERE id=?",
                   (material, tid))
        if publish:
            ok, probs = iw._publish_sync(tid)
            published += 1 if ok else 0
            if not ok:
                problems += probs[:2]
    lb.add_tasks(lesson_id, ids)
    trash.audit(actor, "", "site", "create", "", f"{it.MATERIALS[material]} в урок", lesson_id, f"{len(ids)} шт.")
    return {"ids": ids, "published": published, "problems": problems, "warnings": (res.get("report") or {}).get("warnings") or []}


async def attach_code(request: web.Request) -> web.Response:
    """Загрузить конспект или видео-анимацию кодом прямо в урок."""
    actor = await _require_ia(request)
    lesson_id = int(request.match_info["lesson_id"])
    post = await request.post()
    material = post.get("material") if post.get("material") in ("conspect", "video", "task") else "conspect"
    up = post.get("file")
    if up is None or not hasattr(up, "file") or not getattr(up, "filename", ""):
        raise _back(lesson_id, error="Выберите ZIP-архив или HTML-файл.")
    blob = up.file.read(pkg.ZIP_MAX + 1)
    name = up.filename
    if name.lower().endswith((".html", ".htm")):
        if len(blob) > pkg.ENTRY_MAX:
            raise _back(lesson_id, error="HTML-файл слишком большой.")
        blob, name = _wrap_html(name, blob), name.rsplit(".", 1)[0] + ".zip"
    res = await asyncio.to_thread(_attach_sync, lesson_id, blob, name, material, bool(post.get("publish")), actor)
    if res.get("error"):
        raise _back(lesson_id, error=res["error"])
    msg = f"Добавлено в урок: {len(res['ids'])}."
    if post.get("publish"):
        msg += f" Опубликовано: {res['published']}." + (" Не опубликовано: " + "; ".join(res["problems"]) if res["problems"] else "")
    else:
        msg += " Это черновики: ученики их не видят, пока вы не нажмёте «Опубликовать» в редакторе."
    raise _back(lesson_id, message=msg)


async def template_zip(request: web.Request) -> web.Response:
    await _require_ia(request)
    from services import conspect_template as ct
    blob = await asyncio.to_thread(ct.build_zip)
    return web.Response(body=blob, content_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="smartent-konspekt-template.zip"', "Cache-Control": "no-store"})


# ───────────────────────── ссылки на урок ─────────────────────────

async def link_create(request: web.Request) -> web.Response:
    actor = await learning._require_admin(request)
    lesson_id = int(request.match_info["lesson_id"])
    post = await request.post()
    res = await asyncio.to_thread(
        ll.create, lesson_id, post.get("kind") or "general", actor, label=post.get("label") or "",
        people=post.get("people") or "", grant=bool(post.get("grant")), access_days=int(post.get("access_days") or 0),
        max_uses=int(post.get("max_uses") or 0), expires=post.get("expires") or "")
    if res.get("error"):
        raise _back(lesson_id, error=res["error"], anchor="links")
    await asyncio.to_thread(trash.audit, actor, "", "site", "create", "", "Ссылка на урок", lesson_id, res["code"])
    raise _back(lesson_id, message="Ссылка создана.", anchor="links")


async def link_revoke(request: web.Request) -> web.Response:
    actor = await learning._require_admin(request)
    link_id = int(request.match_info["link_id"])
    row = await asyncio.to_thread(db.fetchone, "SELECT lesson_id FROM lesson_links WHERE id=?", (link_id,))
    if not row:
        raise web.HTTPNotFound(text="Ссылка не найдена")
    await asyncio.to_thread(ll.revoke, link_id)
    await asyncio.to_thread(trash.audit, actor, "", "site", "delete", "", "Ссылка на урок отозвана", row["lesson_id"], "")
    raise _back(row["lesson_id"], message="Ссылка отозвана. Уже выданный по ней доступ остаётся; снять его можно в профиле ученика.",
                anchor="links")


def links_ctx_sync(lesson_id: int) -> dict:
    from webapp import shortcuts as sc
    real = sc.orig_lesson_id(lesson_id)
    direct_app = ll.app_url("x").replace("startapp=l_x", f"startapp=lesson_{lesson_id}")
    from services.broadcast_service import site_url
    return {"lesson_links": ll.for_lesson(lesson_id), "link_kinds": ll.KINDS,
            "direct_site": f"{site_url()}/learn/lesson/{lesson_id}", "direct_app": direct_app,
            "link_access_n": db.fetchone("SELECT COUNT(*) AS c FROM lesson_access WHERE lesson_id=? AND source='link'",
                                         (real,))["c"]}


# ───────────────────────── защищённое видео ─────────────────────────

def _secret() -> bytes:
    from services import workbook_mark_service as wm
    return wm._secret()


def video_token(lesson_id: int, tg_id: int) -> str:
    exp = int(time.time()) + VIDEO_TOKEN_TTL
    sig = hmac.new(_secret(), f"video:{int(lesson_id)}:{int(tg_id)}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def _token_ok(lesson_id: int, tg_id: int, token: str) -> bool:
    try:
        exp_s, sig = (token or "").split(".")
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    good = hmac.new(_secret(), f"video:{int(lesson_id)}:{int(tg_id)}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig)


async def lesson_video(request: web.Request) -> web.Response:
    """Видеофайл урока. Только вошедшему, только с открытым уроком и только по короткой ссылке
    своей страницы: прямого адреса файла у ученика нет, переслать ссылку другому бесполезно."""
    tg_id = await auth.get_logged_in_tg_id(request)
    lesson_id = int(request.match_info["lesson_id"])
    # Гость смотрит видео бесплатного урока по ссылке со страницы (подпись без Telegram ID)
    if not _token_ok(lesson_id, tg_id or 0, request.query.get("t") or ""):
        raise web.HTTPForbidden(text="Ссылка на видео устарела. Обновите страницу урока.")
    from webapp import interactive_web as iw
    if not (tg_id and utils.is_site_admin(tg_id)) and not await asyncio.to_thread(iw._lesson_open_sync, tg_id, lesson_id):
        raise web.HTTPForbidden(text="Нет доступа к уроку.")
    from webapp import shortcuts as sc
    row = await asyncio.to_thread(db.fetchone, "SELECT video_url FROM lessons WHERE id=?", (sc.orig_lesson_id(lesson_id),))
    url = (row["video_url"] if row else "") or ""
    if not url.startswith("/uploads/videos/"):
        raise web.HTTPNotFound()
    from webapp import video
    name = url.rsplit("/", 1)[-1]
    path = video.upload_dir() / name
    if "/" in name or ".." in name or not path.exists():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Content-Disposition"] = "inline"
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_post("/admin/learn/lessons/{lesson_id:\\d+}/code-material", attach_code)
    r.add_get("/admin/learn/conspect-template.zip", template_zip)
    r.add_post("/admin/learn/lessons/{lesson_id:\\d+}/links", link_create)
    r.add_post("/admin/learn/links/{link_id:\\d+}/revoke", link_revoke)
    r.add_get("/learn/lesson/{lesson_id:\\d+}/video", lesson_video)
