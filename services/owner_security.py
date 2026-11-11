"""Вход владельца на сайте по паролю (v92).

Скрытая комбинация только открывает окно входа и прав не даёт. Всё решает сервер:
пароль (хранится только хеш), ограничение попыток, необязательный код в Telegram,
отдельный срок бездействия для такого входа и отзыв других сеансов.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

import config
import database as db
import utils
from services import account_service as acc

MIN_LEN = 10
FAIL_WINDOW_MIN = 15
FAIL_PER_IP = 5           # неверных попыток с одного адреса за окно
FAIL_GLOBAL = 25          # всего за окно, если адреса подменяют
RESET_TTL_MIN = 15
IDLE_DEFAULT = 120
GATE_TAPS = 7
GATE_SECONDS = 5


def _setting(key: str, default: int) -> int:
    row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    try:
        return int(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def _set_setting(key: str, value: int) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(int(value))))


def gate_config() -> dict:
    return {"taps": _setting("owner_gate_taps", GATE_TAPS), "seconds": _setting("owner_gate_seconds", GATE_SECONDS)}


def set_gate(taps: int, seconds: int) -> None:
    _set_setting("owner_gate_taps", max(3, min(int(taps), 20)))
    _set_setting("owner_gate_seconds", max(2, min(int(seconds), 30)))


def gate_matches(stamps: list) -> bool:
    """Отметки нажатий в миллисекундах. Проверка на сервере: в коде страницы комбинации нет."""
    cfg = gate_config()
    if not isinstance(stamps, list) or len(stamps) < cfg["taps"] or len(stamps) > 200:
        return False
    try:
        last = [float(x) for x in stamps[-cfg["taps"]:]]
    except (TypeError, ValueError):
        return False
    return 0 <= last[-1] - last[0] <= cfg["seconds"] * 1000


def _row(tg_id: int) -> dict:
    r = db.fetchone("SELECT * FROM owner_security WHERE tg_id=?", (int(tg_id),))
    return dict(r) if r else {}


def has_password(tg_id: int) -> bool:
    return bool(_row(tg_id).get("pw_hash"))


def password_problem(new: str, repeat: Optional[str] = None) -> str:
    if repeat is not None and new != repeat:
        return "Новый пароль и повтор не совпадают."
    if len(new or "") < MIN_LEN:
        return f"Пароль слишком короткий: нужно не меньше {MIN_LEN} символов."
    if len(new) > 200:
        return "Пароль слишком длинный."
    return ""


def set_password(tg_id: int, new: str) -> None:
    """Прежний пароль сразу перестаёт работать: хеш заменяется."""
    h = acc.hash_password(new)
    db.execute(
        "INSERT INTO owner_security (tg_id, pw_hash, pw_set_at) VALUES (?,?,?) "
        "ON CONFLICT(tg_id) DO UPDATE SET pw_hash=excluded.pw_hash, pw_set_at=excluded.pw_set_at",
        (int(tg_id), h, utils.now_iso()))


def check_password(tg_id: int, password: str) -> bool:
    return acc.verify_password(password or "", _row(tg_id).get("pw_hash") or "")


def find_owner(password: str) -> Optional[int]:
    """Чей это пароль. Считаем только тех, кто сейчас владелец в настройках сервера."""
    found = None
    rows = db.fetchall("SELECT tg_id, pw_hash FROM owner_security WHERE pw_hash IS NOT NULL")
    for r in rows:
        if acc.verify_password(password or "", r["pw_hash"]) and utils.is_owner(r["tg_id"]):
            found = r["tg_id"]
    if not rows:
        # Время ответа не должно выдавать, что пароль ещё не задан
        acc.verify_password(password or "", acc.hash_password("-"))
    return found


def log_attempt(tg_id, ip: str, ua: str, ok: bool, reason: str = "") -> None:
    db.execute("INSERT INTO owner_login_log (ts, tg_id, ip, ua, ok, reason) VALUES (?,?,?,?,?,?)",
               (utils.now_iso(), tg_id, (ip or "")[:64], (ua or "")[:200], 1 if ok else 0, reason[:60]))
    db.execute("DELETE FROM owner_login_log WHERE id <= (SELECT MAX(id) FROM owner_login_log) - 2000")


def _since() -> str:
    return (datetime.utcnow() - timedelta(minutes=FAIL_WINDOW_MIN)).isoformat(timespec="seconds")


def locked(ip: str) -> bool:
    per_ip = db.fetchone("SELECT COUNT(*) AS c FROM owner_login_log WHERE ok=0 AND ts>=? AND ip=?",
                         (_since(), (ip or "")[:64]))["c"]
    total = db.fetchone("SELECT COUNT(*) AS c FROM owner_login_log WHERE ok=0 AND ts>=?", (_since(),))["c"]
    return per_ip >= FAIL_PER_IP or total >= FAIL_GLOBAL


def history(limit: int = 50) -> list:
    return [dict(r) for r in db.fetchall("SELECT * FROM owner_login_log ORDER BY id DESC LIMIT ?", (limit,))]


def session_state(tg_id: int) -> dict:
    r = _row(tg_id)
    return {"ver": int(r.get("session_ver") or 1), "idle_min": int(r.get("idle_min") or IDLE_DEFAULT)}


def end_other_sessions(tg_id: int) -> int:
    db.execute("INSERT INTO owner_security (tg_id, session_ver) VALUES (?, 2) "
               "ON CONFLICT(tg_id) DO UPDATE SET session_ver=COALESCE(session_ver,1)+1", (int(tg_id),))
    return session_state(tg_id)["ver"]


def twofa_on(tg_id: int) -> bool:
    return bool(_row(tg_id).get("twofa"))


def set_twofa(tg_id: int, on: bool) -> None:
    db.execute("UPDATE owner_security SET twofa=? WHERE tg_id=?", (1 if on else 0, int(tg_id)))


def set_idle(tg_id: int, minutes: int) -> None:
    db.execute("INSERT INTO owner_security (tg_id, idle_min) VALUES (?,?) "
               "ON CONFLICT(tg_id) DO UPDATE SET idle_min=excluded.idle_min",
               (int(tg_id), max(5, min(int(minutes), 24 * 60))))


def _tok_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def issue_reset(tg_id: int) -> str:
    """Одноразовая ссылка на новый пароль. Выдаётся только в бота самому владельцу."""
    db.execute("UPDATE owner_reset_tokens SET used_at=? WHERE tg_id=? AND used_at IS NULL",
               (utils.now_iso(), int(tg_id)))
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO owner_reset_tokens (token_hash, tg_id, expires_at) VALUES (?,?,?)",
               (_tok_hash(token), int(tg_id),
                (datetime.utcnow() + timedelta(minutes=RESET_TTL_MIN)).isoformat(timespec="seconds")))
    return token


def reset_owner(token: str) -> Optional[int]:
    r = db.fetchone("SELECT * FROM owner_reset_tokens WHERE token_hash=?", (_tok_hash(token),))
    if not r or r["used_at"] or (r["expires_at"] or "") <= utils.now_iso() or not utils.is_owner(r["tg_id"]):
        return None
    return r["tg_id"]


def use_reset(token: str, new: str) -> bool:
    tg = reset_owner(token)
    if not tg:
        return False
    cur = db.execute("UPDATE owner_reset_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                     (utils.now_iso(), _tok_hash(token)))
    if cur.rowcount != 1:
        return False
    set_password(tg, new)
    end_other_sessions(tg)
    return True
