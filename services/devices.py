"""Устройства и входы (v96).

Каждый вход на сайт или в мини-приложение — строка user_sessions со своим
случайным номером; номер лежит в зашифрованной куке. По этой строке видно
устройство, IP, время входа и последнего действия, и её можно завершить:
на этом устройстве человек выйдет при следующем запросе, остальные не затронуты.

Лимит считает только входы через браузер (по логину и паролю или Telegram
на сайте). Мини-приложение открывается под Telegram-аккаунтом самого ученика,
поэтому показывается в списке, но в лимит не входит. Устройство, которым не
пользовались LIMIT_IDLE_DAYS дней, в лимит тоже не входит: забытая вкладка не
должна мешать войти. Админы и владелец без лимита.
"""
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import config
import database as db
import utils

log = logging.getLogger(__name__)

LIMIT_IDLE_DAYS = 7
TOUCH_SECONDS = 300
POLICIES = {"oldest": "Выкидывать самое старое устройство", "block": "Не пускать на новом устройстве"}
SOURCES = {"site": "сайт", "miniapp": "мини-приложение", "owner": "вход владельца"}


def _setting(key: str, default: str) -> str:
    r = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r and r["value"] not in (None, "") else default


def settings() -> dict:
    pol = _setting("device_policy", "oldest")
    return {"limit": max(0, min(int(_setting("device_limit", "2") or 2), 50)),
            "policy": pol if pol in POLICIES else "oldest",
            "warn": max(2, min(int(_setting("device_warn", "4") or 4), 100))}


def save_settings(limit: int, policy: str, warn: int) -> None:
    for k, v in (("device_limit", max(0, min(int(limit), 50))), ("device_policy", policy if policy in POLICIES else "oldest"),
                 ("device_warn", max(2, min(int(warn), 100)))):
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (k, str(v)))


def user_limit(tg_id: int) -> int:
    """Лимит этого ученика: личный, если админ его задал, иначе общий. 0 — без лимита."""
    if utils.is_site_admin(tg_id):
        return 0
    r = db.fetchone("SELECT device_limit FROM users WHERE tg_id=?", (int(tg_id),))
    if r and r["device_limit"] is not None:
        return max(0, int(r["device_limit"]))
    return settings()["limit"]


# ───────────────────────── устройство по User-Agent ─────────────────────────

def label(ua: str, source: str = "site") -> str:
    """«iPhone, Telegram», «Windows, Chrome» — понятное название без лишних подробностей."""
    ua = ua or ""
    if re.search(r"iPhone", ua):
        os_ = "iPhone"
    elif re.search(r"iPad", ua):
        os_ = "iPad"
    elif re.search(r"Android", ua):
        m = re.search(r"Android [\d.]+; ([^;)]+?)(?: Build|\))", ua)
        os_ = "Android" + (f" ({m.group(1).strip()})" if m and len(m.group(1).strip()) < 30 and m.group(1).strip() != "K" else "")
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_ = "Mac"
    elif "CrOS" in ua:
        os_ = "Chromebook"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = "Неизвестное устройство"
    if source == "miniapp" or "Telegram" in ua:
        app = "Telegram"
    elif "YaBrowser" in ua:
        app = "Яндекс Браузер"
    elif "Edg/" in ua:
        app = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        app = "Opera"
    elif "SamsungBrowser" in ua:
        app = "Samsung Internet"
    elif "Firefox/" in ua or "FxiOS" in ua:
        app = "Firefox"
    elif "CriOS" in ua or "Chrome/" in ua:
        app = "Chrome"
    elif "Safari/" in ua:
        app = "Safari"
    else:
        app = "браузер"
    return f"{os_}, {app}"


def device_key(ua: str, source: str) -> str:
    return hashlib.sha256(f"{source}|{ua or ''}".encode()).hexdigest()[:16]


def client_ip(request) -> str:
    # Railway дописывает настоящий адрес последним; первые значения может подставить кто угодно
    xff = request.headers.get("X-Forwarded-For") or ""
    return (xff.split(",")[-1].strip() if xff else (request.remote or ""))[:64]


# ───────────────────────── журнал ─────────────────────────

def _log(tg_id: int, event: str, lbl: str = "", ip: str = "", source: str = "", key: str = "", details: str = "") -> None:
    db.execute("INSERT INTO device_log (user_tg, ts, event, device_label, ip, source, device_key, details) "
               "VALUES (?,?,?,?,?,?,?,?)", (int(tg_id), utils.now_iso(), event, lbl[:80], ip[:64], source, key, details[:200]))


EVENTS = {"login": "вход", "kicked": "вытеснено новым входом", "blocked": "не пущен: лимит устройств",
          "ended_admin": "завершено админом", "ended_user": "завершено учеником", "logout": "выход",
          "reset": "сброс всех сеансов"}


