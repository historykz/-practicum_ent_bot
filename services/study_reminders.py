"""
Умные напоминания об учёбе.

Бот смотрит не на календарь, а на то, что человек делал. Один и тот же
третий день без занятий выглядит по-разному: кто-то вообще не заходил,
кто-то читает конспекты и не сдаёт ДЗ, кто-то начал задание и бросил.
Тексты для каждого случая свои, и пишет их админ.

Тон нарастает: сначала мягко, потом настойчивее, потом строго — но без
грубости. Как только человек возвращается к занятиям, цепочка сбрасывается
и он получает короткую похвалу, а не очередное «ты ничего не делаешь».
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import database as db
import utils
from services import motivation_service as ms
from services import study_settings as ss
from services import study_tracker as st

log = logging.getLogger(__name__)

ALMATY = timezone(timedelta(hours=5))
CHECK_EVERY = 30 * 60          # как часто просыпаемся, секунды


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _local_hour() -> int:
    return _now().astimezone(ALMATY).hour


# ---------- Кого вообще трогаем ----------

def candidates() -> list:
    """Ученики, за которыми следим. По умолчанию — только с Премиумом."""
    st.ensure_schema()
    premium_only = ss.get_bool("study_premium_only")
    if premium_only:
        rows = db.fetchall(
            "SELECT DISTINCT u.tg_id FROM users u "
            "JOIN premium_users p ON p.user_id = u.id "
            "WHERE u.tg_id IS NOT NULL "
            "AND (p.expires_at IS NULL OR p.expires_at = '' "
            "     OR datetime(p.expires_at) > datetime('now'))")
    else:
        rows = db.fetchall("SELECT tg_id FROM users WHERE tg_id IS NOT NULL")
    return [r["tg_id"] for r in rows]


# ---------- Что именно написать ----------

def decide(data: dict) -> tuple:
    """Решает, писать ли человеку и каким текстом.

    Возвращает (ключ шаблона, ступень строгости) или (None, 0), если писать
    не нужно. Ступень нужна, чтобы не повторять одно и то же письмо.
    """
    idle = data.get("days_idle")
    warn_level = data.get("warn_level") or 0
    d1 = ss.get_int("study_warn1_days", 2)
    d2 = ss.get_int("study_warn2_days", 3)
    d3 = ss.get_int("study_warn3_days", 4)
    case = data.get("case")

    # Ни разу не занимался, хотя доступ есть — мягко зовём начать
    if idle is None:
        if warn_level >= 1:
            return None, 0
        return "never_started", 1

    # Человек читает конспекты, но не сдаёт задания — это не «безделье»,
    # и писать ему «ты ничего не делаешь» было бы неправдой.
    if case in ("reading_no_hw", "hw_unfinished") and idle < d2:
        if warn_level >= 1:
            return None, 0
        return ("reading_no_hw" if case == "reading_no_hw" else "hw_unfinished"), 1

    if idle >= d3:
        if warn_level >= 3:
            return None, 0            # строже уже некуда, повторяться не будем
        return "warn3", 3
    if idle >= d2:
        if warn_level >= 2:
            return None, 0
        # На третий день учитываем, что именно человек делает
        if case in ("reading_no_hw", "hw_unfinished"):
            return "hw_unfinished", 2
        return "warn2", 2
    if idle >= d1:
        if warn_level >= 1:
            return None, 0
        if case == "reading_no_hw":
            return "reading_no_hw", 1
        return "warn1", 1

    # Занимается — ничего не пишем. Разве что хвостов много.
    if data.get("unfinished_count", 0) >= 3 and warn_level == 0 and idle >= 1:
        return "many_unfinished", 1
    return None, 0


# ---------- Ограничения, чтобы не спамить ----------

def may_send(tg_id: int, data: dict) -> bool:
    hour = _local_hour()
    quiet_from = ss.get_int("study_quiet_from", 22)
    quiet_to = ss.get_int("study_quiet_to", 9)
    if quiet_from <= hour or hour < quiet_to:
        return False                   # ночью не тревожим

    start = ss.get_int("study_send_hour", 18)
    until = ss.get_int("study_send_until", 21)
    if not (start <= hour <= until):
        return False                   # шлём в отведённое админом окно

    gap = ss.get_int("study_min_gap_hours", 20)
    last = st.get_state(tg_id).get("last_notify_at")
    if last:
        passed = st.days_since(last)
        if passed is not None and passed * 24 < gap:
            return False               # недавно уже писали
    return True


# ---------- Отправка ----------

async def send_one(bot, tg_id: int) -> bool:
    """Разобраться с одним учеником: надо ли писать и что именно."""
    data = st.profile(tg_id)
    key, level = decide(data)
    if not key:
        return False
    if not may_send(tg_id, data):
        return False

    text = ss.render(ss.template(key), data)
    if not text.strip():
        return False                   # админ выключил или очистил шаблон

    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        log.info("напоминание %s: %s", tg_id, e)
        return False

    stamp = _iso(_now())
    st.set_state(tg_id, warn_level=level, last_warn_at=stamp, last_notify_at=stamp)
    motivation_id = None

    # Мотивашку добавляем к строгим письмам — чтобы после «нажима» человек
    # получал ещё и поддержку, а не только упрёк.
    if ss.get_bool("study_motivation_enabled") and level >= 2:
        item = ms.pick_for(tg_id)
        if item:
            await asyncio.sleep(1.0)
            if await ms.send(bot, tg_id, item):
                motivation_id = item["id"]
                st.set_state(tg_id, last_motivation_at=stamp)

    _log_notification(tg_id, key, text, motivation_id)
    return True


def _log_notification(tg_id: int, kind: str, text: str,
                      motivation_id=None) -> None:
    try:
        db.execute(
            "INSERT INTO study_notifications (tg_id, kind, text, motivation_id, sent_at) "
            "VALUES (?,?,?,?,?)",
            (tg_id, kind, (text or "")[:800], motivation_id, _iso(_now())))
    except Exception:
        pass


def _needs_praise(tg_id: int) -> bool:
    """Стоит ли похвалить: человека ругали, он вернулся, и мы ещё не хвалили.

    Сдача ДЗ сама снимает ступень строгости, поэтому ориентируемся на метку
    последнего предупреждения и на то, что после него похвалы ещё не было.
    """
    state = st.get_state(tg_id)
    warned = state.get("last_warn_at")
    if not warned:
        return False
    try:
        row = db.fetchone(
            "SELECT id FROM study_notifications WHERE tg_id=? AND kind='returned' "
            "AND datetime(sent_at) >= datetime(?) LIMIT 1", (tg_id, warned))
        return row is None
    except Exception:
        return False


async def praise_returned(bot, tg_id: int) -> bool:
    """Похвалить того, кто вернулся к занятиям после предупреждений.

    Похвала — тоже автоматическое сообщение, и она обязана соблюдать те же
    правила, что и обычное напоминание: не ночью, только в отведённое окно,
    не сразу вслед за другим авто-письмом. Без этой проверки поздравление
    «вот, другое дело» могло прилететь в три часа ночи или через 10 минут
    после только что отправленного строгого предупреждения.
    """
    if not _needs_praise(tg_id):
        return False
    data = st.profile(tg_id)
    if not may_send(tg_id, data):
        return False
    text = ss.render(ss.template("returned"), data)
    if not text.strip():
        return False
    try:
        await bot.send_message(tg_id, text)
    except Exception:
        return False
    stamp = _iso(_now())
    st.set_state(tg_id, warn_level=0, last_notify_at=stamp, returned_at=stamp)
    _log_notification(tg_id, "returned", text)
    return True


async def run_once(bot) -> dict:
    """Один проход по всем ученикам. Возвращает сводку — её видно в логах."""
    if not ss.get_bool("study_enabled"):
        return {"skipped": True}
    sent = 0
    praised = 0
    people = candidates()
    for tg_id in people:
        try:
            # Сначала — вернувшиеся: им похвала вместо нового упрёка
            idle = st.days_without_study(tg_id)
            if idle is not None and idle < 1 and _needs_praise(tg_id):
                if await praise_returned(bot, tg_id):
                    praised += 1
                    continue
            if await send_one(bot, tg_id):
                sent += 1
                await asyncio.sleep(0.3)      # бережём лимиты Telegram
        except Exception as e:
            log.warning("контроль обучения, ученик %s: %s", tg_id, e)
    return {"checked": len(people), "sent": sent, "praised": praised}


# ---------- Сводка для админа ----------

def summary() -> dict:
    """Кто отстаёт — цифрами. Этим же пользуется экран «Отстающие»."""
    groups = {"d2": [], "d3": [], "d4": [], "reading_no_hw": [], "never": []}
    d1 = ss.get_int("study_warn1_days", 2)
    d2 = ss.get_int("study_warn2_days", 3)
    d3 = ss.get_int("study_warn3_days", 4)
    for tg_id in candidates():
        data = st.profile(tg_id)
        idle = data.get("days_idle")
        if idle is None:
            groups["never"].append(data)
            continue
        if data.get("case") == "reading_no_hw":
            groups["reading_no_hw"].append(data)
        if idle >= d3:
            groups["d4"].append(data)
        elif idle >= d2:
            groups["d3"].append(data)
        elif idle >= d1:
            groups["d2"].append(data)
    return groups


def summary_text(groups: dict = None) -> str:
    groups = groups if groups is not None else summary()
    d1 = ss.get_int("study_warn1_days", 2)
    d2 = ss.get_int("study_warn2_days", 3)
    d3 = ss.get_int("study_warn3_days", 4)
    return (
        "📊 <b>Отстающие ученики</b>\n\n"
        f"🟡 {d1} дня без учёбы — <b>{len(groups['d2'])}</b>\n"
        f"🟠 {d2} дня — <b>{len(groups['d3'])}</b>\n"
        f"🔴 {d3}+ дней — <b>{len(groups['d4'])}</b>\n"
        f"📚 Открыли конспект, но не сделали ДЗ — <b>{len(groups['reading_no_hw'])}</b>\n"
        f"💤 Ни разу не начинали — <b>{len(groups['never'])}</b>"
    )


async def send_admin_report(bot) -> bool:
    if not ss.get_bool("study_report_enabled"):
        return False
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    text = summary_text()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👀 Посмотреть учеников",
                             callback_data="lag:list:all:0")]])
    ok = False
    for admin_id in _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML",
                                   reply_markup=kb)
            ok = True
        except Exception:
            pass
    return ok


def _admin_ids() -> list:
    ids = set()
    try:
        for x in (getattr(utils, "ADMIN_IDS", None) or []):
            ids.add(int(x))
    except Exception:
        pass
    try:
        import config
        for x in (getattr(config, "ADMIN_IDS", None) or []):
            ids.add(int(x))
        owner = getattr(config, "OWNER_ID", None)
        if owner:
            ids.add(int(owner))
    except Exception:
        pass
    return list(ids)


# ---------- Фоновый цикл ----------

async def loop(bot) -> None:
    """Просыпается раз в полчаса: рассылает напоминания и дневной отчёт."""
    await asyncio.sleep(60)          # даём боту спокойно подняться
    last_report_date = ""
    while True:
        try:
            if ss.get_bool("study_enabled"):
                res = await run_once(bot)
                if res.get("sent") or res.get("praised"):
                    log.info("контроль обучения: %s", res)

            hour = _local_hour()
            today = _now().astimezone(ALMATY).strftime("%Y-%m-%d")
            if (ss.get_bool("study_report_enabled")
                    and hour == ss.get_int("study_report_hour", 20)
                    and last_report_date != today):
                await send_admin_report(bot)
                last_report_date = today
        except Exception as e:
            log.warning("цикл контроля обучения: %s", e)
        await asyncio.sleep(CHECK_EVERY)
