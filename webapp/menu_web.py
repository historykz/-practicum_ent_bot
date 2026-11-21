"""«🏠 Главное меню бота» в админке сайта (v100): /admin/menu.
Та же логика, что в боте (handlers/menu_admin.py): services/main_menu.py."""
import asyncio
import html
import re
from urllib.parse import quote

from aiohttp import web

from services import main_menu as mm
from services import trash
from webapp import admin_hub, learning

PATH = "/admin/menu"


def _go(anchor: str = "", message: str = "", error: str = "") -> web.HTTPFound:
    q = ("?message=" + quote(message)) if message else ("?error=" + quote(error) if error else "")
    return web.HTTPFound(PATH + q + (("#" + anchor) if anchor else ""))


def _page_sync() -> dict:
    rows = mm.all_buttons()
    for b in rows:
        b["title"] = mm.label(b, "ru")
        b["live"] = mm.is_live(b)
        b["status_text"] = mm.status(b)
        b["kind_text"] = mm.KINDS.get(b["kind"], b["kind"])
        b["from_input"] = (b.get("show_from") or "").replace(" ", "T")
        b["until_input"] = (b.get("show_until") or "").replace(" ", "T")
        b["builtin_title"] = mm.BUILTIN_TITLES.get(b.get("builtin_key") or "", "")
        b["photo_is_url"] = (b.get("photo_file_id") or "").startswith("https://")
    preview = []
    for row in mm.keyboard("ru", False).inline_keyboard:
        preview.append([{"text": x.text, "kind": "url" if x.url else ("app" if x.web_app else "cb")} for x in row])
    texts = []
    for key, title in mm.TEXTS.items():
        texts.append({"key": key, "title": title,
                      "ru": mm.get_text(key, "ru"), "kz": mm.get_text(key, "kz") if key != "support_text" else None,
                      "def_ru": mm.default_text(key, "ru"), "def_kz": mm.default_text(key, "kz")})
    plain = html.unescape(re.sub(r"<[^>]+>", "", mm.menu_text("ru")))
    return {"buttons": rows, "preview": preview, "texts": texts, "actions": mm.ACTIONS, "kinds": mm.KINDS,
            "menu_preview_text": plain}


async def page(request: web.Request) -> web.Response:
    await learning._require_owner(request)
    ctx = await asyncio.to_thread(_page_sync)
    return await admin_hub._render(request, "admin_menu.html", ctx)


def _audit(actor, action, title, details=""):
    trash.audit(actor, "", "site", action, "menu_button", "Главное меню: " + (title or ""), None, details)


def _save_sync(actor, bid: int, post) -> str:
    b = mm.get_button(bid)
    if not b:
        raise ValueError("Кнопка не найдена.")
    fields = {"label_ru": post.get("label_ru", ""), "label_kz": post.get("label_kz", ""),
              "full_row": 1 if post.get("full_row") else 0, "enabled": 1 if post.get("enabled") else 0,
              "show_from": post.get("show_from", ""), "show_until": post.get("show_until", "")}
    if b["kind"] == "builtin" and (fields["label_ru"] or "").strip() == mm.BUILTIN_TITLES.get(b["builtin_key"]):
        fields["label_ru"] = ""                       # прежняя надпись — без своей версии
    if b["kind"] in ("link", "webapp"):
        fields["url"] = post.get("url", "")
    if b["kind"] == "action":
        fields["action"] = post.get("action", "")
    if b["kind"] == "text":
        fields["body_ru"] = mm.check_html(post.get("body_ru", ""))
        fields["body_kz"] = mm.check_html(post.get("body_kz", ""))
        fields["links"] = mm.parse_links(post.get("links", ""))
    mm.update(bid, **fields)
    if b["kind"] == "text":
        if post.get("photo_clear"):
            mm.set_photo(bid, "")
        elif (post.get("photo_url") or "").strip():
            mm.set_photo(bid, post.get("photo_url"))
    _audit(actor, "settings", mm.label(mm.get_button(bid), "ru"), "изменена на сайте")
    return "Сохранено. В боте уже так."