# ───────────────────────── сеансы ─────────────────────────

def _active_counted(tg_id: int) -> list:
    since = (datetime.utcnow() - timedelta(days=LIMIT_IDLE_DAYS)).isoformat(timespec="seconds")
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM user_sessions WHERE user_tg=? AND ended_at IS NULL AND source='site' AND last_seen_at>=? "
        "ORDER BY last_seen_at ASC", (int(tg_id), since))]


def start(tg_id: int, ua: str, ip: str, source: str) -> dict:
    """Новый вход. {"ok": True, "sid": …, "kicked": [названия]} или {"ok": False, "error": …} при лимите."""
    source = source if source in SOURCES else "site"
    lbl, key = label(ua, source), device_key(ua, source)
    kicked = []
    lim = user_limit(tg_id) if source == "site" else 0
    if lim:
        active = _active_counted(tg_id)
        over = len(active) - lim + 1
        if over > 0:
            if settings()["policy"] == "block":
                _log(tg_id, "blocked", lbl, ip, source, key)
                _check_suspicious(tg_id)
                return {"ok": False, "error": f"На этот аккаунт уже вошли с {len(active)} устройств, больше нельзя. "
                                              "Выйдите на одном из них («Профиль» → «Мои устройства») или "
                                              "обратитесь к администратору."}
            for old in active[:over]:
                end(old["id"], "kicked")
                kicked.append(old["device_label"])
    sid = secrets.token_hex(16)
    now = utils.now_iso()
    db.execute("INSERT INTO user_sessions (sid, user_tg, created_at, last_seen_at, ip, last_ip, ua, device_label, device_key, source) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)", (sid, int(tg_id), now, now, ip[:64], ip[:64], (ua or "")[:300], lbl, key, source))
    _log(tg_id, "login", lbl, ip, source, key, ("вытеснено: " + ", ".join(kicked)) if kicked else "")
    warned = _check_suspicious(tg_id)
    return {"ok": True, "sid": sid, "kicked": kicked, "warned": warned}


def get(sid: str) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM user_sessions WHERE sid=?", (sid,))
    return dict(r) if r else None


def adopt(tg_id: int, ua: str, ip: str, source: str) -> str:
    """Вход, сделанный до v96: заводим ему запись без проверки лимита, чтобы никого не выкинуло обновлением."""
    source = source if source in SOURCES else "site"
    sid = secrets.token_hex(16)
    now = utils.now_iso()
    db.execute("INSERT INTO user_sessions (sid, user_tg, created_at, last_seen_at, ip, last_ip, ua, device_label, device_key, source) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)", (sid, int(tg_id), now, now, ip[:64], ip[:64], (ua or "")[:300],
                                                label(ua, source), device_key(ua, source), source))
    return sid


def check(sid: str, tg_id: int, ip: str) -> str:
    """Проверка на каждый запрос: ok, ended или kicked (устройство вытеснил новый вход)."""
    row = get(sid)
    if not row or row["user_tg"] != int(tg_id):
        return "ended"
    if row["ended_at"]:
        return "kicked" if row["end_reason"] == "kicked" else "ended"
    touch(row, ip)
    return "ok"


def would_block(tg_id: int) -> str:
    """Перед отправкой кода: если новый вход всё равно не пустят, говорим сразу."""
    lim = user_limit(tg_id)
    if not lim or settings()["policy"] != "block":
        return ""
    n = len(_active_counted(tg_id))
    if n >= lim:
        return (f"На этот аккаунт уже вошли с {n} устройств, больше нельзя. Выйдите на одном из них "
                "(«Профиль» → «Мои устройства») или обратитесь к администратору.")
    return ""


def log_blocked(tg_id: int, ua: str, ip: str, source: str = "site") -> bool:
    """Попытка входа, которую не пустил лимит: в журнал и в подсчёт подозрительных."""
    _log(tg_id, "blocked", label(ua, source), ip, source, device_key(ua, source))
    return _check_suspicious(tg_id)


def touch(row: dict, ip: str) -> None:
    """Отметка активности, не чаще раза в 5 минут — чтобы не писать в базу на каждый запрос."""
    try:
        age = (datetime.utcnow() - datetime.fromisoformat(row["last_seen_at"])).total_seconds()
    except (TypeError, ValueError):
        age = TOUCH_SECONDS + 1
    if age > TOUCH_SECONDS or (ip and ip != row.get("last_ip")):
        db.execute("UPDATE user_sessions SET last_seen_at=?, last_ip=? WHERE id=?", (utils.now_iso(), ip[:64], row["id"]))


def end(session_id: int, reason: str, actor=None) -> bool:
    r = db.fetchone("SELECT * FROM user_sessions WHERE id=?", (int(session_id),))
    if not r or r["ended_at"]:
        return False
    db.execute("UPDATE user_sessions SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL",
               (utils.now_iso(), reason, r["id"]))
    _log(r["user_tg"], reason, r["device_label"] or "", r["last_ip"] or r["ip"] or "", r["source"] or "", r["device_key"] or "",
         f"админ {actor}" if reason == "ended_admin" and actor else "")
    return True


