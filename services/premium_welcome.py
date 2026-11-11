"""
Что бот присылает сразу после оплаты Премиума.

Два сообщения: поздравление с привилегиями и просьба заполнить форму для
карточек Quizlet. Тексты, подписи и ссылки кнопок админ меняет в панели —
в коде они не зашиты. Каждое сообщение можно выключить отдельно, между
ними есть настраиваемая пауза.

Числа в тексте подставляются из настоящих данных: сколько дней куплено,
до какого числа действует и сколько платных тестов открылось.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from services import reminder_service as rs

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))


def _paid_tests_count() -> int:
    """Сколько платных тестов открылось человеку с Премиумом."""
    row = db.fetchone(
        "SELECT COUNT(DISTINCT t.id) AS c FROM tests t "
        "WHERE t.status='active' AND COALESCE(t.is_paid,0)=1")
    if row and row["c"]:
        return row["c"]
    # Если платность отмечена на уроках, а не на тестах — считаем по урокам
    row = db.fetchone(
        "SELECT COUNT(DISTINCT l.test_id) AS c FROM lessons l "
        "WHERE COALESCE(l.is_paid,0)=1 AND l.test_id IS NOT NULL")
    return (row or {"c": 0})["c"] or 0


def _fill(text: str, days: int, until: str, tests: int) -> str:
    return (text.replace("{days}", str(days))
                .replace("{until}", until or "—")
                .replace("{tests}", str(tests)))


def _keyboard(camp: dict) -> InlineKeyboardMarkup:
    rows = []
    for t, u in ((camp.get("button_text"), camp.get("button_url")),
                 (camp.get("button2_text"), camp.get("button2_url"))):
        t = (t or "").strip()
        u = (u or "").strip()
        if not t:
            continue
        if u == "tests":
            # особый случай: список платных тестов внутри бота
            rows.append([InlineKeyboardButton(text=t, callback_data="m:tests")])
        elif u.startswith("http"):
            rows.append([InlineKeyboardButton(text=t, url=u)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def already_greeted(charge_id: str) -> bool:
    """Этот платёж уже отработан? Защита от повторной выдачи и дублей."""
    if not charge_id:
        return False
    row = db.fetchone(
        "SELECT id FROM auth_events WHERE event='premium_welcome' AND details=?",
        (str(charge_id)[:300],))
    return row is not None


def mark_greeted(tg_id: int, charge_id: str) -> None:
    try:
        db.execute(
            "INSERT INTO auth_events (tg_id, event, details) VALUES (?,?,?)",
            (tg_id, "premium_welcome", str(charge_id)[:300]))
    except Exception as e:
        log.warning("mark_greeted: %s", e)


async def send_after_purchase(bot, tg_id: int, days: int, until: str,
                              charge_id: str = "") -> int:
    """Отправить оба сообщения. Возвращает, сколько ушло.

    Повторный вызов с тем же чеком ничего не отправит: платёж уже отмечен
    как отработанный. Премиум при этом не выдаётся здесь — только сообщения.
    """
    if charge_id and await asyncio.to_thread(already_greeted, charge_id):
        log.info("Платёж %s уже поздравлен — второй раз не шлём", charge_id)
        return 0

    tests = await asyncio.to_thread(_paid_tests_count)
    sent = 0
    order = await asyncio.to_thread(_ordered_campaigns)
    for i, camp in enumerate(order):
        if not camp.get("enabled"):
            continue
        text = _fill(camp["message_text"], days, until, tests)
        kb = _keyboard(camp)
        try:
            await bot.send_message(tg_id, text, reply_markup=kb,
                                   parse_mode="HTML", disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            log.warning("Поздравление %s не ушло: %s", camp["campaign_key"], e)
            continue
        # Пауза перед следующим сообщением — чтобы не сыпались одной пачкой
        if i + 1 < len(order):
            delay = max(0, int(camp.get("safe_delay_seconds") or 0))
            if delay:
                await asyncio.sleep(min(delay, 30))

    if charge_id and sent:
        await asyncio.to_thread(mark_greeted, tg_id, charge_id)
    return sent


def _ordered_campaigns() -> list:
    """Порядок сообщений задаёт админ (sort_order), по умолчанию — как в списке."""
    out = []
    for i, key in enumerate((rs.PREMIUM_OK, rs.PREMIUM_QUIZLET)):
        c = rs.get_campaign(key)
        c["_default_order"] = i
        out.append(c)
    out.sort(key=lambda c: (int(c.get("sort_order") or 0), c["_default_order"]))
    return out
