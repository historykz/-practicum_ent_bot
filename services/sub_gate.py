"""
Обязательная подписка для участников групп.

Админ выбирает основные группы и каналы (или группы), на которые участник
обязан подписаться. Пока человек подписан не на всё, писать в группе он не
может: бот удаляет любое его сообщение, ограничивает право писать и один раз
показывает кнопки с нужными каналами и «✅ Проверить подписку». Как только
Telegram подтверждает подписку на всё — ограничение снимается само.

Главные правила:
  • человек определяется только по Telegram user_id, канал и группа — по chat_id;
  • если бот не может проверить канал (нет прав, канал удалён, сбой Telegram),
    участников из-за этого НЕ блокируем — пишем в журнал и предупреждаем админа;
  • ограничение снимаем только то, которое поставил сам бот: мут модератора
    не трогаем;
  • успешная проверка запоминается на время (по умолчанию 10 минут), чтобы не
    упираться в лимиты Telegram; отписку бот видит сразу по событию канала,
    если он администратор канала, иначе — при следующей проверке;
  • статистика ведётся по подтверждённым Telegram фактам и уникальным людям:
    нажатия кнопки «Проверить» сами по себе ничего не засчитывают.
"""
import asyncio
import csv
import io
import json
import logging
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape as _xesc

import database as db

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))
SERVICE_IDS = {777000, 1087968824, 136817688}   # Telegram, анонимный админ, «от имени канала»
OK_STATUSES = {"member", "administrator", "creator"}

# ───────────────────────── настройки ─────────────────────────

DEFAULTS = {
    # Сколько секунд помним, что человек подписан. Раньше — 10 минут: отписавшийся
    # успевал писать всё это время. Теперь Telegram спрашиваем заново не реже чем
    # раз в 30 секунд, а отписку от канала (бот — админ канала) видим сразу.
    "gate_ok_ttl_sec": 30,
    "gate_notice_ttl_min": 10,       # уведомление без подписки висит в группе
    "gate_notice_cooldown_sec": 60,  # не чаще одного уведомления человеку
    "gate_success_delete_sec": 30,   # после успешной проверки уведомление уходит
    "gate_recheck_min": 10,          # перепроверка ограниченных участников
    # Строгий режим: канал, который бот не может проверить, НЕ считается
    # пройденным. Раньше такой канал молча пропускался — и писать мог любой.
    "gate_strict": 1,
    "gate_debug_log": 1,             # журнал решения по каждому сообщению
}
SETTING_TITLES = {
    "gate_ok_ttl_sec": ("Переспрашивать Telegram о подписке не реже чем раз в", "с"),
    "gate_notice_ttl_min": ("Уведомление висит в группе", "мин"),
    "gate_notice_cooldown_sec": ("Пауза между уведомлениями человеку", "с"),
    "gate_success_delete_sec": ("Убрать уведомление после подписки через", "с"),
    "gate_recheck_min": ("Перепроверять ограниченных каждые", "мин"),
    "gate_strict": ("Если канал нельзя проверить", ""),
    "gate_debug_log": ("Журнал проверки каждого сообщения", ""),
}
SETTING_CHOICES = {
    "gate_ok_ttl_sec": [15, 30, 60, 120, 300],
    "gate_notice_ttl_min": [2, 5, 10, 30, 60],
    "gate_notice_cooldown_sec": [30, 60, 120, 300],
    "gate_success_delete_sec": [10, 30, 60, 300],
    "gate_recheck_min": [5, 10, 30, 60],
    "gate_strict": [1, 0],
    "gate_debug_log": [1, 0],
}
BOOL_LABELS = {
    "gate_strict": {1: "⛔ не пускать (строго)", 0: "⚠️ пропускать"},
    "gate_debug_log": {1: "вести", 0: "не вести"},
}
_settings_cache = {"at": 0.0, "values": {}}


def _load_settings() -> dict:
    out = dict(DEFAULTS)
    try:
        rows = db.fetchall("SELECT key, value FROM settings WHERE key LIKE 'gate_%'")
        for r in rows:
            if r["key"] in DEFAULTS:
                try:
                    out[r["key"]] = int(float(r["value"]))
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        log.warning("настройки подписки: %s", e)
    return out


def setting(key: str) -> int:
    now = time.monotonic()
    if now - _settings_cache["at"] > 30 or not _settings_cache["values"]:
        _settings_cache["values"] = _load_settings()
        _settings_cache["at"] = now
    return int(_settings_cache["values"].get(key, DEFAULTS.get(key, 0)))


def set_setting(key: str, value: int) -> None:
    if key not in DEFAULTS:
        raise ValueError(key)
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(int(value))))
    _settings_cache["at"] = 0.0


# ───────────────────────── время ─────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime = None) -> str:
    dt = dt or now_utc()
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_iso(value) -> datetime:
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None


def local_day(value=None) -> str:
    dt = parse_iso(value) if value else now_utc()
    return (dt or now_utc()).astimezone(ALMATY).strftime("%Y-%m-%d")


def fmt_local(value) -> str:
    dt = parse_iso(value)
    return dt.astimezone(ALMATY).strftime("%d.%m.%Y %H:%M") if dt else ""


# ───────────────────────── настройки групп и каналов ─────────────────────────

def channel_url(ch: dict) -> str:
    uname = (ch.get("username") or "").lstrip("@")
    if uname:
        return f"https://t.me/{uname}"
    return ch.get("invite_link") or ""


def list_channels(include_deleted: bool = False) -> list:
    sql = "SELECT * FROM gate_channels"
    if not include_deleted:
        sql += " WHERE COALESCE(is_deleted,0)=0"
    return [dict(r) for r in db.fetchall(sql + " ORDER BY sort_order, id")]


def get_channel(channel_id: int) -> dict:
    r = db.fetchone("SELECT * FROM gate_channels WHERE id=?", (int(channel_id),))
    return dict(r) if r else None


def get_channel_by_chat(chat_id: int) -> dict:
    r = db.fetchone("SELECT * FROM gate_channels WHERE chat_id=?", (int(chat_id),))
    return dict(r) if r else None


def save_channel(info: dict, admin_id: int = None) -> dict:
    """Добавить канал (или вернуть удалённый ранее) — по его Telegram chat_id."""
    cid = int(info["chat_id"])
    old = get_channel_by_chat(cid)
    stamp = iso()
    if old:
        db.execute("UPDATE gate_channels SET username=?, title=?, invite_link=?, kind=?, is_deleted=0, "
                   "is_active=1, updated_at=? WHERE id=?",
                   (info.get("username") or "", info.get("title") or old.get("title") or "",
                    info.get("invite_link") or old.get("invite_link") or "", info.get("kind") or "channel",
                    stamp, old["id"]))
        ch_id = old["id"]
    else:
        nxt = db.fetchone("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM gate_channels")
        ch_id = db.execute(
            "INSERT INTO gate_channels (chat_id, username, title, invite_link, kind, sort_order, created_by, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, info.get("username") or "", info.get("title") or "", info.get("invite_link") or "",
             info.get("kind") or "channel", int(nxt["n"] if nxt else 1), admin_id, stamp)).lastrowid
    invalidate()
    return get_channel(ch_id)