def end_all(tg_id: int, reason: str = "reset", except_sid: str = "") -> int:
    n = 0
    for r in db.fetchall("SELECT id, sid FROM user_sessions WHERE user_tg=? AND ended_at IS NULL", (int(tg_id),)):
        if r["sid"] != except_sid and end(r["id"], reason):
            n += 1
    return n


def sessions(tg_id: int, active_only: bool = True) -> list:
    sql = "SELECT * FROM user_sessions WHERE user_tg=?" + (" AND ended_at IS NULL" if active_only else "")
    out = []
    since = (datetime.utcnow() - timedelta(days=LIMIT_IDLE_DAYS)).isoformat(timespec="seconds")
    for r in db.fetchall(sql + " ORDER BY last_seen_at DESC LIMIT 50", (int(tg_id),)):
        d = dict(r)
        d["counted"] = d["source"] == "site" and (d["last_seen_at"] or "") >= since
        d["source_title"] = SOURCES.get(d["source"], d["source"])
        out.append(d)
    return out


def journal(tg_id: int, limit: int = 60) -> list:
    out = []
    for r in db.fetchall("SELECT * FROM device_log WHERE user_tg=? ORDER BY id DESC LIMIT ?", (int(tg_id), limit)):
        d = dict(r)
        d["event_title"] = EVENTS.get(d["event"], d["event"])
        d["source_title"] = SOURCES.get(d.get("source") or "", d.get("source") or "")
        out.append(d)
    return out


# ───────────────────────── подозрительные ─────────────────────────

def _check_suspicious(tg_id: int) -> bool:
    """За сутки вход (или попытка) с порогового числа разных устройств — пометка и одно сообщение админу в день."""
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = db.fetchall("SELECT DISTINCT device_key, device_label, ip FROM device_log WHERE user_tg=? AND ts>=? "
                       "AND event IN ('login','blocked')", (int(tg_id), since))
    keys = {r["device_key"] for r in rows}
    cfg = settings()
    if len(keys) < cfg["warn"] or utils.is_site_admin(tg_id):
        return False
    today = utils.now_iso()[:10]
    cur = db.execute("UPDATE users SET suspicious_at=?, suspicious_note=? WHERE tg_id=? AND COALESCE(substr(suspicious_at,1,10),'')<>?",
                     (utils.now_iso(), f"{len(keys)} устройств за сутки, IP: {len({r['ip'] for r in rows})}", int(tg_id), today))
    if not cur.rowcount:
        return False
    _pending_alerts.append((int(tg_id), len(keys), len({r["ip"] for r in rows}),
                            sorted({r["device_label"] for r in rows})[:8]))
    return True


_pending_alerts: list = []


async def send_alerts(bot=None) -> int:
    """Сообщение владельцам о подозрительном аккаунте. Вызывается после входа, ошибки не мешают входу."""
    sent = 0
    while _pending_alerts:
        tg_id, n_dev, n_ip, labels = _pending_alerts.pop()
        u = utils.get_user_by_tg(tg_id) or {}
        who = ("@" + u["username"]) if u.get("username") else (u.get("first_name") or str(tg_id))
        from services.broadcast_service import site_url
        text = ("⚠️ <b>Подозрительный аккаунт</b>\n\n"
                f"👤 {utils.escape_html(who)} · ID <code>{tg_id}</code>\n"
                f"📱 Разных устройств за сутки: <b>{n_dev}</b>, разных IP: <b>{n_ip}</b>\n"
                + "".join(f"• {utils.escape_html(x)}\n" for x in labels)
                + f"\nУстройства и журнал входов: {site_url()}/admin/users/{tg_id}")
        own = bot is None
        if own:
            from aiogram import Bot
            bot = Bot(token=config.BOT_TOKEN)
        try:
            for admin in config.ADMIN_IDS:
                try:
                    await bot.send_message(admin, text, parse_mode="HTML", disable_web_page_preview=True)
                    sent += 1
                except Exception as e:
                    log.warning("предупреждение об устройствах %s: %s", admin, e)
        finally:
            if own:
                try:
                    await bot.session.close()
                except Exception:
                    pass
    return sent


def suspicious(limit: int = 100) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT tg_id, username, first_name, suspicious_at, suspicious_note FROM users WHERE suspicious_at IS NOT NULL "
        "ORDER BY suspicious_at DESC LIMIT ?", (limit,))]


def clear_suspicious(tg_id: int) -> None:
    db.execute("UPDATE users SET suspicious_at=NULL, suspicious_note=NULL WHERE tg_id=?", (int(tg_id),))
