"""
Диагностика для админа: /admin/health — что останавливало сервер, медленные
запросы, ошибки сервера и браузера. И приём сообщений об ошибках со страниц
учеников (/api/client-log), чтобы следующую жалобу можно было разобрать
по фактам, а не вслепую.
"""
import asyncio
import json
import time

import aiohttp_jinja2
from aiohttp import web

from services import diag
from webapp import auth

_rate = {}                      # ключ → [метки времени]: не больше 20 сообщений в минуту
RATE_MAX, RATE_WINDOW = 20, 60
CLIENT_KINDS = {"js_error": "client", "js_promise": "client", "net": "net", "auth": "auth",
                "auth_loop": "auth", "stuck": "client", "slow_nav": "net"}


def _allowed(key: str) -> bool:
    now = time.time()
    hist = [t for t in _rate.get(key, []) if now - t < RATE_WINDOW]
    if len(hist) >= RATE_MAX:
        _rate[key] = hist
        return False
    hist.append(now)
    _rate[key] = hist
    if len(_rate) > 5000:
        _rate.clear()
    return True


async def client_log(request: web.Request) -> web.Response:
    """Страница сообщает об ошибке: текст, адрес, устройство. Ничего секретного
    сюда не приходит и не пишется (ни токенов, ни данных входа Telegram)."""
    try:
        raw = await request.text()
        body = json.loads(raw[:4000] or "{}")
    except Exception:
        return web.json_response({"ok": False}, status=400)
    tg_id = None
    try:
        tg_id = await auth.get_logged_in_tg_id(request)
    except Exception:
        pass
    key = str(tg_id or request.headers.get("X-Forwarded-For") or request.remote or "?")
    if not _allowed(key):
        return web.json_response({"ok": True, "skipped": True})
    kind = CLIENT_KINDS.get(str(body.get("kind") or ""), "client")
    diag.record(kind, f"[{str(body.get('kind') or 'client')[:20]}] {str(body.get('msg') or '')[:400]}",
                url=str(body.get("url") or "")[:200], tg_id=tg_id,
                ua=(request.headers.get("User-Agent") or "")[:160],
                extra=(json.dumps(body.get("extra"), ensure_ascii=False)[:400] if body.get("extra") else None))
    return web.json_response({"ok": True})


def _ctx_sync(kind: str) -> dict:
    rows = diag.recent(kind or None, 150)
    for r in rows:
        r["when"] = diag.fmt_local(r["at"])
        r["title"] = diag.KINDS.get(r["kind"], r["kind"])
        m = r.get("meta") or {}
        r["details"] = " · ".join(f"{k}: {m[k]}" for k in ("url", "path", "tg_id", "ms", "seconds", "rid", "ua", "extra") if m.get(k))
        r["trace"] = m.get("stack") or m.get("trace") or ""
    return {"rows": rows, "counts": diag.counts(24), "kinds": list(diag.KINDS.items()), "kind": kind,
            "slow": diag.slow_endpoints(24), "uptime": diag.uptime_text()}


async def admin_health(request: web.Request) -> web.Response:
    from webapp import learning
    await learning._require_admin(request)
    data = await auth.nav_context(request)
    data.update(await asyncio.to_thread(_ctx_sync, request.query.get("kind") or ""))
    return aiohttp_jinja2.render_template("admin_health.html", request, data)


def register_routes(app: web.Application) -> None:
    app.router.add_post("/api/client-log", client_log)
    app.router.add_get("/admin/health", admin_health)
