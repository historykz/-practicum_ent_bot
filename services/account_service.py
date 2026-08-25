"""
Единый аккаунт для сайта, бота и мини-приложения.

Главный принцип: как только Telegram привязан, человека узнаём по Telegram ID,
а не по username — username в Telegram можно поменять в любой момент, и
аккаунт от этого теряться не должен.

Аккаунт на сайте можно завести и до всякого Telegram: тогда у него временный
отрицательный номер вместо tg_id и tg_linked=0. Как только человек откроет
мини-приложение или напишет боту, настоящий Telegram ID подставится на место
временного, а все данные — Премиум, прогресс, конспекты — останутся при нём.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta

import database as db

log = logging.getLogger(__name__)

# --- пароли ---
PBKDF2_ROUNDS = 240_000
MIN_PASSWORD = 8
GEN_LENGTH = 16

# --- защита от перебора ---
MAX_LOGIN_FAILS = 5
LOCKOUT_MINUTES = 15

# --- одноразовые коды ---
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_SECONDS = 60

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def log_event(user_id, tg_id, event: str, details: str = "") -> None:
    """Журнал безопасности. Ни паролей, ни кодов здесь не сохраняем."""
    try:
        db.execute("INSERT INTO auth_events (user_id, tg_id, event, details) "
                   "VALUES (?,?,?,?)", (user_id, tg_id, event, (details or "")[:300]))
    except Exception as e:
        log.debug("auth_events: %s", e)


# ===================== username =====================

def normalize_username(raw: str) -> str:
    """@Student_123 → student_123. Регистр и @ не должны плодить дубли."""
    u = (raw or "").strip()
    if u.startswith("https://t.me/"):
        u = u[len("https://t.me/"):]
    if u.startswith("t.me/"):
        u = u[len("t.me/"):]
    return u.lstrip("@").strip().lower()


def username_valid(u: str) -> bool:
    return bool(_USERNAME_RE.match(u or ""))


def find_by_username(username: str):
    u = normalize_username(username)
    if not u:
        return None
    row = db.fetchone("SELECT * FROM users WHERE LOWER(username)=? ORDER BY "
                      "tg_linked DESC, id ASC LIMIT 1", (u,))
    return dict(row) if row else None


def find_by_tg(tg_id: int):
    row = db.fetchone("SELECT * FROM users WHERE tg_id=?", (int(tg_id),))
    return dict(row) if row else None


# ===================== пароли =====================

def generate_password(length: int = GEN_LENGTH) -> str:
    """Сложный пароль: разный регистр, цифры, спецзнаки — и точно все сразу."""
    lower, upper = string.ascii_lowercase, string.ascii_uppercase
    digits, marks = string.digits, "!@#$%^&*-_=+?"
    alphabet = lower + upper + digits + marks
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(max(12, length)))
        if (any(c in lower for c in pw) and any(c in upper for c in pw)
                and any(c in digits for c in pw) and any(c in marks for c in pw)):
            return pw


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 со случайной солью. Открытых паролей не храним."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def password_strong_enough(pw: str) -> bool:
    return bool(pw) and len(pw) >= MIN_PASSWORD


def set_password(user_id: int, password: str) -> None:
    """Поставить новый пароль. Старый перестаёт работать сразу.

    Заодно поднимаем «эпоху» сессий: этим можно завершить все прежние входы
    на сайте, если пароль меняли из-за утечки.
    """
    db.execute("UPDATE users SET password_hash=?, password_set_at=?, "
               "login_fail_count=0, lockout_until=NULL, "
               "session_epoch=COALESCE(session_epoch,0)+1 WHERE id=?",
               (hash_password(password), _now(), user_id))
    db.execute("UPDATE login_codes SET used_at=? WHERE user_id=? AND used_at IS NULL",
               (_now(), user_id))
    log_event(user_id, None, "password_changed")


def has_password(user: dict) -> bool:
    return bool((user or {}).get("password_hash"))


# ===================== регистрация на сайте =====================

def register_web(username: str, password: str) -> dict:
    """Ручная регистрация: Telegram username + свой пароль.

    Нажимать /start в боте заранее не нужно — Telegram ID подставится потом,
    при первом заходе в мини-приложение или при первом сообщении боту.
    """
    u = normalize_username(username)
    if not username_valid(u):
        return {"ok": False, "error":
                "Username должен быть от 4 до 32 символов: латиница, цифры и _"}
    if not password_strong_enough(password):
        return {"ok": False, "error":
                f"Пароль должен быть не короче {MIN_PASSWORD} символов"}

    existing = find_by_username(u)
    if existing:
        if has_password(existing):
            return {"ok": False, "error":
                    "Аккаунт с таким username уже зарегистрирован. Войдите или "
                    "воспользуйтесь восстановлением пароля."}
        # Человек уже писал боту, но пароля не заводил — это его же аккаунт
        set_password(existing["id"], password)
        log_event(existing["id"], existing.get("tg_id"), "password_set_on_existing")
        return {"ok": True, "user": find_by_username(u), "linked_existing": True}

    # свободный отрицательный номер — место для будущего Telegram ID
    row = db.fetchone("SELECT MIN(tg_id) AS m FROM users WHERE tg_id < 0")
    placeholder = int((row or {}).get("m") or 0) - 1 if (row and row.get("m")) else -1
    db.execute("INSERT INTO users (tg_id, username, tg_linked) VALUES (?,?,0)",
               (placeholder, u))
    user = find_by_tg(placeholder)
    set_password(user["id"], password)
    log_event(user["id"], None, "registered_web", f"username={u}")
    return {"ok": True, "user": find_by_tg(placeholder)}


# ===================== привязка Telegram =====================

def link_telegram(tg_id: int, username: str = "", first_name: str = "",
                  last_name: str = "") -> dict:
    """Найти или завести аккаунт по данным Telegram.

    Порядок поиска: сперва по Telegram ID, и только если его ещё нет —
    по username, один раз, чтобы подхватить аккаунт, заведённый на сайте.
    """
    tg_id = int(tg_id)
    uname = normalize_username(username)

    # 1) знакомый Telegram ID — сразу он
    user = find_by_tg(tg_id)
    if user:
        # username мог смениться: обновляем, аккаунт тот же
        if uname and (user.get("username") or "").lower() != uname:
            db.execute("UPDATE users SET username=? WHERE id=?", (uname, user["id"]))
            log_event(user["id"], tg_id, "username_updated",
                      f"{user.get('username')} → {uname}")
        if not user.get("tg_linked"):
            db.execute("UPDATE users SET tg_linked=1, tg_linked_at=? WHERE id=?",
                       (_now(), user["id"]))
        return {"ok": True, "user": find_by_tg(tg_id), "action": "found_by_tg"}

    # 2) аккаунт с сайта под тем же username и ещё без Telegram
    if uname:
        candidate = find_by_username(uname)
        if candidate:
            if candidate.get("tg_linked") and candidate.get("tg_id") != tg_id:
                # username занят другим живым аккаунтом — молча не сливаем
                log_event(candidate["id"], tg_id, "link_conflict",
                          f"username={uname} занят tg={candidate.get('tg_id')}")
                return {"ok": False, "conflict": "username_taken",
                        "user": _create_fresh(tg_id, uname, first_name, last_name),
                        "action": "created_due_to_conflict"}
            db.execute("UPDATE users SET tg_id=?, tg_linked=1, tg_linked_at=?, "
                       "username=? WHERE id=?",
                       (tg_id, _now(), uname or candidate.get("username"),
                        candidate["id"]))
            log_event(candidate["id"], tg_id, "telegram_linked", f"username={uname}")
            return {"ok": True, "user": find_by_tg(tg_id), "action": "linked_by_username"}

    # 3) никого не нашли — заводим новый аккаунт
    return {"ok": True, "user": _create_fresh(tg_id, uname, first_name, last_name),
            "action": "created"}


def _create_fresh(tg_id: int, uname: str, first_name: str = "", last_name: str = "") -> dict:
    db.execute(
        "INSERT OR IGNORE INTO users (tg_id, username, first_name, last_name, "
        "tg_linked, tg_linked_at) VALUES (?,?,?,?,1,?)",
        (int(tg_id), uname or None, first_name or None, last_name or None, _now()))
    user = find_by_tg(tg_id)
    log_event(user["id"] if user else None, tg_id, "account_created_telegram")
    return user


def ensure_password_for_miniapp(user: dict) -> str:
    """Автоматический пароль для аккаунта, созданного в мини-приложении.

    Возвращает пароль ОДИН раз — сразу после создания, чтобы показать его
    человеку. В базе остаётся только хэш, поэтому «подсмотреть» этот пароль
    потом нельзя: в профиле вместо показа даётся кнопка сделать новый.
    """
    if has_password(user):
        return ""
    pw = generate_password()
    set_password(user["id"], pw)
    log_event(user["id"], user.get("tg_id"), "password_autogenerated")
    return pw


# ===================== вход по паролю =====================

def _locked(user: dict) -> int:
    """Сколько секунд осталось до конца блокировки. 0 — не заблокирован."""
    until = user.get("lockout_until")
    if not until:
        return 0
    try:
        left = (datetime.fromisoformat(until) - datetime.utcnow()).total_seconds()
    except ValueError:
        return 0
    return int(left) if left > 0 else 0


def check_login(username: str, password: str) -> dict:
    """Первый шаг входа с сайта: username + пароль.

    Ответы намеренно одинаковы для «нет такого аккаунта» и «неверный пароль» —
    чтобы по форме входа нельзя было собирать список существующих username.
    """
    bad = {"ok": False, "error": "Неверный username или пароль"}
    user = find_by_username(username)
    if not user or not has_password(user):
        return bad

    left = _locked(user)
    if left:
        return {"ok": False, "error":
                f"Слишком много попыток. Попробуйте через {max(1, left // 60)} мин."}

    if not verify_password(password, user["password_hash"]):
        fails = int(user.get("login_fail_count") or 0) + 1
        if fails >= MAX_LOGIN_FAILS:
            db.execute("UPDATE users SET login_fail_count=0, lockout_until=? WHERE id=?",
                       ((datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES))
                        .isoformat(timespec="seconds"), user["id"]))
            log_event(user["id"], user.get("tg_id"), "login_locked")
            return {"ok": False, "error":
                    f"Слишком много попыток. Вход закрыт на {LOCKOUT_MINUTES} мин."}
        db.execute("UPDATE users SET login_fail_count=? WHERE id=?", (fails, user["id"]))
        log_event(user["id"], user.get("tg_id"), "login_failed")
        return bad

    db.execute("UPDATE users SET login_fail_count=0, lockout_until=NULL WHERE id=?",
               (user["id"],))
    user = find_by_username(username)
    # Telegram привязан — дальше нужен одноразовый код в боте
    if user.get("tg_linked") and int(user.get("tg_id") or 0) > 0:
        return {"ok": True, "need_code": True, "user": user}
    log_event(user["id"], user.get("tg_id"), "login_password_only")
    return {"ok": True, "need_code": False, "user": user}


# ===================== одноразовые коды =====================

def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def can_issue_code(user_id: int, purpose: str = "login") -> int:
    """0 — можно выдавать. Иначе сколько секунд подождать."""
    row = db.fetchone(
        "SELECT created_at FROM login_codes WHERE user_id=? AND purpose=? "
        "ORDER BY id DESC LIMIT 1", (user_id, purpose))
    if not row or not row.get("created_at"):
        return 0
    try:
        age = (datetime.utcnow() - datetime.fromisoformat(
            str(row["created_at"]).replace(" ", "T"))).total_seconds()
    except ValueError:
        return 0
    left = CODE_RESEND_SECONDS - age
    return int(left) if left > 0 else 0


def issue_code(user_id: int, purpose: str = "login") -> str:
    """Выдать новый код. Прежние сразу перестают действовать."""
    db.execute("UPDATE login_codes SET used_at=? WHERE user_id=? AND purpose=? "
               "AND used_at IS NULL", (_now(), user_id, purpose))
    code = f"{secrets.randbelow(10 ** 6):06d}"
    db.execute(
        "INSERT INTO login_codes (user_id, purpose, code_hash, expires_at) "
        "VALUES (?,?,?,?)",
        (user_id, purpose, _hash_code(code),
         (datetime.utcnow() + timedelta(minutes=CODE_TTL_MINUTES))
         .isoformat(timespec="seconds")))
    log_event(user_id, None, f"code_issued_{purpose}")
    return code


def check_code(user_id: int, code: str, purpose: str = "login") -> dict:
    row = db.fetchone(
        "SELECT * FROM login_codes WHERE user_id=? AND purpose=? AND used_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (user_id, purpose))
    if not row:
        return {"ok": False, "error": "Код не запрашивался или уже использован"}
    row = dict(row)
    try:
        expired = datetime.fromisoformat(row["expires_at"]) < datetime.utcnow()
    except ValueError:
        expired = True
    if expired:
        db.execute("UPDATE login_codes SET used_at=? WHERE id=?", (_now(), row["id"]))
        return {"ok": False, "error": "Срок действия кода истёк — запросите новый"}
    if int(row.get("attempts") or 0) >= CODE_MAX_ATTEMPTS:
        db.execute("UPDATE login_codes SET used_at=? WHERE id=?", (_now(), row["id"]))
        return {"ok": False, "error": "Слишком много попыток — запросите новый код"}

    if not hmac.compare_digest(_hash_code((code or "").strip()), row["code_hash"]):
        db.execute("UPDATE login_codes SET attempts=COALESCE(attempts,0)+1 WHERE id=?",
                   (row["id"],))
        log_event(user_id, None, "code_wrong")
        return {"ok": False, "error": "Неверный код"}

    db.execute("UPDATE login_codes SET used_at=? WHERE id=?", (_now(), row["id"]))
    log_event(user_id, None, f"code_ok_{purpose}")
    return {"ok": True}


# ===================== сброс пароля =====================

def can_reset_via_telegram(user: dict) -> bool:
    """Слать код в Telegram можно, только если он точно привязан."""
    return bool(user and user.get("tg_linked") and int(user.get("tg_id") or 0) > 0)


def reset_to_new_password(user_id: int) -> str:
    """Выдать новый сгенерированный пароль. Старый перестаёт работать."""
    pw = generate_password()
    set_password(user_id, pw)
    log_event(user_id, None, "password_reset")
    return pw


def logout_everywhere(user_id: int) -> None:
    db.execute("UPDATE users SET session_epoch=COALESCE(session_epoch,0)+1 WHERE id=?",
               (user_id,))
    log_event(user_id, None, "logout_all")


def account_summary(tg_id: int) -> dict:
    """Что показать человеку в профиле про вход с сайта."""
    user = find_by_tg(tg_id) or {}
    return {
        "username": user.get("username") or "",
        "has_password": has_password(user),
        "tg_linked": bool(user.get("tg_linked")),
        "password_set_at": (user.get("password_set_at") or "")[:10],
    }
