"""
Интерактивные задания: библиотека, загрузка ZIP от внешних ИИ, «Создать кодом».

  /admin/interactive                       библиотека: все задания, загрузка ZIP, шаблон для ИИ, наборы
  /admin/interactive/template.zip          шаблон «для создания интерактивных заданий через ИИ»
  /admin/interactive/upload                загрузить ZIP (одно или много заданий)
  /admin/interactive/package/<id>          что нашлось в архиве: предпросмотр, публикация, вставка в урок
  /admin/interactive/<id>                  задание: код (созданное кодом) или файлы и версии (из ZIP)
  /interactive, /interactive/<id>          задания для учеников
  /interactive/frame/<id>[?lesson=]        документ задания (кладётся в изолированный фрейм)
  /interactive/files/<ключ>/<путь>         файлы задания из ZIP
  /interactive/asset/<файл>                картинки заданий, созданных кодом
  /interactive/api/<id>/...[?lesson=]      прогресс, ответ на проверку, тренировочный результат

Код задания выполняется только во фрейме с sandbox и строгой политикой
(services/interactive.py, services/interactive_pkg.py). Водяной знак рисует платформа
поверх фрейма, его нет в коде задания и оттуда его не убрать.

Право работать с интерактивами: владелец и сайт-админы, у которых владелец не снял
галочку «🎮 интерактивы» (utils.can_manage_interactive).
"""
import asyncio
import json
import logging
import time
from urllib.parse import quote

import aiohttp_jinja2
from aiohttp import web

import utils
from services import interactive as it
from services import interactive_pkg as pkg
from services import lesson_blocks as lb
from webapp import auth, learning

logger = logging.getLogger(__name__)

FRAME_SANDBOX = "allow-scripts allow-popups allow-modals allow-pointer-lock"


def _origin(request: web.Request) -> str:
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    return f"{proto}://{request.host}"


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad_request")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad_request")
    return body


async def _user(request: web.Request):
    tg_id = await learning._require_login(request)
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    if not user:
        raise web.HTTPFound("/?error=login_required")
    return tg_id, user


async def _require_ia(request: web.Request) -> int:
    """Админ сайта с правом на интерактивы."""
    tg_id = await learning._require_admin(request)
    if not await asyncio.to_thread(utils.can_manage_interactive, tg_id):
        raise web.HTTPForbidden(text="Нет права работать с интерактивными заданиями. Его выдаёт владелец.")
    return tg_id


def _back(task_id: int, message: str = "", anchor: str = "") -> web.HTTPFound:
    url = f"/admin/interactive/{task_id}"
    if message:
        url += "?message=" + quote(message)
    if anchor:
        url += "#" + anchor
    return web.HTTPFound(url)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def _read_upload(request: web.Request, field: str, limit: int):
    """Файл из формы потоком, с собственным пределом размера (общий предел сайта 20 МБ
    для архивов с картинками и звуком мал). Возвращает (имя, байты, поля, ошибка)."""
    fields, name, blob = {}, "", None
    try:
        reader = await request.multipart()
    except Exception:
        return "", None, fields, "Нужна форма с файлом."
    async for part in reader:
        if part.name == field and part.filename:
            name = part.filename
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(1 << 16)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > limit:
                    return name, None, fields, f"Файл больше {limit // (1024 * 1024)} МБ."
            blob = bytes(buf)
        elif part.name:
            fields[part.name] = (await part.read(decode=True)).decode("utf-8", "replace")[:5000]
    if blob is None:
        return name, None, fields, "Файл не выбран."
    return name, blob, fields, ""


# ───────────────────────── библиотека ─────────────────────────

def _library_sync(q: str, subject: str, status: str, source: str) -> dict:
    tasks = it.list_all()
    subjects = sorted({t["subject"] for t in tasks if (t.get("subject") or "").strip()})
    ql = (q or "").strip().casefold()

    def keep(t):
        if ql and ql not in " ".join(str(t.get(k) or "") for k in ("title", "topic", "description", "manifest_id")).casefold():
            return False
        if subject and (t.get("subject") or "") != subject:
            return False
        if status and t.get("status") != status:
            return False
        if source and (t.get("source") or "code") != source:
            return False
        return True
    shown = [t for t in tasks if keep(t)]
    return {"tasks": shown, "total": len(tasks), "subjects": subjects, "packages": pkg.list_packages(15),
            "lessons": lb.lesson_choices()}


