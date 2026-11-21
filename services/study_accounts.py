"""Учебные аккаунты учеников (v95).

Учебный аккаунт появляется только после действия администратора «Создать
аккаунт» в боте или в админке сайта. До этого человек — обычный пользователь
бота: /start, язык и профильные предметы аккаунт не создают, сообщений о
регистрации не приходит.

Аккаунт — это тот же пользователь (users) с пометкой study_account, своим
логином и правами: выбранные предметы (subject_access, видны в «Мои классы»),
Premium по желанию админа и режим водяных знаков. Пароль ученик задаёт сам по
одноразовой ссылке; в базе хранятся только хеш пароля и отпечаток ссылки.

v101: пароль может выдать и админ (сгенерированный или написанный вручную).
Такой пароль хранится ещё и зашифрованным (ключ из SESSION_SECRET, не из базы),
чтобы админ видел его в Excel со списком аккаунтов. Как только ученик сам
меняет пароль, зашифрованная копия стирается: его личный пароль не видит никто.
Сроки доступа и предметы можно написать вручную, а не только выбрать кнопкой.

Если человек ещё не запускал бота, создаётся приглашение: бот первым не пишет,
аккаунт создаётся и сообщение уходит, когда он сам нажмёт /start.
"""
import base64
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
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
TERM_CHOICES = {"0": "Бессрочно", "30": "30 дней", "90": "90 дней", "180": "180 дней", "365": "1 год"}
DEVICE_CHOICES = {"": "Как у всех", "1": "1 устройство", "2": "2 устройства", "3": "3 устройства",
                  "0": "Без ограничения"}
PW_MODES = {"auto": "Создать автоматически", "manual": "Написать вручную", "link": "Ученик задаст сам по ссылке"}
ALMATY = timezone(timedelta(hours=5))


# ───────────────────────── сроки, написанные вручную ─────────────────────────
# Срок хранится строкой: "none" — нет, "0" — бессрочно, "45" — дней с сегодняшнего
# дня, "until:2027-09-01T18:59:59" — до конца этого дня по времени Алматы (UTC).

_UNITS = (("нед", 7), ("мес", 30), ("год", 365), ("лет", 365), ("дн", 1), ("день", 1), ("д", 1),
          ("week", 7), ("month", 30), ("year", 365), ("day", 1))
_NONE_WORDS = {"нет", "без", "без premium", "без премиум", "выкл", "отключить", "none", "no", "-", "—", "0 нет"}
_FOREVER_WORDS = {"бессрочно", "навсегда", "без срока", "бесконечно", "forever", "∞", "0"}


