"""Специальные ссылки на урок (v97).

Общая — для всех, у кого есть ссылка; личная — одному ученику; для группы —
списку учеников; у любой может быть срок и предел активаций (сколько разных
людей могут ею воспользоваться). Ссылка с «открывает урок» выдаёт доступ к этому
уроку тому, кто по ней пришёл (lesson_access с source='link', при желании на N
дней), даже если предмет закрыт. Без этой отметки ссылка только ведёт к уроку,
а доступ проверяется как обычно. Другие уроки предмета ссылка не открывает.
"""
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import config
import database as db
import utils

KINDS = {"general": "Общая", "personal": "Личная", "group": "Для группы"}


def _code() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        c = "".join(secrets.choice(alphabet) for _ in range(10))
        if not db.fetchone("SELECT 1 FROM lesson_links WHERE code=?", (c,)):
            return c


def _people(raw: str) -> list:
    """@username и Telegram ID через пробел или запятую → список tg_id и имён (кто ещё не запускал бота)."""
    out = []
    for ident in re.split(r"[\s,;]+", raw or ""):
        ident = ident.strip()
        if not ident:
            continue
        if ident.lstrip("-").isdigit():
            out.append(ident)
        else:
            out.append("@" + ident.lstrip("@").lower())
    return out


def create(lesson_id: int, kind: str, actor, *, label: str = "", people: str = "", grant: bool = True,
           access_days: int = 0, max_uses: int = 0, expires: str = "") -> dict:
    if kind not in KINDS:
        return {"error": "Неизвестный вид ссылки."}
    allowed = _people(people) if kind in ("personal", "group") else []
    if kind == "personal" and len(allowed) != 1:
        return {"error": "Для личной ссылки укажите одного ученика (@username или Telegram ID)."}
    if kind == "group" and not allowed:
        return {"error": "Для ссылки группы укажите учеников."}
    exp = (expires or "").strip()[:10]
    if exp and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
        return {"error": "Дата окончания как 2026-12-31."}
    code = _code()
    lid = db.execute(
        "INSERT INTO lesson_links (lesson_id, code, kind, label, allowed, grant_access, access_days, max_uses, "
        "expires_at, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(lesson_id), code, kind, (label or KINDS[kind])[:120], " ".join(allowed), 1 if grant else 0,
         max(0, int(access_days or 0)), max(0, int(max_uses or 0)), (exp + "T23:59:59") if exp else None,
         actor, utils.now_iso())).lastrowid
    return {"id": lid, "code": code}


def revoke(link_id: int) -> None:
    db.execute("UPDATE lesson_links SET revoked=1 WHERE id=?", (int(link_id),))


def site_url(code: str) -> str:
    from services.broadcast_service import site_url as _s
    return f"{_s()}/l/{code}"


def app_url(code: str) -> str:
    short = (getattr(config, "WEB_APP_SHORT_NAME", "") or "").strip()
    base = f"https://t.me/{config.WEB_BOT_USERNAME}" + (f"/{short}" if short else "")
    return f"{base}?startapp=l_{code}"


def for_lesson(lesson_id: int) -> list:
    out = []
    for r in db.fetchall("SELECT l.*, (SELECT COUNT(*) FROM lesson_link_uses u WHERE u.link_id=l.id) AS uses "
                         "FROM lesson_links l WHERE l.lesson_id=? ORDER BY l.id DESC", (int(lesson_id),)):
        d = dict(r)
        d["site_url"], d["app_url"] = site_url(d["code"]), app_url(d["code"])
        d["kind_title"] = KINDS.get(d["kind"], d["kind"])
        d["state"] = state(d)
        out.append(d)
    return out


def state(link: dict) -> str:
    if link.get("revoked"):
        return "отозвана"
    if link.get("expires_at") and link["expires_at"] <= utils.now_iso():
        return "срок истёк"
    if link.get("max_uses") and int(link.get("uses") or 0) >= int(link["max_uses"]):
        return "активации закончились"
    return "действует"


def _allowed(link: dict, user: dict) -> bool:
    if link["kind"] == "general":
        return True
    names = (link.get("allowed") or "").split()
    uname = "@" + (user.get("username") or "").lower() if user.get("username") else None
    return str(user["tg_id"]) in names or (uname is not None and uname in names)


def open_link(code: str, tg_id: int) -> dict:
    """Ученик открыл ссылку. {"lesson_id"} — куда вести, или {"error"} с понятной причиной."""
    link = db.fetchone("SELECT * FROM lesson_links WHERE code=?", ((code or "").strip().lower(),))
    if not link:
        return {"error": "Ссылка не найдена. Проверьте, что скопировали её целиком."}
    link = dict(link)
    user = utils.get_user_by_tg(tg_id)
    if not user:
        return {"error": "Аккаунт не найден."}
    used = db.fetchone("SELECT * FROM lesson_link_uses WHERE link_id=? AND user_tg=?", (link["id"], tg_id))
    if link["revoked"]:
        return {"error": "Ссылка отозвана администратором."}
    if not _allowed(link, user):
        return {"error": "Эта ссылка выдана другому ученику."}
    if not used:
        if link.get("expires_at") and link["expires_at"] <= utils.now_iso():
            return {"error": "Срок действия ссылки закончился."}
        n = db.fetchone("SELECT COUNT(*) AS c FROM lesson_link_uses WHERE link_id=?", (link["id"],))["c"]
        if link["max_uses"] and n >= link["max_uses"]:
            return {"error": "По этой ссылке уже зашло максимальное число учеников."}
        cur = db.execute("INSERT OR IGNORE INTO lesson_link_uses (link_id, user_tg, used_at, last_at) VALUES (?,?,?,?)",
                         (link["id"], tg_id, utils.now_iso(), utils.now_iso()))
        if cur.rowcount and link["grant_access"]:
            exp = None
            if link["access_days"]:
                exp = (datetime.utcnow() + timedelta(days=int(link["access_days"]))).isoformat(timespec="seconds")
            from webapp import shortcuts as sc
            real = sc.orig_lesson_id(link["lesson_id"])
            row = db.fetchone("SELECT id, source FROM lesson_access WHERE lesson_id=? AND user_tg_id=?", (real, tg_id))
            if not row:
                db.execute("INSERT INTO lesson_access (lesson_id, user_tg_id, granted_by_admin, source, expires_at) "
                           "VALUES (?,?,?, 'link', ?)", (real, tg_id, link["created_by"], exp))
            elif row["source"] == "link":
                db.execute("UPDATE lesson_access SET expires_at=? WHERE id=?", (exp, row["id"]))
    else:
        db.execute("UPDATE lesson_link_uses SET opens=opens+1, last_at=? WHERE id=?", (utils.now_iso(), used["id"]))
    return {"lesson_id": link["lesson_id"]}


def link_access(lesson_id: int, tg_id: Optional[int]) -> bool:
    """Доступ к одному уроку по ссылке: действует, даже если предмет закрыт."""
    if not tg_id:
        return False
    from webapp import shortcuts as sc
    r = db.fetchone("SELECT expires_at FROM lesson_access WHERE lesson_id=? AND user_tg_id=? AND source='link'",
                    (sc.orig_lesson_id(int(lesson_id)), int(tg_id)))
    return bool(r) and utils.deadline_active(r["expires_at"])