async def admin_index(request: web.Request) -> web.Response:
    await _require_ia(request)
    f = {k: (request.query.get(k) or "").strip() for k in ("q", "subject", "status", "source")}
    ctx = await auth.nav_context(request)
    ctx.update(await asyncio.to_thread(_library_sync, f["q"], f["subject"], f["status"], f["source"]))
    ctx["f"] = f
    ctx["message"] = request.query.get("message")
    ctx["error"] = request.query.get("error")
    ctx["zip_mb"] = pkg.ZIP_MAX // (1024 * 1024)
    return aiohttp_jinja2.render_template("admin_interactive.html", request, ctx)


async def template_zip(request: web.Request) -> web.Response:
    await _require_ia(request)
    from services import interactive_template as tpl
    blob = await asyncio.to_thread(tpl.build_zip)
    name = f"smartent-ai-template-v{pkg.FORMAT_VERSION}.zip"
    return web.Response(body=blob, content_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def admin_upload(request: web.Request) -> web.Response:
    tg_id = await _require_ia(request)
    name, blob, _fields, err = await _read_upload(request, "file", pkg.ZIP_MAX)
    if err:
        raise web.HTTPFound("/admin/interactive?error=" + quote(err))
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    res = await asyncio.to_thread(pkg.import_archive, blob, name, (user or {}).get("id") or 0)
    if not res["package_id"]:
        raise web.HTTPFound("/admin/interactive?error=" + quote(res["error"] or "Архив не принят."))
    raise web.HTTPFound(f"/admin/interactive/package/{res['package_id']}")


async def admin_package(request: web.Request) -> web.Response:
    await _require_ia(request)
    p = await asyncio.to_thread(pkg.get_package, int(request.match_info["package_id"]))
    if not p:
        raise web.HTTPNotFound(text="Загрузка не найдена")
    ctx = await auth.nav_context(request)
    ctx["p"] = p
    ctx["lessons"] = await asyncio.to_thread(lb.lesson_choices)
    ctx["message"] = request.query.get("message")
    ctx["error"] = request.query.get("error")
    return aiohttp_jinja2.render_template("admin_interactive_package.html", request, ctx)


def _bulk_sync(action: str, ids: list, lesson_id: int) -> str:
    ids = [i for i in ids if it.get(i)]
    if not ids:
        return "Не выбрано ни одного задания."
    if action == "publish":
        ok, bad = 0, []
        for i in ids:
            done, problems = _publish_sync(i)
            if done:
                ok += 1
            else:
                bad.append(f"№{i}: " + " ".join(problems))
        return f"Опубликовано: {ok}." + (" Не опубликованы: " + "; ".join(bad) if bad else "")
    if action == "unpublish":
        for i in ids:
            it.unpublish(i)
        return f"Снято с публикации: {len(ids)}."
    if action == "lesson":
        if not lesson_id or not lb.lesson_exists(lesson_id):
            return "Выберите урок."
        n = lb.add_tasks(lesson_id, ids)
        return f"Добавлено в урок: {n}. Порядок блоков меняется в редакторе урока."
    if action == "delete":
        for i in ids:
            it.delete(i)
        return f"Удалено: {len(ids)}."
    if action in ("vis_all", "vis_lessons"):
        for i in ids:
            from database import execute
            execute("UPDATE interactive_tasks SET visibility=? WHERE id=?", ("all" if action == "vis_all" else "lessons", i))
        return f"Видимость изменена: {len(ids)}."
    return "Неизвестное действие."


async def admin_bulk(request: web.Request) -> web.Response:
    await _require_ia(request)
    post = await request.post()
    ids = [_int(x) for x in post.getall("task_id", []) if _int(x)]
    msg = await asyncio.to_thread(_bulk_sync, post.get("action") or "", ids, _int(post.get("lesson_id")))
    back = post.get("next") or "/admin/interactive"
    if not back.startswith("/admin/interactive"):
        back = "/admin/interactive"
    raise web.HTTPFound(back + ("&" if "?" in back else "?") + "message=" + quote(msg))


# ───────────────────────── одно задание ─────────────────────────

def _edit_ctx_sync(task: dict) -> dict:
    out = {"lessons_used": lb.lessons_of_task(task["id"]), "lessons": lb.lesson_choices(),
           "problems": it.publish_problems(task) if task.get("source") != "zip" else _zip_problems(task)}
    if task.get("source") == "zip":
        out["files"] = pkg.list_files(task.get("pending_files_key") or task.get("files_key"))
        out["files_where"] = "pending" if task.get("pending_files_key") else "current"
        out["size_mb"] = round(pkg.package_size(task.get("files_key")) / 1024 / 1024, 2)
        out["has_prev"] = bool(task.get("prev_files_key"))
        pkg_row = pkg.get_package(task["package_id"]) if task.get("package_id") else None
        out["package"] = {"id": pkg_row["id"], "file_name": pkg_row["file_name"]} if pkg_row else None
    return out


def _zip_problems(task: dict) -> list:
    problems = []
    if not (task.get("title") or "").strip():
        problems.append("Нет названия.")
    if not task.get("files_key") or pkg.read_entry(task["files_key"], task.get("entry") or "index.html") is None:
        problems.append("Нет страницы входа задания: замените архив.")
    if task.get("grading") == it.SERVER:
        _k, err = it.parse_key(task.get("answer_key") or "")
        if err:
            problems.append("Ключ ответа: " + err)
    return problems


async def admin_edit(request: web.Request) -> web.Response:
    await _require_ia(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    if not task:
        raise web.HTTPNotFound(text="Задание не найдено")
    ctx = await auth.nav_context(request)
    ctx["task"] = task
    ctx["sandbox"] = FRAME_SANDBOX
    ctx["message"] = request.query.get("message")
    ctx.update(await asyncio.to_thread(_edit_ctx_sync, task))
    if task.get("source") == "zip":
        ctx["task_json"] = json.dumps({k: task.get(k) for k in (
            "id", "title", "status", "pending_files_key", "files_key", "entry")}, ensure_ascii=False).replace("</", "<\\/")
        return aiohttp_jinja2.render_template("admin_interactive_zip.html", request, ctx)
    ctx["task_json"] = json.dumps({k: task.get(k) for k in (
        "id", "title", "description", "subject", "topic", "html", "css", "js", "full_doc", "use_full_doc",
        "grading", "answer_key", "max_score", "allow_retry", "status", "visibility", "max_attempts",
        "frame_height")}, ensure_ascii=False).replace("</", "<\\/")
    ctx["assets"] = await asyncio.to_thread(it.assets_of, task["id"])
    return aiohttp_jinja2.render_template("admin_interactive_edit.html", request, ctx)


async def admin_create(request: web.Request) -> web.Response:
    tg_id = await _require_ia(request)
    post = await request.post()
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    task_id = await asyncio.to_thread(it.create, (user or {}).get("id") or 0, post.get("title") or "")
    raise web.HTTPFound(f"/admin/interactive/{task_id}")


async def admin_save(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    body = await _json(request)
    task = await asyncio.to_thread(it.get, task_id)
    if not task:
        return web.json_response({"ok": False, "error": "Задание не найдено."}, status=404)
    if task.get("source") == "zip":
        ok, err = await asyncio.to_thread(it.save_meta, task_id, body)
    else:
        ok, err = await asyncio.to_thread(it.save, task_id, body)
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=400)
    task = await asyncio.to_thread(it.get, task_id)
    if body.get("publish"):
        okp, problems = await asyncio.to_thread(_publish_sync, task_id)
        if not okp:
            return web.json_response({"ok": False, "error": "Сохранено, но опубликовать нельзя: " + " ".join(problems)},
                                     status=400)
        task = await asyncio.to_thread(it.get, task_id)
    problems = it.publish_problems(task) if task.get("source") != "zip" else _zip_problems(task)
    return web.json_response({"ok": True, "status": task["status"], "version": task["version"], "problems": problems})


def _publish_sync(task_id: int) -> tuple:
    task = it.get(task_id)
    if task and task.get("source") == "zip":
        problems = _zip_problems(task)
        if problems:
            return False, problems
        from database import execute
        execute("UPDATE interactive_tasks SET status='published', published_at=? WHERE id=?", (utils.now_iso(), task_id))
        return True, []
    return it.publish(task_id)


async def admin_publish(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    ok, problems = await asyncio.to_thread(_publish_sync, task_id)
    raise _back(task_id, "Задание опубликовано" if ok else "Опубликовать нельзя: " + " ".join(problems))


async def admin_unpublish(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    await asyncio.to_thread(it.unpublish, task_id)
    raise _back(task_id, "Снято с публикации: ученики его не видят ни в списке, ни в уроках")


async def admin_render(request: web.Request) -> web.Response:
    """Документ задания из текущего, ещё не сохранённого кода — для «Запустить»."""
    await _require_ia(request)
    body = await _json(request)
    task = {k: str(body.get(k) or "") for k in ("html", "css", "js", "full_doc")}
    task["use_full_doc"] = bool(body.get("use_full_doc"))
    if any(len(v) > it.MAX_CODE for v in task.values() if isinstance(v, str)):
        return web.json_response({"error": "Слишком много кода."}, status=400)
    init = {"progress": body.get("progress"), "mode": "preview", "graded": body.get("grading") == it.SERVER}
    return web.json_response({"html": it.build_document(task, _origin(request), init)})


async def admin_check(request: web.Request) -> web.Response:
    """Проверить ключ ответа на примере ответа — ничего не сохраняет."""
    await _require_ia(request)
    body = await _json(request)
    key, err = it.parse_key(str(body.get("answer_key") or ""))
    if err:
        return web.json_response({"error": "Ключ ответа: " + err}, status=400)
    try:
        mx = max(1, min(1000, int(body.get("max_score") or 10)))
    except (TypeError, ValueError):
        mx = 10
    return web.json_response(it.grade(key, body.get("answer"), mx))


async def admin_duplicate(request: web.Request) -> web.Response:
    tg_id = await _require_ia(request)
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    new_id = await asyncio.to_thread(it.duplicate, int(request.match_info["task_id"]), (user or {}).get("id") or 0)
    if not new_id:
        raise web.HTTPNotFound(text="Задание не найдено")
    raise _back(new_id, "Копия создана как черновик")


async def admin_delete(request: web.Request) -> web.Response:
    await _require_ia(request)
    await asyncio.to_thread(it.delete, int(request.match_info["task_id"]))
    raise web.HTTPFound("/admin/interactive?message=" + quote("Задание удалено (и из всех уроков)"))


async def admin_asset_upload(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    if not await asyncio.to_thread(it.get, task_id):
        raise web.HTTPNotFound(text="Задание не найдено")
    post = await request.post()
    f = post.get("file")
    if f is None or not hasattr(f, "file"):
        return web.json_response({"error": "Файл не выбран."}, status=400)
    blob = f.file.read()
    asset, err = await asyncio.to_thread(it.add_asset, task_id, f.filename, blob)
    if err:
        return web.json_response({"error": err}, status=400)
    return web.json_response({"ok": True, "asset": asset})


async def admin_asset_delete(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = await asyncio.to_thread(it.delete_asset, int(request.match_info["asset_id"]))
    if task_id is None:
        return web.json_response({"error": "Не найдено."}, status=404)
    return web.json_response({"ok": True})


async def admin_results(request: web.Request) -> web.Response:
    await _require_ia(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    if not task:
        raise web.HTTPNotFound(text="Задание не найдено")
    ctx = await auth.nav_context(request)
    ctx["task"] = task
    ctx["rows"] = await asyncio.to_thread(it.admin_results, task["id"])
    return aiohttp_jinja2.render_template("admin_interactive_results.html", request, ctx)


# ───────────────────────── задание из ZIP: версии и файлы ─────────────────────────

async def admin_replace(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    name, blob, _f, err = await _read_upload(request, "file", pkg.ZIP_MAX)
    if err:
        raise _back(task_id, "Архив не принят: " + err, "versions")
    res = await asyncio.to_thread(pkg.replace_archive, task_id, blob, name)
    if res.get("error"):
        raise _back(task_id, "Архив не принят: " + res["error"], "versions")
    raise _back(task_id, "Новая версия загружена и ждёт проверки: посмотрите предпросмотр и опубликуйте её."
                if res.get("pending") else "Архив заменён (прежняя версия сохранена для отката).", "versions")


async def admin_pending(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    action = request.match_info["action"]
    if action == "publish":
        err = await asyncio.to_thread(pkg.publish_pending, task_id)
        raise _back(task_id, err or "Новая версия опубликована. Прежнюю можно вернуть кнопкой «Откатить».", "versions")
    await asyncio.to_thread(pkg.discard_pending, task_id)
    raise _back(task_id, "Новая версия удалена, ученики видят прежнюю.", "versions")


async def admin_rollback(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    err = await asyncio.to_thread(pkg.rollback, task_id)
    raise _back(task_id, err or "Возвращена прошлая версия. Откат можно отменить той же кнопкой.", "versions")


async def admin_doc(request: web.Request) -> web.Response:
    """Документ задания для предпросмотра админом: текущая, новая (pending) или прошлая версия."""
    await _require_ia(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    if not task:
        raise web.HTTPNotFound()
    which = request.query.get("v") if request.query.get("v") in ("pending", "prev") else "current"
    html = await asyncio.to_thread(_document_sync, task, _origin(request), None, "preview", which)
    if html is None:
        raise web.HTTPNotFound(text="Версии нет")
    return web.Response(text=html, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"})


async def admin_file_get(request: web.Request) -> web.Response:
    await _require_ia(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    if not task or task.get("source") != "zip":
        raise web.HTTPNotFound()
    key = task.get("pending_files_key") or task.get("files_key")
    text = await asyncio.to_thread(pkg.read_text_file, key, request.query.get("path") or "")
    if text is None:
        return web.json_response({"error": "Файл не найден или не текстовый."}, status=404)
    return web.json_response({"path": request.query.get("path"), "content": text})


async def admin_file_save(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    body = await _json(request)
    res = await asyncio.to_thread(pkg.save_text_file, task_id, str(body.get("path") or ""), str(body.get("content") or ""))
    return web.json_response(res, status=400 if res.get("error") else 200)


async def admin_to_lesson(request: web.Request) -> web.Response:
    await _require_ia(request)
    task_id = int(request.match_info["task_id"])
    post = await request.post()
    lesson_id = _int(post.get("lesson_id"))
    if not lesson_id or not await asyncio.to_thread(lb.lesson_exists, lesson_id):
        raise _back(task_id, "Выберите урок.", "lessons")
    bid, err = await asyncio.to_thread(lb.add_interactive, lesson_id, task_id)
    raise _back(task_id, err or "Задание добавлено в конец урока. Место в уроке меняется в редакторе урока.", "lessons")


# ───────────────────────── доступ ученика ─────────────────────────

_lesson_ok_cache: dict = {}          # (tg_id, lesson_id) -> (открыт ли урок, когда проверено)


def _lesson_open_sync(tg_id: int, lesson_id: int) -> bool:
    key, now = (tg_id, lesson_id), time.time()
    hit = _lesson_ok_cache.get(key)
    if hit and now - hit[1] < 60:
        return hit[0]
    try:
        data = learning._lesson_detail_sync(lesson_id, tg_id)
        ok = bool(data) and data.get("access_state") == "full"
    except Exception as e:
        logger.warning("доступ к уроку %s для %s: %s", lesson_id, tg_id, e)
        ok = False
    if len(_lesson_ok_cache) > 20000:
        _lesson_ok_cache.clear()
    if ok:          # помним только «открыт»: купивший доступ не ждёт, пока кэш забудет отказ
        _lesson_ok_cache[key] = (ok, now)
    return ok


def task_access_sync(task: dict, tg_id: int, lesson_id) -> bool:
    """Можно ли ученику открыть задание. Опубликованное «для всех» — да. Задание
    «только в уроках» — если оно стоит в этом уроке и урок ученику открыт (оплата,
    доступ к предмету и прочие правила урока)."""
    if not task:
        return False
    if utils.is_site_admin(tg_id):
        return True
    if task.get("status") != it.PUBLISHED:
        return False
    if (task.get("visibility") or "all") == "all":
        return True
    lid = _int(lesson_id)
    return bool(lid) and lb.task_in_lesson(task["id"], lid) and _lesson_open_sync(tg_id, lid)


def _lesson_param(request: web.Request):
    lid = _int(request.query.get("lesson"))
    return lid or None


def _document_sync(task: dict, origin: str, progress, mode: str, which: str = "current"):
    init = {"progress": progress, "mode": mode, "graded": task.get("grading") == it.SERVER}
    if task.get("source") == "zip":
        return pkg.build_document(task, origin, init, which)
    return it.build_document(task, origin, init)


async def tasks_index(request: web.Request) -> web.Response:
    ctx = await auth.nav_context(request)
    ctx["tasks"] = await asyncio.to_thread(it.list_published)
    return aiohttp_jinja2.render_template("interactive_list.html", request, ctx)


async def play(request: web.Request) -> web.Response:
    tg_id, user = await _user(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    lesson_id = _lesson_param(request)
    if not await asyncio.to_thread(task_access_sync, task, tg_id, lesson_id):
        raise web.HTTPNotFound(text="Задание не найдено")
    ctx = await auth.nav_context(request)
    ctx.update({
        "task": task, "lesson_id": lesson_id or "", "draft_view": task["status"] != it.PUBLISHED,
        "history": await asyncio.to_thread(it.user_results, task["id"], user["id"]),
        "watermark_svg": await asyncio.to_thread(learning._watermark_svg_sync, tg_id),
    })
    resp = aiohttp_jinja2.render_template("interactive_play.html", request, ctx)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def frame(request: web.Request) -> web.Response:
    """Документ задания. Страница кладёт его в изолированный фрейм (srcdoc + sandbox)."""
    tg_id, user = await _user(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    if not await asyncio.to_thread(task_access_sync, task, tg_id, _lesson_param(request)):
        raise web.HTTPNotFound(text="Задание не найдено")
    origin = _origin(request)
    progress = await asyncio.to_thread(it.load_progress, user["id"], task["id"])
    html = await asyncio.to_thread(_document_sync, task, origin, progress, "play")
    if html is None:
        raise web.HTTPNotFound(text="Файлы задания не найдены")
    pol = (pkg.csp(origin, task["files_key"]) if task.get("source") == "zip" else it.csp(origin))
    return web.Response(text=html, content_type="text/html", charset="utf-8", headers={
        "Content-Security-Policy": f"sandbox {FRAME_SANDBOX}; " + pol + "; frame-ancestors 'self'",
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    })


async def files(request: web.Request) -> web.Response:
    """Файлы задания из архива. Ключ папки случайный; выйти за папку нельзя.
    HTML и SVG отдаются с CSP sandbox: открытые напрямую, они не получат прав сайта."""
    path = await asyncio.to_thread(pkg.resolve_file, request.match_info["key"], request.match_info["path"])
    if not path:
        raise web.HTTPNotFound()
    resp = web.FileResponse(path, headers={"Content-Type": pkg.mime_of(path.name)})
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts; default-src * data: blob: 'unsafe-inline' 'unsafe-eval'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


async def asset(request: web.Request) -> web.Response:
    path = await asyncio.to_thread(it.asset_path, request.match_info["name"])
    if not path:
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    # Даже если файл открыть напрямую (svg), он не выполнит скрипт на сайте
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


async def _task_for_api(request: web.Request):
    tg_id, user = await _user(request)
    task = await asyncio.to_thread(it.get, int(request.match_info["task_id"]))
    if not await asyncio.to_thread(task_access_sync, task, tg_id, _lesson_param(request)):
        raise web.HTTPNotFound(text="Задание не найдено")
    lid = _lesson_param(request)
    if lid and not await asyncio.to_thread(lb.task_in_lesson, task["id"], lid):
        lid = None                     # чужой урок в адресе в результат не пишем
    # Предпросмотр в админке: проверяем, но ничего не сохраняем (результаты учеников чистые)
    preview = request.query.get("preview") == "1" and await asyncio.to_thread(utils.is_site_admin, tg_id)
    return user, task, lid, preview


async def api_progress(request: web.Request) -> web.Response:
    user, task, _lid, preview = await _task_for_api(request)
    body = await _json(request)
    if preview:
        return web.json_response({"ok": True, "preview": True})
    err = await asyncio.to_thread(it.save_progress, user["id"], task["id"], body.get("state"))
    return web.json_response({"ok": not err, "error": err or None}, status=400 if err else 200)


def _preview_grade(task: dict, answer) -> dict:
    if task.get("grading") != it.SERVER:
        return {"error": "Это тренировочное задание: результат считает само задание (SmartENT.reportScore)."}
    key, err = it.parse_key(task.get("answer_key") or "")
    if err:
        return {"error": "Ключ ответа: " + err}
    res = it.grade(key, answer, int(task.get("max_score") or 10))
    res["kind"] = "preview"
    res["note"] = "Предпросмотр: результат не сохранён"
    return res


async def api_submit(request: web.Request) -> web.Response:
    user, task, lid, preview = await _task_for_api(request)
    body = await _json(request)
    if preview:
        return web.json_response(_preview_grade(task, body.get("answer")))
    res = await asyncio.to_thread(it.submit, task, user["id"], body.get("answer"), lid)
    return web.json_response(res)


async def api_practice(request: web.Request) -> web.Response:
    user, task, lid, preview = await _task_for_api(request)
    body = await _json(request)
    if preview:
        try:
            sc, mx = int(float(body.get("score"))), int(float(body.get("max")))
        except (TypeError, ValueError):
            return web.json_response({"error": "Неверный результат: нужны числа score и max."}, status=400)
        return web.json_response({"score": sc, "max": mx, "percent": round(sc / mx * 100, 1) if mx else 0,
                                  "kind": "preview"})
    res = await asyncio.to_thread(it.report_practice, task, user["id"], body.get("score"), body.get("max"), lid)
    return web.json_response(res, status=400 if res.get("error") else 200)


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/admin/interactive", admin_index)
    r.add_get("/admin/interactive/template.zip", template_zip)
    r.add_post("/admin/interactive/upload", admin_upload)
    r.add_get("/admin/interactive/package/{package_id:\\d+}", admin_package)
    r.add_post("/admin/interactive/bulk", admin_bulk)
    r.add_post("/admin/interactive/create", admin_create)
    r.add_post("/admin/interactive/check", admin_check)
    r.add_get("/admin/interactive/{task_id:\\d+}", admin_edit)
    r.add_post("/admin/interactive/{task_id:\\d+}/save", admin_save)
    r.add_post("/admin/interactive/{task_id:\\d+}/render", admin_render)
    r.add_post("/admin/interactive/{task_id:\\d+}/publish", admin_publish)
    r.add_post("/admin/interactive/{task_id:\\d+}/unpublish", admin_unpublish)
    r.add_post("/admin/interactive/{task_id:\\d+}/duplicate", admin_duplicate)
    r.add_post("/admin/interactive/{task_id:\\d+}/delete", admin_delete)
    r.add_post("/admin/interactive/{task_id:\\d+}/assets", admin_asset_upload)
    r.add_post("/admin/interactive/assets/{asset_id:\\d+}/delete", admin_asset_delete)
    r.add_get("/admin/interactive/{task_id:\\d+}/results", admin_results)
    r.add_post("/admin/interactive/{task_id:\\d+}/replace", admin_replace)
    r.add_post("/admin/interactive/{task_id:\\d+}/pending/{action:publish|discard}", admin_pending)
    r.add_post("/admin/interactive/{task_id:\\d+}/rollback", admin_rollback)
    r.add_get("/admin/interactive/{task_id:\\d+}/doc", admin_doc)
    r.add_get("/admin/interactive/{task_id:\\d+}/file", admin_file_get)
    r.add_post("/admin/interactive/{task_id:\\d+}/file", admin_file_save)
    r.add_post("/admin/interactive/{task_id:\\d+}/to-lesson", admin_to_lesson)
    r.add_get("/interactive", tasks_index)
    r.add_get("/interactive/{task_id:\\d+}", play)
    r.add_get("/interactive/frame/{task_id:\\d+}", frame)
    r.add_get("/interactive/files/{key:[0-9a-f]{32}}/{path:.+}", files)
    r.add_get("/interactive/asset/{name}", asset)
    r.add_post("/interactive/api/{task_id:\\d+}/progress", api_progress)
    r.add_post("/interactive/api/{task_id:\\d+}/submit", api_submit)
    r.add_post("/interactive/api/{task_id:\\d+}/practice", api_practice)
