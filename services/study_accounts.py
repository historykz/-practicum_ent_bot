"""Учебные аккаунты учеников (v95).

Учебный аккаунт появляется только после действия администратора «Создать
аккаунт» в боте или в админке сайта. До этого человек — обычный пользователь
бота: /start, язык и профильные предметы аккаунт не создают, сообщений о
регистрации не приходит.

Аккаунт — это тот же пользователь (users) с пометкой study_account, своим
логином и правами: выбранные предметы (subject_access, видны в «Мои классы»),
Premium по желанию админа и режим водяных знаков. Пароль ученик задаёт сам по
одноразовой ссылке; в базе хранятся только хеш пароля и отпечаток ссылки.

Если человек ещё не запускал бота, создаётся приглашение: бот первым не пишет,
аккаунт создаётся и сообщение уходит, когда он сам нажмёт /start.
"""
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import config
import database as db
import utils
from services import account_service as acc

log = logging.getLogger(__name__)

SETUP_TTL_DAYS = 7
WM_MODES = {"on": "Включены везде", "off_site": "Отключены только на сайте",
            "off_app": "Отключены только в мини-приложении", "off": "Отключены везде"}
PREMIUM_CHOICES = {"none": "Без Premium", "30": "30 дней", "90": "90 дней", "180": "180 дней",
                   "365": "1 год", "0": "Бессрочно"}


# ───────────────────────── поиск ─────────────────────────

def find_user(ident: str) -> Optional[dict]:
    """Пользователь бота по @username или Telegram ID (только реальные, с Telegram)."""
    raw = (ident or "").strip()
    if raw.lstrip("-").isdigit():
        row = db.fetchone("SELECT * FROM users WHERE tg_id=?", (int(raw),))
    else:
        u = acc.normalize_username(raw)
        row = db.fetchone("SELECT * FROM users WHERE LOWER(username)=? AND tg_id>0 ORDER BY tg_linked DESC, id LIMIT 1",
                          (u,)) if u else None
    if not row or int(row["tg_id"] or 0) <= 0:
        return None
    return dict(row)


def find_by_login(login: str) -> Optional[dict]:
    """Вход на сайте: сначала логин учебного аккаунта, потом Telegram username (старые аккаунты)."""
    raw = (login or "").strip().lower().lstrip("@")
    if not raw:
        return None
    row = db.fetchone("SELECT * FROM users WHERE study_login=?", (raw,))
    return dict(row) if row else acc.find_by_username(raw)


def subjects() -> list:
    return [dict(r) for r in db.fetchall("SELECT id, title FROM subjects ORDER BY sort_order, id")]


# ───────────────────────── логин и ссылка на пароль ─────────────────────────

def _free_login(user: dict) -> str:
    base = acc.normalize_username(user.get("username") or "")
    if base and re.fullmatch(r"[a-z0-9_]{4,32}", base):
        other = db.fetchone("SELECT id FROM users WHERE (study_login=? OR LOWER(username)=?) AND id<>?",
                            (base, base, user["id"]))
        if not other:
            return base
    while True:
        cand = f"smart{secrets.randbelow(900000) + 100000}"
        if not db.fetchone("SELECT 1 FROM users WHERE study_login=? OR LOWER(username)=?", (cand, cand)):
            return cand


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def issue_setup_link(user_id: int, purpose: str = "setup") -> str:
    """Одноразовая ссылка: ученик сам задаёт пароль. Прежние ссылки перестают работать."""
    db.execute("UPDATE account_setup_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
               (utils.now_iso(), int(user_id)))
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO account_setup_tokens (token_hash, user_id, purpose, expires_at, created_at) VALUES (?,?,?,?,?)",
               (_hash(token), int(user_id), purpose,
                (datetime.utcnow() + timedelta(days=SETUP_TTL_DAYS)).isoformat(timespec="seconds"), utils.now_iso()))
    return token


def setup_user(token: str) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM account_setup_tokens WHERE token_hash=?", (_hash(token),))
    if not r or r["used_at"] or (r["expires_at"] or "") <= utils.now_iso():
        return None
    u = db.fetchone("SELECT * FROM users WHERE id=?", (r["user_id"],))
    if not u or not u["study_account"] or (u["study_status"] or "active") != "active":
        return None
    return dict(u)