def update_channel(channel_id: int, **fields) -> None:
    allowed = {"title", "invite_link", "is_active", "all_groups", "check_state", "check_error",
               "checked_at", "username", "is_deleted", "disabled_reason"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    sets["updated_at"] = iso()
    db.execute(f"UPDATE gate_channels SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
               tuple(sets.values()) + (int(channel_id),))
    invalidate()


def delete_channel(channel_id: int) -> None:
    """Убрать из списка. Статистика по каналу остаётся."""
    update_channel(channel_id, is_deleted=1, is_active=0)


def move_channel(channel_id: int, delta: int) -> None:
    chans = list_channels()
    ids = [c["id"] for c in chans]
    if channel_id not in ids:
        return
    i = ids.index(channel_id)
    j = max(0, min(len(ids) - 1, i + delta))
    if i == j:
        return
    ids[i], ids[j] = ids[j], ids[i]
    for pos, cid in enumerate(ids, 1):
        db.execute("UPDATE gate_channels SET sort_order=? WHERE id=?", (pos, cid))
    invalidate()


def known_groups() -> list:
    """Группы, где есть бот, + настройки проверки."""
    rows = db.fetchall(
        "SELECT k.chat_id, k.title, k.type, k.is_bot_admin, g.enabled, g.rights_state, g.rights_missing, "
        "g.can_restrict, g.checked_at FROM known_groups k LEFT JOIN gate_groups g ON g.chat_id=k.chat_id "
        "WHERE k.type IN ('group','supergroup') ORDER BY COALESCE(g.enabled,0) DESC, k.title")
    try:
        from services import chat_migration as _cm
        moved = {str(k) for k in _cm._map()}
    except Exception:
        moved = set()
    # Группа стала супергруппой: её старый ID мёртвый. Раньше он оставался в списке рядом
    # с новым под тем же названием — включить проверку можно было не в той группе
    out = [dict(r) for r in rows if str(r["chat_id"]) not in moved]
    seen = {r["chat_id"] for r in out}
    for r in db.fetchall("SELECT * FROM gate_groups"):   # группа с настройкой, но бот её забыл
        if r["chat_id"] not in seen and str(r["chat_id"]) not in moved:
            d = dict(r)
            d.setdefault("type", "supergroup")
            out.append(d)
    return out


def get_group(chat_id: int) -> dict:
    r = db.fetchone("SELECT * FROM gate_groups WHERE chat_id=?", (int(chat_id),))
    if r:
        return dict(r)
    k = db.fetchone("SELECT chat_id, title FROM known_groups WHERE chat_id=?", (int(chat_id),))
    return {"chat_id": int(chat_id), "title": (k["title"] if k else ""), "enabled": 0,
            "rights_state": "unknown", "rights_missing": "", "can_restrict": 1}


def _upsert_group(chat_id: int, **fields) -> None:
    title = fields.pop("title", None)
    if title is None:
        k = db.fetchone("SELECT title FROM known_groups WHERE chat_id=?", (int(chat_id),))
        title = k["title"] if k else ""
    db.execute("INSERT INTO gate_groups (chat_id, title, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(chat_id) DO UPDATE SET title=CASE WHEN excluded.title<>'' THEN excluded.title "
               "ELSE gate_groups.title END, updated_at=excluded.updated_at", (int(chat_id), title or "", iso()))
    if fields:
        db.execute(f"UPDATE gate_groups SET {', '.join(k + '=?' for k in fields)} WHERE chat_id=?",
                   tuple(fields.values()) + (int(chat_id),))
    invalidate()


def set_group_enabled(chat_id: int, enabled: bool) -> None:
    _upsert_group(chat_id, enabled=1 if enabled else 0, enabled_at=iso() if enabled else None)


def set_link(group_chat_id: int, channel_chat_id: int, applies: bool) -> None:
    db.execute("INSERT INTO gate_links (group_chat_id, channel_chat_id, applies) VALUES (?,?,?) "
               "ON CONFLICT(group_chat_id, channel_chat_id) DO UPDATE SET applies=excluded.applies",
               (int(group_chat_id), int(channel_chat_id), 1 if applies else 0))
    invalidate()


def list_exempt() -> list:
    return [dict(r) for r in db.fetchall(
        "SELECT e.*, u.username, u.first_name FROM gate_exempt e LEFT JOIN users u ON u.tg_id=e.tg_id "
        "ORDER BY e.created_at DESC")]


def add_exempt(tg_id: int, note: str = "", admin_id: int = None) -> None:
    db.execute("INSERT INTO gate_exempt (tg_id, note, added_by) VALUES (?,?,?) "
               "ON CONFLICT(tg_id) DO UPDATE SET note=excluded.note", (int(tg_id), note or "", admin_id))
    invalidate()


def remove_exempt(tg_id: int) -> None:
    db.execute("DELETE FROM gate_exempt WHERE tg_id=?", (int(tg_id),))
    invalidate()


# Снимок настроек в памяти: проверка каждого сообщения не ходит в базу
_snap = {"at": 0.0, "groups": {}, "channels": [], "links": {}, "exempt": set(), "admins": set()}
SNAP_TTL = 20.0


def _load_snapshot() -> dict:
    groups = {int(r["chat_id"]): dict(r) for r in db.fetchall("SELECT * FROM gate_groups WHERE enabled=1")}
    channels = [dict(r) for r in db.fetchall(
        "SELECT * FROM gate_channels WHERE COALESCE(is_deleted,0)=0 AND COALESCE(is_active,1)=1 "
        "ORDER BY sort_order, id")]
    links = {(int(r["group_chat_id"]), int(r["channel_chat_id"])): int(r["applies"])
             for r in db.fetchall("SELECT * FROM gate_links")}
    exempt = {int(r["tg_id"]) for r in db.fetchall("SELECT tg_id FROM gate_exempt")}
    admins = set()
    try:
        import config
        admins = {int(x) for x in (getattr(config, "ADMIN_IDS", None) or [])}
        admins |= {int(r["tg_id"]) for r in db.fetchall("SELECT tg_id FROM admins")}
    except Exception:
        pass
    return {"at": time.monotonic(), "groups": groups, "channels": channels, "links": links,
            "exempt": exempt, "admins": admins}


def snapshot(force: bool = False) -> dict:
    """Синхронно (вызывать в рабочем потоке)."""
    global _snap
    if force or time.monotonic() - _snap["at"] > SNAP_TTL:
        _snap = _load_snapshot()
    return _snap


async def asnapshot() -> dict:
    if time.monotonic() - _snap["at"] > SNAP_TTL:
        return await asyncio.to_thread(snapshot, True)
    return _snap


def invalidate() -> None:
    """Настройки поменялись: снимок и решения по людям считаем заново."""
    _snap["at"] = 0.0
    _verdicts.clear()


def channels_for(snap: dict, group_chat_id: int) -> list:
    out = []
    for ch in snap["channels"]:
        applies = snap["links"].get((int(group_chat_id), int(ch["chat_id"])))
        if applies is None:
            applies = int(ch.get("all_groups") or 0)
        if applies and int(ch["chat_id"]) != int(group_chat_id):
            out.append(ch)
    return out


def groups_for_channel(snap: dict, channel_chat_id: int) -> list:
    return [g for g in snap["groups"]
            if any(int(c["chat_id"]) == int(channel_chat_id) for c in channels_for(snap, g))]


# ───────────────────────── Telegram ─────────────────────────

def _raw():
    from services import autopub_service as aps
    return aps


async def _call(coro, timeout: float = 10.0):
    return await asyncio.wait_for(coro, timeout)


def _err_text(e) -> str:
    return f"{type(e).__name__}: {str(e)[:160]}"


_NOT_MEMBER = ("user not found", "participant_id_invalid", "user_not_participant", "member not found",
               "user_id_invalid")


def _is_transient(e) -> bool:
    try:
        return _raw()._transient(e)
    except Exception:
        return isinstance(e, asyncio.TimeoutError)


async def member_status(bot, chat_ref, user_id: int) -> dict:
    return await _call(_raw().raw_get_chat_member(bot, chat_ref, int(user_id)))


def membership_from(m: dict):
    """Ответ getChatMember → подписан ли. Подписан: creator, administrator, member и restricted
    с is_member=true. Не подписан: left, kicked (бан), restricted с is_member=false."""
    st = str(m.get("status") or "")
    if st in OK_STATUSES:
        return True
    if st == "restricted":
        return bool(m.get("is_member"))
    return False


async def check_member(bot, ch: dict, user_id: int, info: dict = None):
    """True — подписан, False — Telegram подтвердил, что нет, None — проверить не удалось.
    В info складывается ответ Telegram для журнала: status, is_member, ошибка."""
    info = info if info is not None else {}
    for attempt in (1, 2):
        try:
            m = await member_status(bot, ch["chat_id"], user_id)
        except Exception as e:
            low = str(e).lower()
            if any(k in low for k in _NOT_MEMBER):
                info.update(status="not_found", is_member=False)
                return False, ""
            err = _err_text(e)
            if attempt == 1 and _is_transient_text(err):
                await asyncio.sleep(0.4)          # сбой связи — одна быстрая повторная попытка
                continue
            info.update(status="error", error=err)
            return None, err
        info.update(status=str(m.get("status") or ""), is_member=m.get("is_member"))
        return membership_from(m), ""
    return None, "нет ответа"


_admins = {}            # chat_id → (monotonic, {user_id})
ADMINS_TTL = 600.0


async def group_admins(bot, chat_id: int) -> set:
    hit = _admins.get(int(chat_id))
    if hit and time.monotonic() - hit[0] < ADMINS_TTL:
        return hit[1]
    try:
        rows = await _call(bot(_RawGetChatAdministrators(chat_id=int(chat_id))))
        ids = {int((r.get("user") or {}).get("id") or 0) for r in (rows or [])}
    except Exception as e:
        log.info("подписка: список админов %s: %s", chat_id, e)
        return hit[1] if hit else None
    _admins[int(chat_id)] = (time.monotonic(), ids)
    return ids


def forget_admins(chat_id: int) -> None:
    _admins.pop(int(chat_id), None)


from typing import Any as _Any, List as _List, Union as _Union          # noqa: E402
from aiogram.methods.base import TelegramMethod as _TgMethod              # noqa: E402


class _RawGetChatAdministrators(_TgMethod[_List[_Any]]):
    """Список админов словарями: новые поля Telegram не ломают разбор."""
    __returning__ = _List[_Any]
    __api_method__ = "getChatAdministrators"

    chat_id: _Union[int, str]


def _perms(allow: bool):
    from aiogram.types import ChatPermissions
    return ChatPermissions(**{f: allow for f in ChatPermissions.model_fields})


async def restrict(bot, chat_id: int, user_id: int):
    try:
        await _call(bot.restrict_chat_member(int(chat_id), int(user_id), permissions=_perms(False),
                                             use_independent_chat_permissions=True))
        return True, ""
    except Exception as e:
        return False, _err_text(e)


async def unrestrict(bot, chat_id: int, user_id: int):
    """«Все права = True» — так Telegram снимает ограничения: дальше действуют общие правила чата."""
    try:
        await _call(bot.restrict_chat_member(int(chat_id), int(user_id), permissions=_perms(True),
                                             use_independent_chat_permissions=True))
        return True, ""
    except Exception as e:
        return False, _err_text(e)


async def safe_delete(bot, chat_id: int, message_id: int, warn: bool = False) -> bool:
    try:
        await _call(bot.delete_message(int(chat_id), int(message_id)), 8)
        return True
    except Exception as e:
        low = str(e).lower()
        if "message to delete not found" not in low and "message can't be deleted" not in low:
            log.info("подписка: не удалил сообщение %s/%s: %s", chat_id, message_id, e)
        if warn and "message to delete not found" not in low:
            title = (_snap["groups"].get(int(chat_id)) or {}).get("title") or chat_id
            await notify_admins(
                bot, f"delete:{chat_id}",
                f"⚠️ Обязательная подписка: бот не может удалять сообщения в группе «{_h(title)}». "
                f"Дайте ему право администратора «{RIGHT_TITLES['can_delete_messages']}».\n\n<i>{_h(_err_text(e))}</i>")
        return False


# ───────────────────────── уведомления админу ─────────────────────────

_notified = {}


async def notify_admins(bot, key: str, text: str, cooldown: int = 6 * 3600) -> bool:
    last = _notified.get(key, 0)
    if time.monotonic() - last < cooldown and last:
        return False
    _notified[key] = time.monotonic()
    try:
        import config
        ids = list(dict.fromkeys(int(x) for x in (getattr(config, "ADMIN_IDS", None) or [])))
    except Exception:
        ids = []
    sent = False
    for aid in ids:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
            sent = True
        except Exception:
            pass
    return sent


def _h(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_err_written = {}


async def report_channel_error(bot, ch: dict, err: str) -> None:
    # Сломанный канал при оживлённой группе дал бы запись на каждое сообщение:
    # пишем в журнал не чаще раза в 5 минут на канал
    last = _err_written.get(ch["chat_id"], 0)
    if last and time.monotonic() - last < 300:
        return
    _err_written[ch["chat_id"]] = time.monotonic()
    await asyncio.to_thread(_mark_channel, ch["id"], "error", err)
    await asyncio.to_thread(_event, None, None, ch["chat_id"], "check_error", err[:300])
    strict = bool(setting("gate_strict"))
    tail = ("🔴 Включён строгий режим: пока бот не может проверить этот канал, сообщения участников "
            "в группах с этим каналом удаляются." if strict else
            "Режим «пропускать»: участников из-за этого канала бот сейчас не проверяет.")
    await notify_admins(
        bot, f"ch:{ch['chat_id']}",
        f"⚠️ Не удалось проверить обязательную подписку на канал «{_h(ch.get('title') or ch['chat_id'])}». "
        f"Проверьте права бота: он должен быть администратором канала.\n\n<i>{_h(err)}</i>\n\n{tail}",
        cooldown=1800 if strict else 6 * 3600)


def _mark_channel(channel_id: int, state: str, err: str = "") -> None:
    row = db.fetchone("SELECT check_state, check_error FROM gate_channels WHERE id=?", (int(channel_id),))
    if row and row["check_state"] == state and (row["check_error"] or "") == (err or ""):
        db.execute("UPDATE gate_channels SET checked_at=? WHERE id=?", (iso(), int(channel_id)))
        return
    db.execute("UPDATE gate_channels SET check_state=?, check_error=?, checked_at=? WHERE id=?",
               (state, err or "", iso(), int(channel_id)))
    _snap["at"] = 0.0


# ───────────────────────── права бота ─────────────────────────

NOT_ADMIN = "Бот не администратор группы"
RIGHT_TITLES = {
    "can_delete_messages": "Удаление сообщений",
    "can_restrict_members": "Блокировка участников (ограничение и изменение их разрешений)",
}


async def check_group_rights(bot, chat_id: int) -> dict:
    """Что умеет бот в основной группе. Сохраняет итог для админки."""
    res = {"ok": False, "missing": [], "error": "", "title": "", "supergroup": True, "is_admin": False}
    try:
        chat = await _call(_raw().raw_get_chat(bot, int(chat_id)))
        me = await member_status(bot, int(chat_id), bot.id)
    except Exception as e:
        res["error"] = _err_text(e)
        await asyncio.to_thread(_upsert_group, chat_id, rights_state="error",
                                rights_missing=res["error"], checked_at=iso())
        return res
    res["title"] = chat.get("title") or ""
    res["supergroup"] = chat.get("type") != "group"
    status = str(me.get("status") or "")
    res["is_admin"] = status in ("administrator", "creator")
    if status == "creator":
        missing = []
    elif status != "administrator":
        missing = list(RIGHT_TITLES.values())
        res["error"] = NOT_ADMIN
    else:
        missing = [title for key, title in RIGHT_TITLES.items() if not me.get(key)]
    res["missing"] = missing
    res["ok"] = not missing and not res["error"]
    state = "ok" if res["ok"] else "missing"
    text = res["error"] or "; ".join(missing)
    if not res["supergroup"]:
        text = (text + "; " if text else "") + "обычная группа: ограничить право писать нельзя, бот будет только удалять сообщения"
    await asyncio.to_thread(_upsert_group, chat_id, title=res["title"], rights_state=state, rights_missing=text,
                            can_restrict=1 if res["supergroup"] else 0, checked_at=iso())
    forget_admins(chat_id)
    return res


async def resolve_resource(bot, ref) -> dict:
    """Канал или группа по @username, ссылке t.me/…, ID или пересланному посту."""
    ref = str(ref or "").strip()
    m = re.match(r"^(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?:s/)?([A-Za-z0-9_]{4,})/?(?:\d+)?$", ref)
    if m:
        ref = "@" + m.group(1)
    elif re.match(r"^(?:https?://)?t\.me/(\+|joinchat/)", ref):
        return {"error": "По пригласительной ссылке Telegram не сообщает боту, что это за канал. "
                         "Перешлите сюда любой пост из канала или отправьте его ID (-100…)."}
    elif re.match(r"^@?[A-Za-z][A-Za-z0-9_]{3,}$", ref):
        ref = "@" + ref.lstrip("@")
    elif not re.match(r"^-?\d{5,}$", ref):
        return {"error": "Не понял, что это за канал. Пришлите @username, ссылку t.me/…, ID (-100…) "
                         "или перешлите пост из канала."}
    try:
        chat = await _call(_raw().raw_get_chat(bot, ref))
    except Exception as e:
        return {"error": f"Бот не видит этот канал или группу: {_err_text(e)}. Добавьте бота туда "
                         "администратором и попробуйте снова."}
    ctype = str(chat.get("type") or "")
    if ctype == "private":
        return {"error": "Это личный чат, а не канал или группа."}
    return {"chat_id": int(chat["id"]), "title": chat.get("title") or "", "username": chat.get("username") or "",
            "kind": "channel" if ctype == "channel" else "group", "invite_link": chat.get("invite_link") or ""}


CANT_CHECK = ("❌ Бот не может проверять подписку на этот канал. Добавьте бота в канал и выдайте "
              "необходимые права.")


async def check_channel_access(bot, ch: dict, probe_user_id: int = None) -> dict:
    """Может ли бот проверять подписчиков этого канала: статус бота в канале и пробный
    getChatMember по живому человеку (тому, кто нажал кнопку). Итог — в карточку канала."""
    res = {"ok": False, "text": "", "probe": None}
    try:
        me = await member_status(bot, ch["chat_id"], bot.id)
        status = str(me.get("status") or "")
    except Exception as e:
        res["text"] = f"Бот не видит {'канал' if ch.get('kind') == 'channel' else 'группу'}: {_err_text(e)}"
        await asyncio.to_thread(_mark_channel, ch["id"], "error", res["text"])
        return res
    if ch.get("kind") == "channel":
        if status in ("administrator", "creator"):
            res["ok"] = True
        else:
            res["text"] = ("Сделайте бота администратором канала. Права можно оставить минимальные: "
                           "без этого Telegram не даёт боту видеть подписчиков.")
    else:
        if status in ("administrator", "creator", "member") or (status == "restricted" and me.get("is_member")):
            res["ok"] = True
        else:
            res["text"] = "Добавьте бота в эту группу (лучше администратором), иначе он не видит участников."
    if res["ok"] and probe_user_id:
        info = {}
        verdict, err = await check_member(bot, ch, probe_user_id, info)
        if verdict is None:
            res["ok"] = False
            res["text"] = f"Пробная проверка участника не прошла: {err}"
        else:
            res["probe"] = {"subscribed": verdict, **info}
    await asyncio.to_thread(_mark_channel, ch["id"], "ok" if res["ok"] else "error", res["text"])
    if res["ok"]:
        _err_written.pop(ch["chat_id"], None)
        row = await asyncio.to_thread(get_channel, ch["id"])
        if row and not row.get("is_active") and (row.get("disabled_reason") or "") == "check":
            # Канал выключался только потому, что бот не мог его проверять — теперь включаем
            await asyncio.to_thread(update_channel, ch["id"], is_active=1, disabled_reason="")
            res["activated"] = True
    return res


async def make_invite_link(bot, ch: dict) -> str:
    """Ссылка для приватного канала: Telegram даёт её боту-администратору."""
    try:
        chat = await _call(_raw().raw_get_chat(bot, ch["chat_id"]))
        if chat.get("invite_link"):
            return chat["invite_link"]
    except Exception:
        pass
    try:
        link = await _call(bot.create_chat_invite_link(int(ch["chat_id"]), name="Обязательная подписка"))
        return link.invite_link
    except Exception as e:
        log.info("подписка: ссылку для %s создать не удалось: %s", ch.get("chat_id"), e)
        return ""


# ───────────────────────── запись фактов и статистики ─────────────────────────

def _event(chat_id, tg_id, channel_chat_id, kind: str, info: str = "") -> None:
    db.execute("INSERT INTO gate_events (ts, chat_id, tg_id, channel_chat_id, kind, info) VALUES (?,?,?,?,?,?)",
               (iso(), chat_id, tg_id, channel_chat_id, kind, info or ""))


def bump_daily(chat_id: int, field: str, n: int = 1) -> None:
    if field not in ("deleted", "notices", "restricted"):
        return
    db.execute(f"INSERT INTO gate_daily (day, chat_id, {field}) VALUES (?,?,?) "
               f"ON CONFLICT(day, chat_id) DO UPDATE SET {field}={field}+excluded.{field}",
               (local_day(), int(chat_id), int(n)))


def get_member(chat_id: int, tg_id: int) -> dict:
    r = db.fetchone("SELECT * FROM gate_members WHERE chat_id=? AND tg_id=?", (int(chat_id), int(tg_id)))
    return dict(r) if r else None


def update_member(chat_id: int, tg_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = iso()
    db.execute(f"UPDATE gate_members SET {', '.join(k + '=?' for k in fields)} WHERE chat_id=? AND tg_id=?",
               tuple(fields.values()) + (int(chat_id), int(tg_id)))


def record_check(chat_id: int, tg_id: int, results: dict, username: str = "", full_name: str = "") -> dict:
    """Записать итог проверки. results: {channel_chat_id: True | False | None}.
    Возвращает {'ok', 'prev', 'became_ok', 'relocked'}."""
    chat_id, tg_id = int(chat_id), int(tg_id)
    stamp = iso()
    known = {k: v for k, v in results.items() if v is not None}
    ok = all(known.values()) if known else True
    out = {"ok": ok, "prev": None, "became_ok": False, "relocked": False}
    with db.db_lock():
        m = get_member(chat_id, tg_id)
        if m is None or m.get("status") == "new":
            # Первая проверка этого человека в этой группе («new» — только вступил)
            if m is None:
                db.execute("INSERT INTO gate_members (chat_id, tg_id, status) VALUES (?,?, 'new')", (chat_id, tg_id))
            update_member(chat_id, tg_id, username=username or (m or {}).get("username") or "",
                          full_name=full_name or (m or {}).get("full_name") or "",
                          status="ok" if ok else "blocked", first_seen_at=stamp, first_check_ok=1 if ok else 0,
                          first_required_at=None if ok else stamp, last_check_at=stamp,
                          last_ok_at=stamp if ok else None)
            _event(chat_id, tg_id, None, "first_check", "ok" if ok else "missing")
            if not ok:
                _event(chat_id, tg_id, None, "required")
        else:
            prev = m.get("status") or "ok"
            out["prev"] = prev
            upd = {"last_check_at": stamp}
            if username or full_name:
                upd.update(username=username or m.get("username") or "", full_name=full_name or m.get("full_name") or "")
            if ok:
                upd["last_ok_at"] = stamp
                if prev == "blocked":
                    upd["status"] = "ok"
                    upd["access_at"] = stamp
                    if not m.get("verified_at"):
                        upd["verified_at"] = stamp
                        _event(chat_id, tg_id, None, "subscribed_after")
                    _event(chat_id, tg_id, None, "access_granted")
                    out["became_ok"] = True
                elif prev == "relocked":
                    upd.update(status="ok", access_at=stamp, resubscribed_at=stamp)
                    _event(chat_id, tg_id, None, "resubscribed")
                    _event(chat_id, tg_id, None, "access_granted")
                    out["became_ok"] = True
            else:
                if prev == "ok":
                    upd.update(status="relocked", relocked_at=stamp, relock_count=int(m.get("relock_count") or 0) + 1)
                    _event(chat_id, tg_id, None, "relocked")
                    out["relocked"] = True
                elif not m.get("first_required_at"):
                    upd["first_required_at"] = stamp
            update_member(chat_id, tg_id, **upd)
        for ch_chat, res in results.items():
            _record_channel(chat_id, tg_id, int(ch_chat), res, stamp)
    return out


def _record_channel(chat_id: int, tg_id: int, ch_chat: int, res, stamp: str) -> None:
    if res is None:
        return
    row = db.fetchone("SELECT * FROM gate_member_channels WHERE chat_id=? AND tg_id=? AND channel_chat_id=?",
                      (chat_id, tg_id, ch_chat))
    if row is None:
        db.execute("INSERT INTO gate_member_channels (chat_id, tg_id, channel_chat_id, state, was_member_before, "
                   "first_checked_at, first_required_at, last_check_at) VALUES (?,?,?,?,?,?,?,?)",
                   (chat_id, tg_id, ch_chat, "member" if res else "missing", 1 if res else 0, stamp,
                    None if res else stamp, stamp))
        if not res:
            _event(chat_id, tg_id, ch_chat, "ch_required")
        return
    _apply_channel_state(dict(row), res, stamp)


def _apply_channel_state(row: dict, is_member: bool, stamp: str) -> None:
    chat_id, tg_id, ch_chat = row["chat_id"], row["tg_id"], row["channel_chat_id"]
    state = row.get("state")
    upd = {"last_check_at": stamp}
    if is_member:
        if state == "missing":
            upd["state"] = "member"
            if row.get("first_required_at") and not row.get("subscribed_at"):
                upd["subscribed_at"] = stamp
                _event(chat_id, tg_id, ch_chat, "ch_subscribed")
        elif state == "left":
            upd.update(state="member", resubscribed_at=stamp, resub_count=int(row.get("resub_count") or 0) + 1)
            _event(chat_id, tg_id, ch_chat, "ch_resubscribed")
    else:
        if state == "member":
            upd.update(state="left", left_at=stamp, unsub_count=int(row.get("unsub_count") or 0) + 1)
            _event(chat_id, tg_id, ch_chat, "ch_unsubscribed")
        elif state is None:
            upd["state"] = "missing"
        if not row.get("first_required_at") and state != "member":
            upd["first_required_at"] = stamp
            _event(chat_id, tg_id, ch_chat, "ch_required")
    db.execute(f"UPDATE gate_member_channels SET {', '.join(k + '=?' for k in upd)} "
               "WHERE chat_id=? AND tg_id=? AND channel_chat_id=?",
               tuple(upd.values()) + (chat_id, tg_id, ch_chat))


def record_channel_event(channel_chat_id: int, tg_id: int, is_member: bool) -> list:
    """Событие канала (вступил/вышел) — для всех групп, где за человеком следим.
    Возвращает chat_id групп, где он сейчас ограничен ботом."""
    stamp = iso()
    groups = []
    with db.db_lock():
        for row in db.fetchall("SELECT * FROM gate_member_channels WHERE tg_id=? AND channel_chat_id=?",
                               (int(tg_id), int(channel_chat_id))):
            _apply_channel_state(dict(row), bool(is_member), stamp)
            groups.append(int(row["chat_id"]))
    return groups


def record_join(chat_id: int, tg_id: int, username: str = "", full_name: str = "") -> None:
    stamp = iso()
    with db.db_lock():
        m = get_member(chat_id, tg_id)
        if m is None:
            db.execute("INSERT INTO gate_members (chat_id, tg_id, username, full_name, status, joined_at, "
                       "last_join_at, updated_at) VALUES (?,?,?,?, 'new', ?, ?, ?)",
                       (int(chat_id), int(tg_id), username or "", full_name or "", stamp, stamp, stamp))
        else:
            update_member(chat_id, tg_id, last_join_at=stamp, joined_at=m.get("joined_at") or stamp,
                          left_at=None)
        _event(int(chat_id), int(tg_id), None, "joined")


def record_left(chat_id: int, tg_id: int) -> None:
    if get_member(chat_id, tg_id):
        update_member(chat_id, tg_id, left_at=iso())


# ───────────────────────── решение по человеку ─────────────────────────

_verdicts = {}          # (chat_id, user_id) → {"ok", "missing", "ts"}
_last_good = {}         # (user_id, channel_chat_id) → когда Telegram последний раз подтвердил подписку
LAST_GOOD_SEC = 600.0   # при сбое связи с Telegram верим подтверждению не старше 10 минут
_locks = {}
MISSING_TTL = 20.0


def _lock_for(key) -> asyncio.Lock:
    lk = _locks.get(key)
    if lk is None:
        if len(_locks) > 5000:
            for k in [k for k, v in _locks.items() if not v.locked()][:2500]:
                _locks.pop(k, None)
        lk = _locks[key] = asyncio.Lock()
    return lk


def forget_user(tg_id: int, chat_id: int = None) -> None:
    for key in [k for k in _verdicts if k[1] == int(tg_id) and (chat_id is None or k[0] == int(chat_id))]:
        _verdicts.pop(key, None)


async def exempt_reason(bot, snap: dict, chat_id: int, user) -> str:
    """Почему человека не проверяем ('' — проверяем)."""
    uid = int(user.id)
    if uid == getattr(bot, "id", None):
        return "сам бот"
    if getattr(user, "is_bot", False):
        return "бот"
    if uid in SERVICE_IDS:
        return "служебный аккаунт Telegram"
    if uid in snap["exempt"]:
        return "в исключениях"
    if uid in snap["admins"]:
        return "админ бота"
    admins = await group_admins(bot, chat_id)
    if admins is None:
        # Список админов не получили — спрашиваем про этого человека напрямую
        try:
            st = str((await member_status(bot, chat_id, uid)).get("status") or "")
        except Exception:
            st = ""
        return "админ группы" if st in ("administrator", "creator") else ""
    return "админ группы" if uid in admins else ""


async def is_exempt(bot, snap: dict, chat_id: int, user) -> bool:
    return bool(await exempt_reason(bot, snap, chat_id, user))


def _name(user) -> str:
    return " ".join(x for x in (getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "") if x).strip()


async def evaluate(bot, chat_id: int, user, force: bool = False) -> dict:
    """Проверить подписку человека для группы. Результат: {'ok', 'missing', 'errors', 'event'}."""
    key = (int(chat_id), int(user.id))
    hit = _verdicts.get(key)
    if not force and hit and _fresh(hit):
        return hit
    async with _lock_for(key):
        hit = _verdicts.get(key)
        if not force and hit and _fresh(hit):
            return hit
        snap = await asnapshot()
        chans = channels_for(snap, chat_id)
        results, missing, errors, checks, unverifiable = {}, [], [], [], []
        strict = bool(setting("gate_strict"))
        if chans:
            infos = [{} for _ in chans]
            outcomes = await asyncio.gather(*(check_member(bot, ch, user.id, inf) for ch, inf in zip(chans, infos)))
            now = time.monotonic()
            for ch, inf, (res, err) in zip(chans, infos, outcomes):
                cc = int(ch["chat_id"])
                results[cc] = res
                row = {"channel_id": cc, "title": ch.get("title") or "", "status": inf.get("status"),
                       "is_member": inf.get("is_member"), "error": err}
                if res is True:
                    _last_good[(int(user.id), cc)] = now
                    row["result"] = "подписан"
                elif res is False:
                    missing.append(ch)
                    _last_good.pop((int(user.id), cc), None)
                    row["result"] = "НЕ подписан"
                else:
                    errors.append((ch, err))
                    lg = _last_good.get((int(user.id), cc))
                    if _is_transient_text(err) and lg and now - lg < LAST_GOOD_SEC:
                        row["result"] = "сбой связи, взят последний подтверждённый ответ: подписан"
                    elif strict:
                        missing.append(ch)
                        unverifiable.append(ch)
                        row["result"] = "проверить нельзя → НЕ пропускаем (строгий режим)"
                    else:
                        row["result"] = "проверить нельзя → пропускаем (режим «пропускать»)"
                checks.append(row)
        for ch, err in errors:
            if err and not _is_transient_text(err):
                await report_channel_error(bot, ch, err)
        for ch in chans:
            if results.get(int(ch["chat_id"])) is not None and ch.get("check_state") != "ok":
                _err_written.pop(ch["chat_id"], None)
                await asyncio.to_thread(_mark_channel, ch["id"], "ok", "")
        try:
            ev = await asyncio.to_thread(record_check, chat_id, user.id, results,
                                         getattr(user, "username", "") or "", _name(user))
        except Exception as e:
            log.warning("подписка: запись проверки %s/%s: %s", chat_id, user.id, e)
            ev = {"ok": not missing, "became_ok": False, "relocked": False, "prev": None}
        verdict = {"ok": not missing, "missing": missing, "errors": errors, "ts": time.monotonic(), "event": ev,
                   "checks": checks, "unverifiable": unverifiable, "channels": len(chans), "source": "telegram"}
        _verdicts[key] = verdict
        if len(_verdicts) > 20000:
            for k in list(_verdicts)[:10000]:
                _verdicts.pop(k, None)
        return verdict


def _fresh(v: dict) -> bool:
    if v.get("unverifiable"):
        ttl = 10.0                       # канал не проверяется — пробуем снова чаще
    else:
        ttl = setting("gate_ok_ttl_sec") if v["ok"] else MISSING_TTL
    return time.monotonic() - v["ts"] < ttl


def _is_transient_text(err: str) -> bool:
    return err.split(":", 1)[0] in ("TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter",
                                    "RestartingTelegram", "TimeoutError", "ClientConnectionError",
                                    "ClientOSError", "ServerDisconnectedError")


# ───────────────────────── действия в группе ─────────────────────────

def notice_text(user, missing: list, relock: bool = False) -> str:
    name = _h(_name(user) or (("@" + user.username) if getattr(user, "username", "") else "Участник"))
    who = f'<a href="tg://user?id={int(user.id)}">{name}</a>'
    if relock:
        head = (f"{who}, вы отписались от обязательного канала, поэтому писать в этом чате снова нельзя.\n\n"
                "Подпишитесь 👇 и нажмите «✅ Проверить подписку».")
    else:
        head = (f"{who}, чтобы писать в этом чате, необходимо подписаться на наши обязательные каналы 👇\n"
                "После подписки нажмите «✅ Проверить подписку».")
    no_link = [ch for ch in missing if not channel_url(ch)]
    if no_link:
        head += "\n\n" + "\n".join(f"📢 {_h(ch.get('title') or ch['chat_id'])}" for ch in no_link)
    return head


def notice_kb(chat_id: int, missing: list):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for ch in missing:
        url = channel_url(ch)
        if url:
            icon = "📢" if ch.get("kind") == "channel" else "👥"
            rows.append([InlineKeyboardButton(text=f"{icon} {(ch.get('title') or 'Канал')[:40]}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data=f"gate:chk:{int(chat_id)}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mute_active(chat_id: int, tg_id: int) -> bool:
    try:
        row = db.fetchone("SELECT until_ts FROM chat_moderation WHERE chat_id=? AND user_tg_id=? AND action='mute'",
                          (int(chat_id), int(tg_id)))
    except Exception:
        return False
    if not row:
        return False
    until = row["until_ts"]
    if not until:
        return True
    try:
        return datetime.fromisoformat(str(until)) > datetime.utcnow()
    except ValueError:
        return True


_restricted_at = {}
_last_seen = {}         # группа → когда бот последний раз получил из неё сообщение


async def block(bot, chat_id: int, user, verdict: dict, thread_id: int = None, from_message: bool = False) -> dict:
    """Человек подписан не на всё: ограничить (один раз) и показать каналы (не чаще паузы).
    Десять сообщений подряд приходят одновременно — ограничение и уведомление всё равно одни.
    Возвращает {'restricted': True|False|None(уже был), 'error': ..., 'notice': bool}."""
    async with _lock_for(("block", int(chat_id), int(user.id))):
        return await _block(bot, chat_id, user, verdict, thread_id, from_message)


async def _block(bot, chat_id: int, user, verdict: dict, thread_id: int = None, from_message: bool = False) -> dict:
    out = {"restricted": None, "error": "", "notice": False}
    m = await asyncio.to_thread(get_member, chat_id, user.id) or {}
    group = _snap["groups"].get(int(chat_id)) or {}
    key = (int(chat_id), int(user.id))
    need = not m.get("restricted")
    if not need and from_message and time.monotonic() - _restricted_at.get(key, 0) > 5:
        # Ограниченный человек написать не может. Раз сообщение пришло, ограничение
        # кто-то снял вручную (или оно истекло) — ставим заново
        need = True
    if need and not int(group.get("can_restrict", 1) or 0):
        out.update(restricted=False, error="обычная группа: Telegram не даёт ограничивать участников")
    if need and int(group.get("can_restrict", 1) or 0):
        ok, err = await restrict(bot, chat_id, user.id)
        out.update(restricted=ok, error=err)
        if ok:
            _restricted_at[key] = time.monotonic()
            if len(_restricted_at) > 20000:
                _restricted_at.clear()
            await asyncio.to_thread(update_member, chat_id, user.id, restricted=1)
            await asyncio.to_thread(bump_daily, chat_id, "restricted")
        else:
            low = err.lower()
            if "supergroup" in low or "only for supergroups" in low:
                await asyncio.to_thread(_upsert_group, chat_id, can_restrict=0)
            await notify_admins(
                bot, f"restrict:{chat_id}",
                f"⚠️ Обязательная подписка: бот не смог ограничить участника в группе "
                f"«{_h(group.get('title') or chat_id)}». Нужны права администратора: "
                f"«{RIGHT_TITLES['can_restrict_members']}» и «{RIGHT_TITLES['can_delete_messages']}».\n\n"
                f"<i>{_h(err)}</i>")
    out["notice"] = await send_notice(bot, chat_id, user, verdict, thread_id, m)
    return out


async def send_notice(bot, chat_id: int, user, verdict: dict, thread_id: int = None, m: dict = None) -> bool:
    m = m if m is not None else (await asyncio.to_thread(get_member, chat_id, user.id) or {})
    last = parse_iso(m.get("notice_at"))
    if last and (now_utc() - last).total_seconds() < setting("gate_notice_cooldown_sec"):
        return False
    if m.get("notice_msg_id"):
        await safe_delete(bot, chat_id, m["notice_msg_id"])
    relock = bool((verdict.get("event") or {}).get("relocked")) or m.get("status") == "relocked"
    kwargs = {"parse_mode": "HTML", "reply_markup": notice_kb(chat_id, verdict["missing"]),
              "disable_web_page_preview": True}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        msg = await _call(bot.send_message(int(chat_id), notice_text(user, verdict["missing"], relock), **kwargs))
    except Exception as e:
        log.info("подписка: уведомление в %s не отправлено: %s", chat_id, e)
        return False
    stamp = now_utc()
    await asyncio.to_thread(update_member, chat_id, user.id, notice_msg_id=msg.message_id, notice_at=iso(stamp),
                            notice_del_at=iso(stamp + timedelta(minutes=setting("gate_notice_ttl_min"))))
    await asyncio.to_thread(bump_daily, chat_id, "notices")
    return True


async def grant(bot, chat_id: int, tg_id: int) -> bool:
    """Подписка подтверждена: снять ограничение, которое ставил бот, и убрать уведомление."""
    m = await asyncio.to_thread(get_member, chat_id, tg_id) or {}
    lifted = False
    if m.get("restricted"):
        if await asyncio.to_thread(_mute_active, chat_id, tg_id):
            await asyncio.to_thread(update_member, chat_id, tg_id, restricted=0)   # мут модератора не трогаем
        else:
            ok, err = await unrestrict(bot, chat_id, tg_id)
            if ok:
                await asyncio.to_thread(update_member, chat_id, tg_id, restricted=0)
                lifted = True
            else:
                log.warning("подписка: не снял ограничение %s/%s: %s", chat_id, tg_id, err)
    if m.get("notice_msg_id"):
        when = now_utc() + timedelta(seconds=setting("gate_success_delete_sec"))
        # notice_at сбрасываем: если человек снова отпишется, новое уведомление придёт сразу
        await asyncio.to_thread(update_member, chat_id, tg_id, notice_del_at=iso(when), notice_at=None)
    return lifted


_quiet = {}


def _decision_line(chat_id, user, verdict: dict, action: str, source: str) -> str:
    parts = [f"user_id={int(user.id)}", f"group_id={int(chat_id)}",
             f"required={[c.get('channel_id') for c in (verdict or {}).get('checks') or []] or (verdict or {}).get('required') or []}"]
    for c in (verdict or {}).get("checks") or []:
        parts.append(f"[channel_id={c['channel_id']} status={c.get('status')} is_member={c.get('is_member')} "
                     f"result={c.get('result')}" + (f" error={c['error']}" if c.get("error") else "") + "]")
    if verdict is not None and "ok" in verdict:
        parts.append(f"verified={'true' if verdict['ok'] else 'false'}")
    parts.append(f"source={source}")
    parts.append(f"action={action}")
    return " ".join(parts)


def _save_decision(chat_id, tg_id, username, verified, source, action, details) -> None:
    db.execute("INSERT INTO gate_check_log (ts, chat_id, tg_id, username, verified, source, action, details) "
               "VALUES (?,?,?,?,?,?,?,?)", (iso(), int(chat_id), int(tg_id), username or "",
                                           None if verified is None else (1 if verified else 0),
                                           source, action, details[:2000]))


async def log_decision(chat_id, user, verdict, action: str, source: str) -> None:
    """Почему сообщение пропущено или удалено — в лог сервера и в «Журнал проверок» админки."""
    line = _decision_line(chat_id, user, verdict, action, source)
    log.info("GATE %s", line)
    if not setting("gate_debug_log"):
        return
    try:
        ver = None if verdict is None or "ok" not in verdict else bool(verdict["ok"])
        await asyncio.to_thread(_save_decision, chat_id, user.id, getattr(user, "username", "") or "",
                                ver, source, action, line)
    except Exception as e:
        log.debug("журнал подписки: %s", e)


def recent_decisions(chat_id: int = None, limit: int = 25) -> list:
    sql, args = "SELECT * FROM gate_check_log", []
    if chat_id:
        sql += " WHERE chat_id=?"
        args.append(int(chat_id))
    return [dict(r) for r in db.fetchall(sql + " ORDER BY id DESC LIMIT ?", tuple(args) + (int(limit),))]


async def tell_opened(bot, chat_id: int, tg_id: int) -> None:
    """Доступ открылся без кнопки (подписался позже): сказать в личку, если человек писал боту."""
    title = (_snap["groups"].get(int(chat_id)) or {}).get("title") or ""
    try:
        await _call(bot.send_message(int(tg_id), "✅ Подписка подтверждена! Теперь вы можете писать сообщения "
                                                 f"в группе{' «' + _h(title) + '»' if title else ''}.", parse_mode="HTML"), 8)
    except Exception:
        pass


async def guard_message(bot, message) -> bool:
    """Единая проверка ЛЮБОГО сообщения в группе ДО всех обработчиков: текст, фото, видео,
    голосовое, кружок, файл, стикер, GIF, опрос, ссылка, ответ, пересланное — всё равно.
    False — сообщение удалено, дальше его никто не обрабатывает."""
    chat = message.chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return True
    snap = _snap if time.monotonic() - _snap["at"] <= SNAP_TTL else await asnapshot()
    if int(chat.id) not in snap["groups"]:
        return True
    _last_seen[int(chat.id)] = time.time()
    # Служебные сообщения: вступление и выход — не удаляем, только учитываем
    if message.new_chat_members:
        for u in message.new_chat_members:
            if not u.is_bot:
                asyncio.create_task(on_join(bot, chat.id, u, source="service"))
        return True
    if message.left_chat_member:
        await on_left(bot, chat.id, message.left_chat_member.id)
        return True
    chans = channels_for(snap, chat.id)
    if not chans:
        # Проверка включена, а каналов нет — защищать нечего. Это ошибка настройки: в лог и админу
        if time.monotonic() - _quiet.get(("nochan", int(chat.id)), 0) > 600:
            _quiet[("nochan", int(chat.id))] = time.monotonic()
            log.warning("GATE group_id=%s: проверка включена, но к группе не привязан ни один включённый канал", chat.id)
        return True
    if message.is_automatic_forward:
        return True
    sender_chat = message.sender_chat
    if sender_chat is not None:
        # Анонимный админ группы и посты официальных каналов — свои
        if int(sender_chat.id) == int(chat.id) or any(int(c["chat_id"]) == int(sender_chat.id) for c in snap["channels"]):
            return True
        # «От имени канала»: подписку канала проверить нельзя — это обход проверки
        deleted = await safe_delete(bot, chat.id, message.message_id, warn=True)
        await asyncio.to_thread(bump_daily, chat.id, "deleted")
        log.info("GATE group_id=%s sender_chat=%s: сообщение от имени чужого канала, action=%s",
                 chat.id, sender_chat.id, "DELETE" if deleted else "DELETE_FAILED")
        return False
    user = message.from_user
    if user is None:
        return True
    reason = await exempt_reason(bot, snap, chat.id, user)
    if reason:
        await log_decision(chat.id, user, {"required": [int(c["chat_id"]) for c in chans]}, f"PASS ({reason})", "исключение")
        return True
    hit = _verdicts.get((int(chat.id), int(user.id)))
    cached = bool(hit and _fresh(hit))
    verdict = await evaluate(bot, chat.id, user)
    source = "кеш" if cached and verdict is hit else "telegram"
    if verdict["ok"]:
        ev = verdict.get("event") or {}
        if ev.get("became_ok") and source == "telegram":
            asyncio.create_task(grant(bot, chat.id, user.id))
        await log_decision(chat.id, user, verdict, "PASS", source)
        return True
    deleted = await safe_delete(bot, chat.id, message.message_id, warn=True)
    await asyncio.to_thread(_count_deleted, chat.id, user.id)
    thread = message.message_thread_id if getattr(message, "is_topic_message", False) else None
    res = await block(bot, chat.id, user, verdict, thread, from_message=True)
    action = ("DELETE_MESSAGE" if deleted else "DELETE_FAILED") + (
        " + RESTRICT" if res.get("restricted") else
        " + RESTRICT_FAILED(" + (res.get("error") or "")[:80] + ")" if res.get("restricted") is False else
        " + уже ограничен")
    await log_decision(chat.id, user, verdict, action, source)
    return False


def _count_deleted(chat_id: int, tg_id: int) -> None:
    bump_daily(chat_id, "deleted")
    db.execute("UPDATE gate_members SET deleted_count=COALESCE(deleted_count,0)+1 WHERE chat_id=? AND tg_id=?",
               (int(chat_id), int(tg_id)))


_joins = {}


async def on_join(bot, chat_id: int, user, source: str = "chat_member") -> None:
    """Новый участник основной группы: проверить сразу и, если нужно, ограничить."""
    try:
        snap = await asnapshot()
        if int(chat_id) not in snap["groups"] or getattr(user, "is_bot", False):
            return
        key = (int(chat_id), int(user.id))
        last = _joins.get(key, 0)
        if time.monotonic() - last < 30:
            return                                  # то же вступление пришло вторым событием
        _joins[key] = time.monotonic()
        if len(_joins) > 5000:
            for k in list(_joins)[:2500]:
                _joins.pop(k, None)
        await asyncio.to_thread(record_join, chat_id, user.id, getattr(user, "username", "") or "", _name(user))
        if not channels_for(snap, chat_id) or await is_exempt(bot, snap, chat_id, user):
            return
        verdict = await evaluate(bot, chat_id, user, force=True)
        if verdict["ok"]:
            m = await asyncio.to_thread(get_member, chat_id, user.id) or {}
            if m.get("restricted"):
                await grant(bot, chat_id, user.id)      # вернулся уже подписанным
            return
        await block(bot, chat_id, user, verdict)
    except Exception as e:
        log.warning("подписка: вступление %s в %s: %s", getattr(user, "id", "?"), chat_id, e)


async def on_left(bot, chat_id: int, tg_id: int) -> None:
    _joins.pop((int(chat_id), int(tg_id)), None)     # следующий вход — новое вступление
    try:
        await asyncio.to_thread(record_left, chat_id, tg_id)
        forget_user(tg_id, chat_id)
    except Exception as e:
        log.info("подписка: выход %s: %s", tg_id, e)


async def on_channel_member(bot, channel_chat_id: int, user, is_member: bool) -> bool:
    """Событие обязательного канала. True — канал наш (обработан)."""
    snap = await asnapshot()
    if not any(int(c["chat_id"]) == int(channel_chat_id) for c in snap["channels"]):
        return False
    groups = await asyncio.to_thread(record_channel_event, channel_chat_id, user.id, is_member)
    for g in set(groups) | set(groups_for_channel(snap, channel_chat_id)):
        forget_user(user.id, g)
    if is_member:
        # Подписался: если в какой-то группе он ограничен — проверить и открыть сразу
        for g in set(groups):
            if g not in snap["groups"]:
                continue
            m = await asyncio.to_thread(get_member, g, user.id) or {}
            if m.get("status") in ("blocked", "relocked"):
                verdict = await evaluate(bot, g, user, force=True)
                if verdict["ok"]:
                    await grant(bot, g, user.id)
                    await tell_opened(bot, g, user.id)
    return True


async def check_button(bot, chat_id: int, user) -> dict:
    """Кнопка «✅ Проверить подписку»: перепроверить и при успехе открыть чат."""
    snap = await asnapshot()
    if int(chat_id) not in snap["groups"] or not channels_for(snap, chat_id):
        return {"ok": True, "exempt": True, "missing": []}
    if await is_exempt(bot, snap, chat_id, user):
        return {"ok": True, "exempt": True, "missing": []}
    verdict = await evaluate(bot, chat_id, user, force=True)
    if verdict["ok"]:
        await grant(bot, chat_id, user.id)
    return verdict


# ───────────────────────── фоновая работа ─────────────────────────

async def cleanup_notices(bot) -> int:
    rows = await asyncio.to_thread(db.fetchall,
                                   "SELECT chat_id, tg_id, notice_msg_id FROM gate_members "
                                   "WHERE notice_msg_id IS NOT NULL AND notice_del_at IS NOT NULL "
                                   "AND notice_del_at<=? LIMIT 50", (iso(),))
    for r in rows:
        await safe_delete(bot, r["chat_id"], r["notice_msg_id"])
        await asyncio.to_thread(update_member, r["chat_id"], r["tg_id"], notice_msg_id=None, notice_del_at=None)
    return len(rows)


class _U:
    def __init__(self, tg_id, username="", full_name=""):
        self.id = int(tg_id)
        self.username = username or ""
        self.first_name = full_name or ""
        self.last_name = ""
        self.is_bot = False


async def recheck_restricted(bot, limit: int = 40) -> int:
    """Ограниченные ботом участники: вдруг уже подписались (событие канала могло не прийти)."""
    snap = await asnapshot()
    if not snap["groups"]:
        return 0
    marks = ",".join("?" * len(snap["groups"]))
    rows = await asyncio.to_thread(
        db.fetchall,
        f"SELECT chat_id, tg_id, username, full_name FROM gate_members WHERE restricted=1 "
        f"AND status IN ('blocked','relocked') AND chat_id IN ({marks}) AND left_at IS NULL "
        f"ORDER BY COALESCE(last_check_at,'') LIMIT ?", tuple(snap["groups"]) + (int(limit),))
    opened = 0
    for r in rows:
        u = _U(r["tg_id"], r["username"], r["full_name"])
        verdict = await evaluate(bot, r["chat_id"], u, force=True)
        if verdict["ok"]:
            await grant(bot, r["chat_id"], r["tg_id"])
            await tell_opened(bot, r["chat_id"], r["tg_id"])
            opened += 1
        await asyncio.sleep(0.3)
    return opened


async def release_disabled(bot, limit: int = 30) -> int:
    """Проверку в группе выключили или канал убрали: снять ограничения, которые ставил бот."""
    snap = await asnapshot()
    rows = await asyncio.to_thread(db.fetchall,
                                   "SELECT chat_id, tg_id FROM gate_members WHERE restricted=1 LIMIT 500")
    done = 0
    for r in rows:
        g = int(r["chat_id"])
        if g in snap["groups"] and channels_for(snap, g):
            continue
        await grant(bot, g, r["tg_id"])
        done += 1
        if done >= limit:
            break
        await asyncio.sleep(0.3)
    return done


def group_problems(chat_id: int) -> list:
    """Почему проверка в группе может не работать — человеческим языком, для админки."""
    g = get_group(chat_id)
    if not g.get("enabled"):
        return ["⚪ Проверка подписки в этой группе выключена."]
    out = []
    rs, miss = g.get("rights_state") or "unknown", g.get("rights_missing") or ""
    if rs == "error":
        out.append(f"🔴 Обязательная подписка не защищает чат: бот не видит группу ({_h(miss)}).")
    elif rs == "missing":
        if NOT_ADMIN in miss:
            out.append("🔴 Обязательная подписка не защищает чат: бот не администратор группы — Telegram "
                       "не присылает ему сообщения участников, удалять их он не может.")
        else:
            if RIGHT_TITLES["can_delete_messages"] in miss:
                out.append("🔴 Обязательная подписка не защищает чат: у бота нет права удалять сообщения.")
            if RIGHT_TITLES["can_restrict_members"] in miss:
                out.append("🔴 У бота нет права ограничивать участников: сообщения удаляются, но запретить "
                           "писать бот не может.")
    elif rs == "unknown":
        out.append("⚪ Права бота в группе ещё не проверялись — нажмите «🔄 Проверить права бота».")
    if not int(g.get("can_restrict", 1) or 0):
        out.append("🟡 Обычная группа: Telegram не даёт ограничивать участников, бот только удаляет сообщения.")
    snap = snapshot(force=True)
    chans = channels_for(snap, chat_id)
    if not chans:
        out.append("🔴 К группе не привязан ни один включённый канал — проверять нечего, писать могут все.")
    for c in chans:
        if c.get("check_state") == "error":
            out.append(f"🔴 Канал «{_h(c.get('title') or c['chat_id'])}»: проверка НЕ работает — "
                       f"{_h(c.get('check_error') or '')}")
    return out


def system_problems() -> list:
    """Сводка для главного меню: всё, из-за чего кто-то может писать без подписки."""
    out = []
    enabled = [g for g in known_groups() if g.get("enabled")]
    if not enabled:
        out.append("🔴 Проверка не включена ни в одной группе — откройте «👥 Основные группы».")
    for g in enabled:
        for p in group_problems(g["chat_id"]):
            out.append(f"{_h(g.get('title') or g['chat_id'])}: {p}")
    for c in list_channels():
        if not c.get("is_active") and (c.get("disabled_reason") or "") == "check":
            out.append(f"🔴 Канал «{_h(c.get('title') or c['chat_id'])}» выключен: бот не может его проверять.")
    return out


async def diagnose(bot, probe_user_id: int = None) -> str:
    """«🔍 Проверить систему подписок»: каналы, права бота, группы — живыми запросами к Telegram."""
    problems = 0
    lines = ["🔍 <b>Проверка системы обязательной подписки</b>\n"]
    strict = bool(setting("gate_strict"))
    lines.append("Режим: " + ("⛔ строгий — если канал нельзя проверить, сообщения участников удаляются"
                               if strict else "⚠️ «пропускать» — канал, который нельзя проверить, писать не мешает"))
    lines.append(f"Подписку переспрашиваем у Telegram не реже чем раз в {setting('gate_ok_ttl_sec')} с.")
    try:
        me = await _call(bot.get_me())
        lines.append(f"Бот: @{_h(me.username or '')}")
    except Exception as e:
        lines.append(f"🔴 Бот не отвечает Telegram: {_h(_err_text(e))}")
        problems += 1
    lines.append("\n📢 <b>Обязательные каналы</b>")
    chans = await asyncio.to_thread(list_channels)
    if not chans:
        lines.append("🔴 Список пуст — добавьте каналы.")
        problems += 1
    for ch in chans:
        title = f"{_h(ch.get('title') or '')} (<code>{ch['chat_id']}</code>)"
        if not ch.get("is_active"):
            why = " — бот не может его проверять" if (ch.get("disabled_reason") or "") == "check" else ""
            lines.append(f"🟡 {title} — канал отключён{why}")
            continue
        res = await check_channel_access(bot, ch, probe_user_id)
        if res["ok"]:
            pr = res.get("probe") or {}
            you = ""
            if pr:
                you = (f"; вы: {'подписаны' if pr.get('subscribed') else 'НЕ подписаны'} "
                       f"(status={pr.get('status')}, is_member={pr.get('is_member')})")
            lines.append(f"🟢 {title} — OK: бот видит подписчиков, getChatMember работает{you}")
        else:
            problems += 1
            lines.append(f"🔴 {title} — {_h(res['text'])}")
    lines.append("\n👥 <b>Основные группы</b>")
    groups = await asyncio.to_thread(known_groups)
    enabled = [g for g in groups if g.get("enabled")]
    if not enabled:
        problems += 1
        lines.append("🔴 Проверка не включена ни в одной группе.")
    snap = await asyncio.to_thread(snapshot, True)
    for g in enabled:
        cid = int(g["chat_id"])
        r = await check_group_rights(bot, cid)
        lines.append(f"\n<b>{_h(r.get('title') or g.get('title') or cid)}</b> (<code>{cid}</code>)")
        if r["error"] and not r.get("is_admin"):
            problems += 1
            lines.append(f"🔴 {_h(r['error'])} — Telegram не присылает боту сообщения участников")
        else:
            lines.append("🟢 бот администратор группы")
            can_del = RIGHT_TITLES["can_delete_messages"] not in r["missing"]
            can_res = RIGHT_TITLES["can_restrict_members"] not in r["missing"]
            lines.append("🟢 удаление сообщений" if can_del else "🔴 удаление сообщений — нет права")
            if not r["supergroup"]:
                lines.append("🟡 ограничение участников — обычная группа, Telegram не даёт ограничивать")
            else:
                lines.append("🟢 ограничение участников" if can_res else "🔴 ограничение участников — нет права")
            problems += (0 if can_del else 1) + (0 if can_res or not r["supergroup"] else 1)
        gch = channels_for(snap, cid)
        if gch:
            lines.append("🟢 каналы: " + ", ".join(_h(c.get("title") or c["chat_id"]) for c in gch))
        else:
            problems += 1
            lines.append("🔴 к группе не привязан ни один включённый канал")
        seen = _last_seen.get(cid)
        if seen:
            lines.append("🟢 сообщения участников бот получает: последнее в "
                         + datetime.fromtimestamp(seen, ALMATY).strftime("%d.%m %H:%M"))
        else:
            lines.append("⚪ с запуска бота сообщений из этой группы не было (если участники пишут, а "
                         "здесь пусто — бот не получает сообщения: проверьте, что он администратор)")
    off = [g for g in groups if not g.get("enabled")]
    if off:
        lines.append("\n⚪ Без проверки: " + ", ".join(_h(g.get("title") or g["chat_id"]) for g in off[:15]))
    lines.append("\n" + ("🟢 <b>Итог: всё работает.</b>" if not problems else
                         f"🔴 <b>Итог: проблем — {problems}.</b> Пока они есть, кто-то может писать без подписки "
                         "или, в строгом режиме, сообщения подписанных тоже удаляются."))
    return "\n".join(lines)[:4000]


async def periodic_health(bot) -> None:
    """Раз в полчаса: права бота в группах и в каналах. Сломалось — админ узнаёт сразу."""
    snap = await asnapshot()
    for cid in list(snap["groups"]):
        r = await check_group_rights(bot, cid)
        if not r["ok"] and (r["error"] or r["missing"]):
            need = _h(r["error"]) if r["error"] else "\n".join(f"• {x}" for x in r["missing"])
            await notify_admins(bot, f"grp:{cid}",
                                f"🔴 Обязательная подписка не защищает чат «{_h(r.get('title') or cid)}»: "
                                f"боту нужны права администратора:\n{need}", cooldown=6 * 3600)
        await asyncio.sleep(0.3)
    for ch in snap["channels"]:
        before = ch.get("check_state")
        r = await check_channel_access(bot, ch)
        if not r["ok"] and before != "error":
            await report_channel_error(bot, ch, r["text"])
        await asyncio.sleep(0.3)


def _resolve_moved_groups() -> int:
    """Группа стала супергруппой до того, как появилась подписка: перенести настройку на новый ID."""
    try:
        from services import chat_migration as _cm
    except Exception:
        return 0
    n = 0
    for r in db.fetchall("SELECT chat_id FROM gate_groups"):
        new = _cm.resolve(int(r["chat_id"]))
        if new is not None and int(new) != int(r["chat_id"]):
            migrate_chat(int(r["chat_id"]), int(new))
            n += 1
    return n


def _trim_log() -> None:
    db.execute("DELETE FROM gate_check_log WHERE id <= (SELECT MAX(id) FROM gate_check_log) - 5000")


async def loop(bot) -> None:
    """Уборка уведомлений, перепроверка ограниченных, снятие ограничений в выключенных группах,
    раз в полчаса — проверка прав бота."""
    last_recheck = time.monotonic()
    last_release = 0.0
    last_health = time.monotonic() - 1800 + 60          # первая проверка прав — через минуту после запуска
    last_trim = 0.0
    try:
        await asyncio.to_thread(_resolve_moved_groups)
    except Exception as e:
        log.warning("подписка: перенос ID групп: %s", e)
    while True:
        await asyncio.sleep(15)
        try:
            await cleanup_notices(bot)
            now = time.monotonic()
            if now - last_release >= 60:
                last_release = now
                await release_disabled(bot)
            if now - last_recheck >= setting("gate_recheck_min") * 60:
                last_recheck = now
                await recheck_restricted(bot)
            if now - last_health >= 1800:
                last_health = now
                await periodic_health(bot)
            if now - last_trim >= 600:
                last_trim = now
                await asyncio.to_thread(_trim_log)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("обязательная подписка, фон: %s", e)


async def on_bot_status(bot, event) -> None:
    """Права бота поменялись: в обязательном канале — проверка подписки, в основной группе —
    удаление и ограничение. Админ узнаёт сразу, а не когда что-то сломается."""
    chat = event.chat
    st = getattr(event.new_chat_member, "status", "")
    new = str(getattr(st, "value", st) or "")
    ch = await asyncio.to_thread(get_channel_by_chat, chat.id)
    if ch and not ch.get("is_deleted"):
        lost = None
        if new in ("left", "kicked"):
            lost = "бот удалён из канала" if ch.get("kind") == "channel" else "бот удалён из группы"
        elif ch.get("kind") == "channel" and new not in ("administrator", "creator"):
            lost = "бот больше не администратор канала"
        if lost:
            await asyncio.to_thread(_mark_channel, ch["id"], "error", lost)
            _notified.pop(f"ch:{ch['chat_id']}", None)
            await notify_admins(
                bot, f"ch:{ch['chat_id']}",
                f"⚠️ Не удалось проверить обязательную подписку на канал «{_h(ch.get('title') or ch['chat_id'])}». "
                f"Проверьте права бота.\n\n<i>{lost}</i>\n\nПока проверка не заработает, участников из-за "
                "этого канала бот не блокирует.")
        else:
            await asyncio.to_thread(_mark_channel, ch["id"], "ok", "")
        invalidate()
    if chat.type in ("group", "supergroup"):
        forget_admins(chat.id)
        g = await asyncio.to_thread(get_group, chat.id)
        if g.get("enabled"):
            if new in ("left", "kicked"):
                await notify_admins(bot, f"grp:{chat.id}",
                                    f"⚠️ Обязательная подписка: бота удалили из группы «{_h(g.get('title') or chat.id)}».")
                return
            res = await check_group_rights(bot, chat.id)
            if not res["ok"]:
                need = "\n".join(f"• {x}" for x in res["missing"]) or res["error"]
                await notify_admins(
                    bot, f"grp:{chat.id}",
                    f"⚠️ Обязательная подписка в группе «{_h(res.get('title') or g.get('title') or chat.id)}» "
                    f"работать не сможет: боту нужны права администратора:\n{need}", cooldown=600)


def migrate_chat(old: int, new: int) -> None:
    """Группа стала супергруппой — переносим настройки и историю на новый ID."""
    for sql in ("UPDATE OR IGNORE gate_groups SET chat_id=? WHERE chat_id=?",
                "UPDATE OR IGNORE gate_links SET group_chat_id=? WHERE group_chat_id=?",
                "UPDATE OR IGNORE gate_members SET chat_id=? WHERE chat_id=?",
                "UPDATE OR IGNORE gate_member_channels SET chat_id=? WHERE chat_id=?",
                "UPDATE gate_events SET chat_id=? WHERE chat_id=?",
                "UPDATE OR IGNORE gate_daily SET chat_id=? WHERE chat_id=?",
                "UPDATE OR IGNORE gate_channels SET chat_id=? WHERE chat_id=?",
                "UPDATE OR IGNORE gate_links SET channel_chat_id=? WHERE channel_chat_id=?",
                "UPDATE OR IGNORE gate_member_channels SET channel_chat_id=? WHERE channel_chat_id=?",
                "UPDATE gate_events SET channel_chat_id=? WHERE channel_chat_id=?"):
        try:
            db.execute(sql, (int(new), int(old)))
        except Exception as e:
            log.warning("подписка: перенос ID %s → %s: %s", old, new, e)
    invalidate()


# ───────────────────────── статистика ─────────────────────────

PERIODS = {"td": "Сегодня", "yd": "Вчера", "7d": "7 дней", "30d": "30 дней", "mo": "Текущий месяц",
           "pm": "Прошлый месяц", "all": "Всё время", "cu": "Свой период"}


def period_bounds(code: str, custom: tuple = None, today: date = None):
    """(начало, конец) в UTC-строках и подпись. Границы — по времени Алматы."""
    today = today or datetime.now(ALMATY).date()
    if code == "td":
        a, b = today, today
    elif code == "yd":
        a = b = today - timedelta(days=1)
    elif code == "7d":
        a, b = today - timedelta(days=6), today
    elif code == "30d":
        a, b = today - timedelta(days=29), today
    elif code == "mo":
        a, b = today.replace(day=1), today
    elif code == "pm":
        first = today.replace(day=1)
        b = first - timedelta(days=1)
        a = b.replace(day=1)
    elif code == "cu" and custom:
        a, b = custom
    else:
        return None, None, PERIODS.get("all"), None, None
    start = datetime(a.year, a.month, a.day, tzinfo=ALMATY)
    end = datetime(b.year, b.month, b.day, tzinfo=ALMATY) + timedelta(days=1)
    label = f"{PERIODS.get(code, '')}: {a.strftime('%d.%m.%Y')}" + (f" – {b.strftime('%d.%m.%Y')}" if a != b else "")
    return iso(start), iso(end), label, a, b


def parse_custom_period(text: str):
    found = re.findall(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text or "")
    if not found:
        return None
    ds = []
    for d, mth, y in found[:2]:
        y = int(y) + (2000 if len(y) == 2 else 0)
        try:
            ds.append(date(y, int(mth), int(d)))
        except ValueError:
            return None
    a = ds[0]
    b = ds[1] if len(ds) > 1 else ds[0]
    if b < a:
        a, b = b, a
    return a, b


def _in(ts, a, b) -> bool:
    return (a is None or ts >= a) and (b is None or ts < b)


def _ev(kinds, chat_id=None, channel_chat_id=None) -> list:
    marks = ",".join("?" * len(kinds))
    sql = f"SELECT id, ts, chat_id, tg_id, channel_chat_id, kind, info FROM gate_events WHERE kind IN ({marks})"
    args = list(kinds)
    if chat_id is not None:
        sql += " AND chat_id=?"
        args.append(int(chat_id))
    if channel_chat_id is not None:
        sql += " AND channel_chat_id=?"
        args.append(int(channel_chat_id))
    return [dict(r) for r in db.fetchall(sql + " ORDER BY id", tuple(args))]


def _first_by(rows, keyf) -> dict:
    out = {}
    for r in rows:
        k = keyf(r)
        if k not in out:
            out[k] = r
    return out


def overview(code: str = "all", chat_id: int = None, custom: tuple = None) -> dict:
    a, b, label, *_ = period_bounds(code, custom)
    rows = _ev(("first_check", "subscribed_after", "access_granted", "relocked", "resubscribed", "joined"),
               chat_id=chat_id)
    by = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)
    first = _first_by(by.get("first_check", []), lambda r: r["tg_id"])
    cohort = [r for r in first.values() if _in(r["ts"], a, b)]
    already = {r["tg_id"] for r in cohort if r["info"] == "ok"}
    missing = {r["tg_id"] for r in cohort if r["info"] != "ok"}
    subs_any = {r["tg_id"] for r in by.get("subscribed_after", [])}
    subscribed = missing & subs_any
    in_p = lambda k: {r["tg_id"] for r in by.get(k, []) if _in(r["ts"], a, b)}
    sub_first = _first_by(by.get("subscribed_after", []), lambda r: r["tg_id"])
    subs_by_date = {r["tg_id"] for r in sub_first.values() if _in(r["ts"], a, b)}
    daily = _daily_counters(a, b, chat_id)
    return {
        "label": label,
        "checked": len(cohort),
        "already": len(already),
        "missing": len(missing),
        "subscribed": len(subscribed),
        "not_subscribed": len(missing - subscribed),
        "conversion": round(100.0 * len(subscribed) / len(missing), 1) if missing else 0.0,
        "subscribed_in_period": len(subs_by_date),
        "access": len(in_p("access_granted")),
        "relocked": len(in_p("relocked")),
        "resubscribed": len(in_p("resubscribed")),
        "joined": len(in_p("joined")),
        "deleted": daily["deleted"],
        "notices": daily["notices"],
        "restricted": daily["restricted"],
    }


def _daily_counters(a, b, chat_id=None) -> dict:
    da = parse_iso(a).astimezone(ALMATY).strftime("%Y-%m-%d") if a else "0000-00-00"
    db_ = (parse_iso(b) - timedelta(seconds=1)).astimezone(ALMATY).strftime("%Y-%m-%d") if b else "9999-99-99"
    sql = ("SELECT COALESCE(SUM(deleted),0) AS d, COALESCE(SUM(notices),0) AS n, COALESCE(SUM(restricted),0) AS r "
           "FROM gate_daily WHERE day>=? AND day<=?")
    args = [da, db_]
    if chat_id is not None:
        sql += " AND chat_id=?"
        args.append(int(chat_id))
    r = db.fetchone(sql, tuple(args))
    return {"deleted": int(r["d"] or 0), "notices": int(r["n"] or 0), "restricted": int(r["r"] or 0)}


def channel_title(ch_chat: int, cache: dict = None) -> str:
    if cache is not None and ch_chat in cache:
        return cache[ch_chat]
    r = db.fetchone("SELECT title, is_deleted FROM gate_channels WHERE chat_id=?", (int(ch_chat),))
    t = (r["title"] or str(ch_chat)) + (" (удалён из списка)" if r and r["is_deleted"] else "") if r else str(ch_chat)
    if cache is not None:
        cache[ch_chat] = t
    return t


def by_channels(code: str = "all", chat_id: int = None, custom: tuple = None) -> list:
    a, b, *_ = period_bounds(code, custom)
    rows = _ev(("ch_required", "ch_subscribed", "ch_unsubscribed", "ch_resubscribed"), chat_id=chat_id)
    chans = {}
    for r in rows:
        chans.setdefault(int(r["channel_chat_id"] or 0), []).append(r)
    order = {int(c["chat_id"]): i for i, c in enumerate(list_channels(include_deleted=True))}
    out, titles = [], {}
    for ch_chat, evs in chans.items():
        req_first = _first_by([r for r in evs if r["kind"] == "ch_required"], lambda r: r["tg_id"])
        shown = {u for u, r in req_first.items() if _in(r["ts"], a, b)}
        subs = {r["tg_id"] for r in evs if r["kind"] == "ch_subscribed"}
        got = shown & subs
        sub_first = _first_by([r for r in evs if r["kind"] == "ch_subscribed"], lambda r: r["tg_id"])
        out.append({
            "channel_chat_id": ch_chat, "title": channel_title(ch_chat, titles),
            "shown": len(shown), "subscribed": len(got), "not_subscribed": len(shown - got),
            "conversion": round(100.0 * len(got) / len(shown), 1) if shown else 0.0,
            "subscribed_in_period": len({u for u, r in sub_first.items() if _in(r["ts"], a, b)}),
            "unsubscribed": len({r["tg_id"] for r in evs if r["kind"] == "ch_unsubscribed" and _in(r["ts"], a, b)}),
            "resubscribed": len({r["tg_id"] for r in evs if r["kind"] == "ch_resubscribed" and _in(r["ts"], a, b)}),
        })
    for c in list_channels():
        if int(c["chat_id"]) not in chans:
            out.append({"channel_chat_id": int(c["chat_id"]), "title": c["title"] or str(c["chat_id"]), "shown": 0,
                        "subscribed": 0, "not_subscribed": 0, "conversion": 0.0, "subscribed_in_period": 0,
                        "unsubscribed": 0, "resubscribed": 0})
    out.sort(key=lambda r: order.get(r["channel_chat_id"], 10 ** 6))
    return out


def by_groups(code: str = "all", custom: tuple = None) -> list:
    a, b, *_ = period_bounds(code, custom)
    rows = _ev(("joined", "first_check", "required", "subscribed_after", "ch_subscribed"))
    groups = {}
    for r in rows:
        groups.setdefault(int(r["chat_id"] or 0), []).append(r)
    titles = {int(g["chat_id"]): g.get("title") or str(g["chat_id"]) for g in known_groups()}
    ch_titles = {}
    out = []
    for g, evs in groups.items():
        req_first = _first_by([r for r in evs if r["kind"] == "required"], lambda r: r["tg_id"])
        required = {u for u, r in req_first.items() if _in(r["ts"], a, b)}
        subs = {r["tg_id"] for r in evs if r["kind"] == "subscribed_after"}
        first = _first_by([r for r in evs if r["kind"] == "first_check"], lambda r: r["tg_id"])
        per_ch = {}
        for r in evs:
            if r["kind"] == "ch_subscribed" and _in(r["ts"], a, b):
                per_ch.setdefault(int(r["channel_chat_id"]), set()).add(r["tg_id"])
        out.append({
            "chat_id": g, "title": titles.get(g, str(g)),
            "joined": len({r["tg_id"] for r in evs if r["kind"] == "joined" and _in(r["ts"], a, b)}),
            "checked": len({u for u, r in first.items() if _in(r["ts"], a, b)}),
            "required": len(required), "subscribed": len(required & subs),
            "conversion": round(100.0 * len(required & subs) / len(required), 1) if required else 0.0,
            "channels": sorted(((channel_title(c, ch_titles), len(us)) for c, us in per_ch.items()),
                               key=lambda x: -x[1]),
        })
    out.sort(key=lambda r: -r["joined"] - r["checked"])
    return out


def daily(code: str = "7d", chat_id: int = None, custom: tuple = None) -> dict:
    a, b, label, da, dbb = period_bounds(code, custom)
    today = datetime.now(ALMATY).date()
    if da is None:
        da, dbb = today - timedelta(days=29), today
        label = f"последние 30 дней: {da.strftime('%d.%m.%Y')} – {dbb.strftime('%d.%m.%Y')}"
    rows = _ev(("subscribed_after",), chat_id=chat_id)
    first = _first_by(rows, lambda r: r["tg_id"])
    per_day = {}
    for r in first.values():
        d = parse_iso(r["ts"]).astimezone(ALMATY).date()
        per_day[d] = per_day.get(d, 0) + 1
    days, d = [], da
    while d <= dbb:
        days.append((d, per_day.get(d, 0)))
        d += timedelta(days=1)
    n_today = per_day.get(today, 0)
    n_yday = per_day.get(today - timedelta(days=1), 0)
    total = sum(n for _, n in days)
    return {"label": label, "days": days, "today": n_today, "yesterday": n_yday, "delta": n_today - n_yday,
            "avg": round(total / len(days), 1) if days else 0.0, "total": total}


def user_rows(code: str = "all", chat_id: int = None, channel_chat_id: int = None, custom: tuple = None) -> list:
    """Подробно по людям: кто, в какой группе, какой канал, когда что произошло."""
    a, b, *_ = period_bounds(code, custom)
    sql = ("SELECT mc.*, m.username, m.full_name, m.first_seen_at, m.access_at, m.verified_at, m.status AS user_status "
           "FROM gate_member_channels mc LEFT JOIN gate_members m ON m.chat_id=mc.chat_id AND m.tg_id=mc.tg_id "
           "WHERE 1=1")
    args = []
    if chat_id is not None:
        sql += " AND mc.chat_id=?"
        args.append(int(chat_id))
    if channel_chat_id is not None:
        sql += " AND mc.channel_chat_id=?"
        args.append(int(channel_chat_id))
    rows = [dict(r) for r in db.fetchall(sql + " ORDER BY mc.first_checked_at", tuple(args))]
    gtitles = {int(g["chat_id"]): g.get("title") or str(g["chat_id"]) for g in known_groups()}
    ctitles = {}
    out = []
    for r in rows:
        stamps = [r.get(k) for k in ("first_checked_at", "subscribed_at", "left_at", "resubscribed_at") if r.get(k)]
        if a is not None and not any(_in(s, a, b) for s in stamps):
            continue
        out.append({
            "user_id": r["tg_id"], "username": r.get("username") or "", "name": r.get("full_name") or "",
            "group_id": r["chat_id"], "group": gtitles.get(int(r["chat_id"]), str(r["chat_id"])),
            "channel_id": r["channel_chat_id"], "channel": channel_title(r["channel_chat_id"], ctitles),
            "was_before": "да" if r.get("was_member_before") else "нет",
            "required_at": fmt_local(r.get("first_required_at")), "subscribed_at": fmt_local(r.get("subscribed_at")),
            "access_at": fmt_local(r.get("access_at")), "left_at": fmt_local(r.get("left_at")),
            "resubscribed_at": fmt_local(r.get("resubscribed_at")),
            "state": {"member": "подписан", "missing": "не подписан", "left": "отписался"}.get(r.get("state"), ""),
        })
    return out


# ───────────────────────── выгрузка ─────────────────────────

def _report_tables(code: str, chat_id: int = None, channel_chat_id: int = None, custom: tuple = None):
    ov = overview(code, chat_id, custom)
    chans = by_channels(code, chat_id, custom)
    if channel_chat_id is not None:
        chans = [c for c in chans if int(c["channel_chat_id"]) == int(channel_chat_id)]
    groups = by_groups(code, custom)
    if chat_id is not None:
        groups = [g for g in groups if int(g["chat_id"]) == int(chat_id)]
    dy = daily(code if code != "all" else "30d", chat_id, custom)
    summary = [["Показатель", "Значение"],
               ["Период", ov["label"]],
               ["Проверено пользователей", ov["checked"]],
               ["Уже были подписаны", ov["already"]],
               ["Не были подписаны", ov["missing"]],
               ["Подписались после требования", ov["subscribed"]],
               ["Не подписались", ov["not_subscribed"]],
               ["Конверсия, %", ov["conversion"]],
               ["Получили доступ после проверки", ov["access"]],
               ["Отписались и снова ограничены", ov["relocked"]],
               ["Повторно подписались", ov["resubscribed"]],
               ["Новых участников в группах", ov["joined"]],
               ["Удалено сообщений", ov["deleted"]],
               ["Подписок за период (по дате подписки)", ov["subscribed_in_period"]]]
    t_ch = [["Канал", "ID", "Показан", "Подписались после требования", "Не подписались", "Конверсия, %",
             "Отписались позже", "Повторно подписались"]]
    for c in chans:
        t_ch.append([c["title"], c["channel_chat_id"], c["shown"], c["subscribed"], c["not_subscribed"],
                     c["conversion"], c["unsubscribed"], c["resubscribed"]])
    t_gr = [["Группа", "ID", "Новых участников", "Проверено", "Потребовалась подписка", "Подписались", "Конверсия, %"]]
    for g in groups:
        t_gr.append([g["title"], g["chat_id"], g["joined"], g["checked"], g["required"], g["subscribed"],
                     g["conversion"]])
    t_dy = [["Дата", "Подписались после требования"]] + [[d.strftime("%d.%m.%Y"), n] for d, n in dy["days"]]
    users = user_rows(code, chat_id, channel_chat_id, custom)
    t_us = [["User ID", "Username", "Имя", "Группа", "ID группы", "Канал", "ID канала", "Был подписан до проверки",
             "Бот потребовал подписку", "Подписался", "Получил доступ", "Отписался", "Подписался снова", "Сейчас"]]
    for u in users:
        t_us.append([u["user_id"], ("@" + u["username"]) if u["username"] else "", u["name"], u["group"], u["group_id"],
                     u["channel"], u["channel_id"], u["was_before"], u["required_at"], u["subscribed_at"],
                     u["access_at"], u["left_at"], u["resubscribed_at"], u["state"]])
    return [("Итог", summary), ("По каналам", t_ch), ("По группам", t_gr), ("По дням", t_dy), ("Пользователи", t_us)]


def export_csv(code: str, chat_id: int = None, channel_chat_id: int = None, custom: tuple = None) -> bytes:
    """Один CSV: разделы друг под другом. «;» и BOM — Excel в русской раскладке открывает сразу."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    for i, (name, rows) in enumerate(_report_tables(code, chat_id, channel_chat_id, custom)):
        if i:
            w.writerow([])
        w.writerow([f"== {name} =="])
        for row in rows:
            w.writerow(row)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def export_xlsx(code: str, chat_id: int = None, channel_chat_id: int = None, custom: tuple = None) -> bytes:
    return xlsx_bytes(_report_tables(code, chat_id, channel_chat_id, custom))


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_bytes(sheets: list) -> bytes:
    """Простой файл Excel (.xlsx) без сторонних библиотек: несколько листов, жирная шапка."""
    out = io.BytesIO()
    z = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
    names = []
    for i, (name, _) in enumerate(sheets, 1):
        clean = re.sub(r"[\[\]:*?/\\]", " ", str(name))[:31] or f"Лист{i}"
        names.append(clean)
    z.writestr("[Content_Types].xml",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
               + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                         for i in range(1, len(sheets) + 1)) + '</Types>')
    z.writestr("_rels/.rels",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
               '</Relationships>')
    z.writestr("xl/workbook.xml",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
               + "".join(f'<sheet name="{_xesc(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                         for i, n in enumerate(names, 1)) + '</sheets></workbook>')
    z.writestr("xl/_rels/workbook.xml.rels",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                         for i in range(1, len(sheets) + 1))
               + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')
    z.writestr("xl/styles.xml",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
               '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
               '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
               '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
               '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
               '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
               '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
               '</styleSheet>')
    for i, (_, rows) in enumerate(sheets, 1):
        widths = {}
        body = []
        for ri, row in enumerate(rows, 1):
            cells = []
            for ci, v in enumerate(row, 1):
                ref = f"{_col(ci)}{ri}"
                style = ' s="1"' if ri == 1 else ""
                if isinstance(v, bool):
                    v = "да" if v else "нет"
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cells.append(f'<c r="{ref}"{style}><v>{v}</v></c>')
                    w = len(str(v))
                elif v is None or v == "":
                    continue
                else:
                    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(v))
                    cells.append(f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{_xesc(txt)}</t></is></c>')
                    w = len(txt)
                widths[ci] = max(widths.get(ci, 0), min(60, w + 2))
            body.append(f'<row r="{ri}">' + "".join(cells) + "</row>")
        cols = ("<cols>" + "".join(f'<col min="{c}" max="{c}" width="{max(8, w)}" customWidth="1"/>'
                                   for c, w in sorted(widths.items())) + "</cols>") if widths else ""
        z.writestr(f"xl/worksheets/sheet{i}.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                   + cols + "<sheetData>" + "".join(body) + "</sheetData></worksheet>")
    z.close()
    return out.getvalue()