def parse_term(text: str, allow_none: bool = True) -> Optional[str]:
    """Срок, написанный руками: «45», «45 дней», «3 месяца», «1 год», «2 недели»,
    «до 01.09.2027», «01.09.2027», «2027-09-01», «бессрочно», «нет».
    None — не понял (или дата уже прошла)."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е")).rstrip(".")
    if not t:
        return None
    if t in _NONE_WORDS:
        return "none" if allow_none else None
    if t in _FOREVER_WORDS:
        return "0"
    t = re.sub(r"^(до|по|until)\s+", "", t)
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", t) or None
    ymd = None
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ymd = (y + 2000 if y < 100 else y, mo, d)
    else:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            ymd = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if ymd:
        try:
            end_local = datetime(ymd[0], ymd[1], ymd[2], 23, 59, 59, tzinfo=ALMATY)
        except ValueError:
            return None
        end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
        if end_utc <= datetime.utcnow():
            return None
        return "until:" + end_utc.isoformat(timespec="seconds")
    m = re.fullmatch(r"(\d{1,5})\s*([a-zа-я]*)", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    mult = 1
    if unit:
        for prefix, k in _UNITS:
            if unit.startswith(prefix):
                mult = k
                break
        else:
            return None
    days = n * mult
    if days <= 0:
        return "0"
    return str(min(days, 36500))


def term_expires(spec: str) -> Optional[str]:
    """Дата окончания (UTC) для срока; None — бессрочно."""
    spec = str(spec or "0")
    if spec.startswith("until:"):
        return spec[6:]
    days = int(spec) if spec.isdigit() else 0
    if days <= 0:
        return None
    return (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")


def term_label(spec, none_text: str = "нет") -> str:
    spec = str(spec if spec is not None else "0")
    if spec in ("none", ""):
        return none_text
    if spec.startswith("until:"):
        return "до " + local_date(spec[6:])
    days = int(spec) if spec.isdigit() else 0
    if days <= 0:
        return "бессрочно"
    return {365: "1 год"}.get(days, f"{days} дн.")


def local_date(iso) -> str:
    """Дата по Алматы для показа: 01.09.2027."""
    dt = utils.parse_utc_naive(iso) if iso else None
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(ALMATY).strftime("%d.%m.%Y")


def parse_subjects(text: str) -> tuple:
    """Предметы, написанные вручную: номера из списка, названия (можно начало
    слова), «все». Возвращает (id выбранных, нераспознанные куски)."""
    subs = subjects()
    ids, bad = [], []
    for part in re.split(r"[,;\n]+", text or ""):
        p = part.strip().strip(".").lower().replace("ё", "е")
        if not p:
            continue
        if p in ("все", "всё", "all", "все предметы"):
            ids = [s["id"] for s in subs]
            continue
        found = None
        if p.isdigit() and 1 <= int(p) <= len(subs):
            found = subs[int(p) - 1]["id"]
        else:
            norm = lambda x: (x or "").lower().replace("ё", "е").strip()
            exact = [s["id"] for s in subs if norm(s["title"]) == p]
            starts = [s["id"] for s in subs if norm(s["title"]).startswith(p)]
            inside = [s["id"] for s in subs if p in norm(s["title"])]
            for group in (exact, starts, inside):
                if len(group) == 1:
                    found = group[0]
                    break
        if found is None:
            bad.append(part.strip())
        elif found not in ids:
            ids.append(found)
    return ids, bad


def parse_device_limit(text: str) -> Optional[str]:
    """«как у всех» → "", «без лимита» → "0", число 1..50 → "N". None — не понял."""
    t = (text or "").strip().lower().replace("ё", "е")
    if t in ("как у всех", "общий", "по умолчанию", "стандарт", ""):
        return ""
    if t in ("без лимита", "без ограничения", "безлимит", "бесконечно", "∞", "0", "нет"):
        return "0"
    m = re.fullmatch(r"(\d{1,2})( [a-zа-я]+)?", t)
    if m and 1 <= int(m.group(1)) <= 50:
        return str(int(m.group(1)))
    return None


def device_label(spec) -> str:
    spec = "" if spec is None else str(spec)
    return DEVICE_CHOICES.get(spec) or f"{spec} устр."


# ───────────────────────── пароль, который видит админ ─────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    secret = config.SESSION_SECRET or ("smartent-session::" + (config.BOT_TOKEN or ""))
    key = base64.urlsafe_b64encode(hashlib.sha256(("study-pw-v1|" + secret).encode()).digest())
    return Fernet(key)


def encrypt_pw(pw: str) -> str:
    return _fernet().encrypt(pw.encode()).decode()


def decrypt_pw(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        return None          # ключ сменили (SESSION_SECRET) — пароль надо выдать заново


_PW_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def gen_password(length: int = 10) -> str:
    """Удобный для передачи пароль: без похожих символов (0/O, 1/l/I)."""
    while True:
        pw = "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def password_problem(pw: str) -> str:
    """Почему пароль, написанный админом, не подходит; пусто — подходит."""
    pw = pw or ""
    if len(pw) < acc.MIN_PASSWORD:
        return f"Пароль должен быть не короче {acc.MIN_PASSWORD} символов."
    if len(pw) > 64:
        return "Пароль слишком длинный: не больше 64 символов."
    if any(c.isspace() for c in pw):
        return "В пароле не должно быть пробелов."
    return ""


def set_admin_password(user: dict, pw: str = "", actor=None, source: str = "bot", end_sessions: bool = True) -> str:
    """Пароль от админа: ставится сразу, зашифрованная копия — для Excel. Возвращает пароль.
    end_sessions — прежние входы на сайте заканчиваются (смена пароля, не первое создание)."""
    pw = pw or gen_password()
    acc.set_password(user["id"], pw)              # прежний пароль перестаёт действовать
    db.execute("UPDATE users SET study_pw_enc=? WHERE id=?", (encrypt_pw(pw), user["id"]))
    if end_sessions:
        db.execute("UPDATE users SET admin_session_reset=COALESCE(admin_session_reset,0)+1 WHERE id=?", (user["id"],))
        try:
            from services import devices as _dev
            _dev.end_all(user["tg_id"], "reset")
        except Exception as e:
            log.warning("выход на устройствах после смены пароля: %s", e)
    acc.log_event(user["id"], user.get("tg_id"), "study_password_admin")
    _audit(actor, source, "settings", user, "новый пароль от админа")
    return pw


def admin_password(user: dict) -> Optional[str]:
    """Пароль, который выдал админ, если ученик его ещё не менял."""
    return decrypt_pw(user.get("study_pw_enc") or "")


def password_state(user: dict) -> str:
    """Что писать в колонке «Пароль»."""
    pw = admin_password(user)
    if pw:
        return pw
    if user.get("study_pw_enc"):
        return "не расшифровать: выдайте новый"
    if acc.has_password(user):
        return "задал ученик (скрыт)"
    return "ещё не задан"


# ───────────────────────── поиск ─────────────────────────

def find_user(ident: str, by_login: bool = False) -> Optional[dict]:
    """Пользователь бота по @username или Telegram ID (только реальные, с Telegram).
    by_login=True — ещё и по логину учебного аккаунта (для «Изменить настройки»)."""
    raw = (ident or "").strip()
    if raw.lstrip("-").isdigit():
        row = db.fetchone("SELECT * FROM users WHERE tg_id=?", (int(raw),))
        if not row and by_login:
            row = db.fetchone("SELECT * FROM users WHERE study_login=? AND tg_id>0", (raw,))
    else:
        u = acc.normalize_username(raw)
        row = None
        if u and by_login:
            row = db.fetchone("SELECT * FROM users WHERE study_login=? AND tg_id>0", (u,))
        if not row and u:
            row = db.fetchone("SELECT * FROM users WHERE LOWER(username)=? AND tg_id>0 "
                              "ORDER BY tg_linked DESC, id LIMIT 1", (u,))
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

def _grant_subjects(user: dict, subject_ids: list, actor, term: Optional[str] = None) -> list:
    """term=None — бессрочно, а действующий доступ не трогаем; term задан — ставим этот срок всем."""
    titles = []
    for sid in subject_ids:
        s = db.fetchone("SELECT id, title FROM subjects WHERE id=?", (int(sid),))
        if not s:
            continue
        cur = db.fetchone("SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?", (s["id"], user["tg_id"]))
        if term is not None and term != "none":
            db.execute("INSERT INTO subject_access (subject_id, user_tg_id, expires_at, granted_by_admin) VALUES (?,?,?,?) "
                       "ON CONFLICT(subject_id, user_tg_id) DO UPDATE SET expires_at=excluded.expires_at, "
                       "granted_by_admin=excluded.granted_by_admin",
                       (s["id"], user["tg_id"], term_expires(term), actor))
        elif cur and utils.deadline_active(cur["expires_at"]):
            pass                                        # уже есть действующий доступ — не трогаем срок
        else:
            db.execute("INSERT INTO subject_access (subject_id, user_tg_id, expires_at, granted_by_admin) VALUES (?,?,NULL,?) "
                       "ON CONFLICT(subject_id, user_tg_id) DO UPDATE SET expires_at=NULL, granted_by_admin=excluded.granted_by_admin",
                       (s["id"], user["tg_id"], actor))
        titles.append(s["title"])
    return titles


def apply_premium(user: dict, choice: str, actor) -> str:
    """choice — срок: "none", "0" (бессрочно), число дней или "until:<дата UTC>"."""
    if choice in (None, "", "none"):
        return ""
    choice = str(choice)
    if choice.startswith("until:"):
        expires = choice[6:]
        if db.fetchone("SELECT 1 FROM premium_users WHERE user_id=?", (user["id"],)):
            db.execute("UPDATE premium_users SET expires_at=?, granted_at=?, granted_by_admin=?, notified_expired=0 "
                       "WHERE user_id=?", (expires, utils.now_iso(), actor or 0, user["id"]))
        else:
            db.execute("INSERT INTO premium_users (user_id, expires_at, granted_by_admin, notified_expired) VALUES (?,?,?,0)",
                       (user["id"], expires, actor or 0))
        try:
            from services import reward_service as _rs
            _rs.check_user(user["tg_id"])
        except Exception as e:
            log.warning("premium check_user: %s", e)
        return term_label(choice)
    days = int(choice) if choice.isdigit() else 0
    utils.grant_premium(user["id"], days, actor or 0)
    try:
        from services import reward_service as _rs
        _rs.check_user(user["tg_id"])
    except Exception as e:
        log.warning("premium check_user: %s", e)
    return "бессрочно" if days == 0 else f"{days} дней"


def create(user: dict, subject_ids: list, premium: str = "none", wm_mode: str = "on", actor=None,
           source: str = "bot", *, term: Optional[str] = None, devices: Optional[str] = None,
           pw_mode: str = "link", password: str = "") -> dict:
    """Создать (или дополнить) учебный аккаунт. Повторное создание дубликата не делает.

    term — срок доступа к предметам (None — бессрочно), devices — лимит устройств
    ("" — как у всех), pw_mode: auto — пароль создаётся сразу, manual — пароль
    от админа (password), link — ученик задаёт сам по ссылке (как раньше)."""
    fresh = not user.get("study_account")
    login = user.get("study_login") or _free_login(user)
    db.execute("UPDATE users SET study_account=1, study_login=?, study_status='active', "
               "study_created_at=COALESCE(study_created_at, ?), study_created_by=COALESCE(study_created_by, ?), "
               "watermark_mode=? WHERE id=?",
               (login, utils.now_iso(), actor, wm_mode if wm_mode in WM_MODES else "on", user["id"]))
    if devices is not None:
        set_device_limit(user, devices, actor, source, quiet=True)
    titles = _grant_subjects(user, subject_ids, actor, term)
    prem = apply_premium(user, premium, actor)
    pw, token = "", None
    if pw_mode in ("auto", "manual") and (fresh or not acc.has_password(user) or password):
        pw = set_admin_password(user, password if pw_mode == "manual" else "", actor, source, end_sessions=not fresh)
    elif fresh or not acc.has_password(user):
        token = issue_setup_link(user["id"])
    acc.log_event(user["id"], user["tg_id"], "study_account_created" if fresh else "study_account_updated",
                  f"предметы: {', '.join(titles)}")
    _audit(actor, source, "create" if fresh else "save", user,
           f"предметы: {', '.join(titles)}; срок: {term_label(term)}; Premium: {prem or 'нет'}")
    fresh_user = db.fetchone("SELECT * FROM users WHERE id=?", (user["id"],))
    return {"login": login, "token": token, "password": pw, "subjects": titles, "premium": prem, "fresh": fresh,
            "term": term_label(term), "has_password": acc.has_password(dict(fresh_user) if fresh_user else user)}


def set_subjects(user: dict, subject_ids: list, actor, source: str = "bot", term: Optional[str] = None) -> list:
    """Ровно эти предметы в «Моих классах»: лишние доступы снимаются."""
    keep = {int(x) for x in subject_ids}
    for r in db.fetchall("SELECT subject_id FROM subject_access WHERE user_tg_id=?", (user["tg_id"],)):
        if r["subject_id"] not in keep:
            db.execute("DELETE FROM subject_access WHERE subject_id=? AND user_tg_id=?", (r["subject_id"], user["tg_id"]))
    titles = _grant_subjects(user, sorted(keep), actor, term)
    _audit(actor, source, "subject_grant", user, ", ".join(titles) or "без предметов")
    return titles


def set_term(user: dict, term: str, actor, source: str = "bot") -> int:
    """Один срок для всех предметов ученика. Число предметов."""
    rows = db.fetchall("SELECT subject_id FROM subject_access WHERE user_tg_id=?", (user["tg_id"],))
    exp = term_expires(term)
    for r in rows:
        db.execute("UPDATE subject_access SET expires_at=?, granted_by_admin=? WHERE subject_id=? AND user_tg_id=?",
                   (exp, actor, r["subject_id"], user["tg_id"]))
    _audit(actor, source, "subject_grant", user, "срок доступа к предметам: " + term_label(term))
    return len(rows)


def set_device_limit(user: dict, spec: str, actor, source: str = "bot", quiet: bool = False) -> None:
    """"" — как у всех, "0" — без ограничения, число — свой лимит."""
    spec = "" if spec is None else str(spec)
    val = None if spec == "" else max(0, min(int(spec), 50))
    db.execute("UPDATE users SET device_limit=? WHERE id=?", (val, user["id"]))
    if not quiet:
        _audit(actor, source, "settings", user, "лимит устройств: " + device_label(spec))


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
            "wm": WM_MODES.get(user.get("watermark_mode") or "on"), "has_password": acc.has_password(user),
            "admin_pw": admin_password(user), "pw_state": password_state(user),
            "devices": device_label("" if user.get("device_limit") is None else str(user.get("device_limit")))}


def list_accounts(limit: int = 30) -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM users WHERE study_account=1 ORDER BY study_created_at DESC LIMIT ?", (limit,))]


def counts() -> dict:
    c = lambda sql: (db.fetchone(sql) or {"c": 0})["c"]
    return {"all": c("SELECT COUNT(*) AS c FROM users WHERE study_account=1"),
            "active": c("SELECT COUNT(*) AS c FROM users WHERE study_account=1 AND COALESCE(study_status,'active')='active'"),
            "disabled": c("SELECT COUNT(*) AS c FROM users WHERE study_account=1 AND study_status='disabled'"),
            "waiting": c("SELECT COUNT(*) AS c FROM study_invites WHERE status='waiting'")}


def delete_account(user: dict, actor, source: str = "bot", with_premium: bool = False) -> int:
    """Удалить учебный аккаунт в корзину: логин, пароль и предметы «Моих классов».
    Пользователь бота, его баллы, прогресс и результаты остаются. Из корзины всё
    возвращается как было. Номер записи в корзине (0 — нечего удалять)."""
    from services import trash
    title = f"{user.get('study_login') or ''} · " + (("@" + user["username"]) if user.get("username")
                                                     else (user.get("first_name") or str(user["tg_id"])))
    item = trash.move(
        "account", title, actor, source, [],
        extra=(("subject_access", "user_tg_id=?", (user["tg_id"],)),),
        updates=(("users", user["id"], {"study_account": 0, "study_login": None, "study_status": None,
                                        "password_hash": None, "study_pw_enc": None}),),
        meta={"location": "Учебные аккаунты"}, note="ученик остаётся пользователем бота")
    if not item:
        return 0
    db.execute("UPDATE users SET session_epoch=COALESCE(session_epoch,0)+1, "
               "admin_session_reset=COALESCE(admin_session_reset,0)+1 WHERE id=?", (user["id"],))
    db.execute("UPDATE account_setup_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
               (utils.now_iso(), user["id"]))
    try:
        from services import devices as _dev
        _dev.end_all(user["tg_id"], "reset")
    except Exception as e:
        log.warning("выход на устройствах при удалении аккаунта: %s", e)
    if with_premium and utils.get_premium_info(user["id"]):
        trash.trash_premium(user["id"], actor, source)
    acc.log_event(user["id"], user["tg_id"], "study_account_deleted")
    return item


def cancel_invite(username: str) -> int:
    u = acc.normalize_username(username)
    return db.execute("UPDATE study_invites SET status='cancelled' WHERE username=? AND status='waiting'", (u,)).rowcount


# ───────────────────────── Excel со всеми аккаунтами ─────────────────────────

XLSX_HEADERS = ["№", "Логин", "Пароль", "Telegram", "Telegram ID", "Имя", "Статус", "Предметы",
                "Срок предметов", "Premium", "Водяные знаки", "Лимит устройств", "Устройств сейчас",
                "Создан", "Последний вход"]


def export_rows() -> list:
    rows = []
    users = [dict(r) for r in db.fetchall(
        "SELECT * FROM users WHERE study_account=1 ORDER BY study_created_at, id")]
    try:
        from services import devices as _dev
    except Exception:
        _dev = None
    for n, u in enumerate(users, start=1):
        classes = my_classes(u["tg_id"])
        terms = sorted({("бессрочно" if not c["expires_at"] else "до " + local_date(c["expires_at"])) for c in classes})
        prem = utils.get_premium_info(u["id"])
        if prem and utils.deadline_active(prem.get("expires_at")):
            prem_txt = "бессрочно" if not prem.get("expires_at") else "до " + local_date(prem["expires_at"])
        else:
            prem_txt = "нет"
        try:
            n_dev = len(_dev.sessions(u["tg_id"])) if _dev else ""
        except Exception:
            n_dev = ""
        try:
            from services import activity as _act
            last = (_act.summary(u["tg_id"]).get("last_login") or {}).get("ts") or ""
        except Exception:
            last = ""
        rows.append([n, u.get("study_login") or "", password_state(u),
                     ("@" + u["username"]) if u.get("username") else "", u["tg_id"],
                     " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x),
                     "активен" if (u.get("study_status") or "active") == "active" else "отключён",
                     ", ".join(c["title"] for c in classes) or "нет", ", ".join(terms),
                     prem_txt, WM_MODES.get(u.get("watermark_mode") or "on", ""),
                     device_label("" if u.get("device_limit") is None else str(u["device_limit"])), n_dev,
                     local_date(u.get("study_created_at")), last])
    for inv in db.fetchall("SELECT * FROM study_invites WHERE status='waiting' ORDER BY id"):
        inv = dict(inv)
        try:
            ids = json.loads(inv.get("subjects") or "[]")
        except Exception:
            ids = []
        titles = [s["title"] for s in subjects() if s["id"] in ids]
        rows.append([len(rows) + 1, "", decrypt_pw(inv.get("pw_enc") or "") or "появится после /start",
                     "@" + inv["username"], "", "", "ждёт запуска бота", ", ".join(titles) or "нет",
                     term_label(inv.get("term") or "0"), term_label(inv.get("premium") or "none"),
                     WM_MODES.get(inv.get("watermark_mode") or "on", ""),
                     device_label(inv.get("device_limit") or ""), "", local_date(inv.get("created_at")), ""])
    return rows


def export_xlsx() -> bytes:
    from services import xlsx_writer
    return xlsx_writer.build(XLSX_HEADERS, export_rows(), "Аккаунты")


def export_name() -> str:
    return "accounts_" + datetime.now(ALMATY).strftime("%Y-%m-%d_%H-%M") + ".xlsx"


# ───────────────────────── приглашения тем, кто не запускал бота ─────────────────────────

def invite(username: str, subject_ids: list, premium: str, wm_mode: str, actor, *, term: Optional[str] = None,
           devices: Optional[str] = None, pw_mode: str = "link", password: str = "") -> bool:
    u = acc.normalize_username(username)
    if not re.fullmatch(r"[a-z0-9_]{4,32}", u or ""):
        return False
    db.execute("UPDATE study_invites SET status='cancelled' WHERE username=? AND status='waiting'", (u,))
    if pw_mode == "auto":
        password = gen_password()                   # видно в Excel сразу, ставится при /start
    db.execute("INSERT INTO study_invites (username, subjects, premium, watermark_mode, created_by, created_at, status, "
               "term, device_limit, pw_mode, pw_enc) VALUES (?,?,?,?,?,?, 'waiting', ?,?,?,?)",
               (u, json.dumps([int(x) for x in subject_ids]), premium or "none", wm_mode or "on", actor, utils.now_iso(),
                term, devices, pw_mode if pw_mode in PW_MODES else "link",
                encrypt_pw(password) if password and pw_mode in ("auto", "manual") else None))
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
    inv = dict(inv)
    pw = decrypt_pw(inv.get("pw_enc") or "") or ""
    mode = inv.get("pw_mode") or "link"
    if mode in ("auto", "manual"):
        # пароль придуман заранее (он уже в Excel) — ставим именно его;
        # копию не прочитать (сменили SESSION_SECRET) — придумаем новый
        mode = "manual" if pw else "auto"
    return create(user, json.loads(inv["subjects"] or "[]"), inv["premium"], inv["watermark_mode"], inv["created_by"],
                  source="bot", term=inv.get("term"), devices=inv.get("device_limit"), pw_mode=mode, password=pw)


# ───────────────────────── сообщение ученику ─────────────────────────

def welcome_text(info: dict, user: dict) -> str:
    esc = utils.escape_html
    subj = "\n".join("• " + esc(t) for t in info.get("subjects") or []) or "• пока не выбраны"
    from services.broadcast_service import site_url
    if info.get("password"):
        pw_line = (f"🔐 Пароль: <code>{esc(info['password'])}</code>\n"
                   "Сменить пароль можно в профиле на сайте.")
    else:
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