def use_setup(token: str, password: str) -> Optional[dict]:
    user = setup_user(token)
    if not user:
        return None
    cur = db.execute("UPDATE account_setup_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                     (utils.now_iso(), _hash(token)))
    if cur.rowcount != 1:
        return None
    acc.set_password(user["id"], password)          # прежний пароль и сеансы перестают действовать
    # Ссылка пришла в личный чат с ботом: открыв её, человек подтвердил, что этот Telegram — его
    db.execute("UPDATE users SET tg_linked=1, tg_linked_at=COALESCE(tg_linked_at, ?) WHERE id=?", (utils.now_iso(), user["id"]))
    acc.log_event(user["id"], user["tg_id"], "study_password_set")
    return dict(db.fetchone("SELECT * FROM users WHERE id=?", (user["id"],)))


def setup_url(token: str) -> str:
    from services.broadcast_service import site_url
    return f"{site_url()}/account/setup/{token}"


# ───────────────────────── создание и правка ─────────────────────────

def _grant_subjects(user: dict, subject_ids: list, actor) -> list:
    titles = []
    for sid in subject_ids:
        s = db.fetchone("SELECT id, title FROM subjects WHERE id=?", (int(sid),))
        if not s:
            continue
        cur = db.fetchone("SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?", (s["id"], user["tg_id"]))
        if cur and utils.deadline_active(cur["expires_at"]):
            pass                                        # уже есть действующий доступ — не трогаем срок
        else:
            db.execute("INSERT INTO subject_access (subject_id, user_tg_id, expires_at, granted_by_admin) VALUES (?,?,NULL,?) "
                       "ON CONFLICT(subject_id, user_tg_id) DO UPDATE SET expires_at=NULL, granted_by_admin=excluded.granted_by_admin",
                       (s["id"], user["tg_id"], actor))
        titles.append(s["title"])
    return titles


def apply_premium(user: dict, choice: str, actor) -> str:
    if choice in (None, "", "none"):
        return ""
    days = int(choice) if str(choice).isdigit() else 0
    utils.grant_premium(user["id"], days, actor or 0)
    try:
        from services import reward_service as _rs
        _rs.check_user(user["tg_id"])
    except Exception as e:
        log.warning("premium check_user: %s", e)
    return "бессрочно" if days == 0 else f"{days} дней"


def create(user: dict, subject_ids: list, premium: str = "none", wm_mode: str = "on", actor=None,
           source: str = "bot") -> dict:
    """Создать (или дополнить) учебный аккаунт. Повторное создание дубликата не делает."""
    fresh = not user.get("study_account")
    login = user.get("study_login") or _free_login(user)
    db.execute("UPDATE users SET study_account=1, study_login=?, study_status='active', "
               "study_created_at=COALESCE(study_created_at, ?), study_created_by=COALESCE(study_created_by, ?), "
               "watermark_mode=? WHERE id=?",
               (login, utils.now_iso(), actor, wm_mode if wm_mode in WM_MODES else "on", user["id"]))
    titles = _grant_subjects(user, subject_ids, actor)
    prem = apply_premium(user, premium, actor)
    token = issue_setup_link(user["id"]) if fresh or not acc.has_password(user) else None
    acc.log_event(user["id"], user["tg_id"], "study_account_created" if fresh else "study_account_updated",
                  f"предметы: {', '.join(titles)}")
    _audit(actor, source, "create" if fresh else "save", user, f"предметы: {', '.join(titles)}; Premium: {prem or 'нет'}")
    return {"login": login, "token": token, "subjects": titles, "premium": prem, "fresh": fresh,
            "has_password": acc.has_password(user)}


def set_subjects(user: dict, subject_ids: list, actor, source: str = "bot") -> list:
    """Ровно эти предметы в «Моих классах»: лишние доступы снимаются."""
    keep = {int(x) for x in subject_ids}
    for r in db.fetchall("SELECT subject_id FROM subject_access WHERE user_tg_id=?", (user["tg_id"],)):
        if r["subject_id"] not in keep:
            db.execute("DELETE FROM subject_access WHERE subject_id=? AND user_tg_id=?", (r["subject_id"], user["tg_id"]))
    titles = _grant_subjects(user, sorted(keep), actor)
    _audit(actor, source, "subject_grant", user, ", ".join(titles) or "без предметов")
    return titles


def set_watermark(user: dict, mode: str, actor, source: str = "bot") -> None:
    if mode in WM_MODES:
        db.execute("UPDATE users SET watermark_mode=? WHERE id=?", (mode, user["id"]))
        _audit(actor, source, "settings", user, "водяные знаки: " + WM_MODES[mode])


def set_status(user: dict, active: bool, actor, source: str = "bot") -> None:
    db.execute("UPDATE users SET study_status=? WHERE id=?", ("active" if active else "disabled", user["id"]))
    if not active:
        db.execute("UPDATE users SET admin_session_reset=COALESCE(admin_session_reset,0)+1 WHERE id=?", (user["id"],))
    _audit(actor, source, "unblock" if active else "block", user, "учебный аккаунт " + ("включён" if active else "отключён"))


def _audit(actor, source, action, user, details) -> None:
    try:
        from services import trash
        trash.audit(actor, "", source, action, "user",
                    user.get("study_login") or user.get("username") or str(user["tg_id"]), user["tg_id"], details)
    except Exception:
        pass


def my_classes(tg_id: int) -> list:
    """Предметы с выданным и действующим доступом — только они в «Моих классах»."""
    rows = db.fetchall("SELECT s.id, s.title, s.description, a.expires_at FROM subject_access a "
                       "JOIN subjects s ON s.id=a.subject_id WHERE a.user_tg_id=? ORDER BY s.sort_order, s.id",
                       (int(tg_id),))
    return [dict(r) for r in rows if utils.deadline_active(r["expires_at"])]


def card(user: dict) -> dict:
    prem = utils.get_premium_info(user["id"])
    return {"user": user, "classes": my_classes(user["tg_id"]),
            "premium": prem if prem and utils.deadline_active(prem.get("expires_at")) else None,
            "wm": WM_MODES.get(user.get("watermark_mode") or "on"), "has_password": acc.has_password(user)}


def list_accounts(limit: int = 30) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM users WHERE study_account=1 ORDER BY study_created_at DESC LIMIT ?", (limit,))]


# ───────────────────────── приглашения тем, кто не запускал бота ─────────────────────────

def invite(username: str, subject_ids: list, premium: str, wm_mode: str, actor) -> bool:
    u = acc.normalize_username(username)
    if not re.fullmatch(r"[a-z0-9_]{4,32}", u or ""):
        return False
    db.execute("UPDATE study_invites SET status='cancelled' WHERE username=? AND status='waiting'", (u,))
    db.execute("INSERT INTO study_invites (username, subjects, premium, watermark_mode, created_by, created_at, status) "
               "VALUES (?,?,?,?,?,?, 'waiting')", (u, json.dumps([int(x) for x in subject_ids]), premium or "none",
                                                    wm_mode or "on", actor, utils.now_iso()))
    return True


def waiting_invites() -> list:
    return [dict(r) for r in db.fetchall("SELECT * FROM study_invites WHERE status='waiting' ORDER BY id DESC LIMIT 30")]


def activate_invite(user: dict) -> Optional[dict]:
    """Человек сам запустил бота: если админ готовил ему аккаунт — создать его сейчас."""
    u = acc.normalize_username(user.get("username") or "")
    if not u:
        return None
    inv = db.fetchone("SELECT * FROM study_invites WHERE username=? AND status='waiting' ORDER BY id DESC LIMIT 1", (u,))
    if not inv:
        return None
    if not db.execute("UPDATE study_invites SET status='done', done_at=?, tg_id=? WHERE id=? AND status='waiting'",
                      (utils.now_iso(), user["tg_id"], inv["id"])).rowcount:
        return None
    return create(user, json.loads(inv["subjects"] or "[]"), inv["premium"], inv["watermark_mode"], inv["created_by"],
                  source="bot")


# ───────────────────────── сообщение ученику ─────────────────────────

def welcome_text(info: dict, user: dict) -> str:
    esc = utils.escape_html
    subj = "\n".join("• " + esc(t) for t in info.get("subjects") or []) or "• пока не выбраны"
    from services.broadcast_service import site_url
    pw_line = ("🔐 Пароль: создайте по кнопке ниже (ссылка действует 7 дней)"
               if info.get("token") else "🔐 Пароль: прежний, он у вас уже есть")
    prem = utils.get_premium_info(user["id"])
    status = "PREMIUM" if prem and utils.deadline_active(prem.get("expires_at")) else "обычный"
    return ("🎉 <b>ВАШ УЧЕБНЫЙ АККАУНТ ГОТОВ!</b>\n\n"
            "Поздравляем! Администратор SMART ЕНТ открыл вам доступ к обучению.\n\n"
            f"📚 <b>Ваши предметы:</b>\n{subj}\n\n"
            f"👑 Статус: <b>{status}</b>\n"
            f"👤 Логин: <code>{esc(info['login'])}</code>\n"
            f"{pw_line}\n"
            f"🌐 Сайт: {esc(site_url())}\n\n"
            "При входе на сайт бот пришлёт одноразовый код для подтверждения.\n\n"
            "👇 ВОЙТИ В АККАУНТ")


def welcome_markup(info: dict):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from services.broadcast_service import site_url
    url = setup_url(info["token"]) if info.get("token") else f"{site_url()}/login?next=/classes"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔐 СОЗДАТЬ ПАРОЛЬ И ВОЙТИ" if info.get("token") else "🌐 ВОЙТИ В АККАУНТ", url=url)]])


async def notify(bot, user: dict, info: dict) -> bool:
    """Личное сообщение только этому ученику. Тем, кто не запускал бота, не пишем."""
    if not int(user.get("tg_id") or 0) > 0 or user.get("bot_blocked"):
        return False
    try:
        await bot.send_message(int(user["tg_id"]), welcome_text(info, user), parse_mode="HTML",
                               reply_markup=welcome_markup(info), disable_web_page_preview=True)
        return True
    except Exception as e:
        log.warning("сообщение об аккаунте %s: %s", user.get("tg_id"), e)
        return False