async def save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    bid = int(request.match_info["bid"])
    post = await request.post()
    try:
        msg = await asyncio.to_thread(_save_sync, actor, bid, post)
    except ValueError as e:
        raise _go(f"b{bid}", error=str(e))
    raise _go(f"b{bid}", message=msg)


async def action(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    bid = int(request.match_info["bid"])
    post = await request.post()
    what = post.get("what")
    b = await asyncio.to_thread(mm.get_button, bid)
    if not b:
        raise _go("", error="Кнопка не найдена.")
    try:
        if what in ("up", "down"):
            await asyncio.to_thread(mm.move, bid, what)
            raise _go(f"b{bid}")
        if what == "toggle":
            await asyncio.to_thread(mm.update, bid, enabled=0 if b["enabled"] else 1)
            await asyncio.to_thread(_audit, actor, "settings", mm.label(b, "ru"), "выкл" if b["enabled"] else "вкл")
            raise _go(f"b{bid}", message=("Кнопка выключена." if b["enabled"] else "Кнопка включена."))
        if what == "delete":
            await asyncio.to_thread(mm.delete, bid, actor, "site")
            raise _go("", message=trash.MOVED_MSG)
    except ValueError as e:
        raise _go("", error=str(e))
    raise _go("", error="Неизвестное действие.")


def _create_sync(actor, post) -> int:
    kind = post.get("kind", "")
    body = mm.check_html(post.get("body_ru", "")) if kind == "text" else ""
    links = mm.parse_links(post.get("links", "")) if kind == "text" else ""
    bid = mm.create(kind, post.get("label_ru", ""), url=post.get("url", ""), body=body,
                    action=post.get("action", ""), label_kz=post.get("label_kz", ""),
                    full_row=1 if post.get("full_row") else 0, show_from=post.get("show_from", ""),
                    show_until=post.get("show_until", ""))
    if links:
        mm.update(bid, links=links)
    if kind == "text" and (post.get("photo_url") or "").strip():
        mm.set_photo(bid, post.get("photo_url"))
    _audit(actor, "create", post.get("label_ru", ""), mm.KINDS.get(kind, kind))
    return bid


async def create(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    try:
        bid = await asyncio.to_thread(_create_sync, actor, post)
    except ValueError as e:
        raise _go("new", error=str(e))
    raise _go(f"b{bid}", message="Кнопка добавлена в конец меню.")


async def text_save(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    post = await request.post()
    key = post.get("key", "")
    try:
        for lang in ("ru", "kz"):
            if key == "support_text" and lang == "kz":
                continue
            if lang in post:
                value = (post.get(lang) or "").replace("\r\n", "\n").strip()
                if value == (mm.default_text(key, lang) or "").strip():
                    value = ""                      # как в коде — своя версия не нужна
                await asyncio.to_thread(mm.set_text, key, lang, mm.check_html(value) if value else "")
    except ValueError as e:
        raise _go("texts", error=str(e))
    await asyncio.to_thread(trash.audit, actor, "", "site", "save", "menu_button", "Тексты: " + mm.TEXTS.get(key, key))
    raise _go("texts", message="Текст сохранён.")


async def reset(request: web.Request) -> web.Response:
    actor = await learning._require_owner(request)
    await asyncio.to_thread(mm.reset_builtins)
    await asyncio.to_thread(_audit, actor, "settings", "встроенные кнопки как было")
    raise _go("", message="Встроенные кнопки как было. Ваши кнопки остались.")


def register_routes(app: web.Application) -> None:
    r = app.router
    r.add_get(PATH, page)
    r.add_post(PATH + "/new", create)
    r.add_post(PATH + "/texts", text_save)
    r.add_post(PATH + "/reset", reset)
    r.add_post(PATH + "/{bid:\\d+}", save)
    r.add_post(PATH + "/{bid:\\d+}/action", action)
