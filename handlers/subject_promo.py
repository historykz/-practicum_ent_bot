"""
Витрина предмета: карточка, которая открывается по ссылке из канала.

Админ берёт в админке ссылку вида t.me/бот?start=subj_12 и публикует её.
Человек жмёт — попадает в бота (то есть уже зарегистрирован) и сразу видит,
что за предмет, сколько уроков, какие бесплатные, а какие платные, и кнопку
«Открыть каталог». Без этого ссылка вела бы просто в /start, и предмет
пришлось бы искать руками.
"""
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
import utils

log = logging.getLogger(__name__)


def site_url() -> str:
    url = (getattr(config, "SITE_URL", "") or "").strip().rstrip("/")
    return url or "https://practicumentbot-production.up.railway.app"


def subject_link(subject_id: int) -> str:
    """Ссылка-витрина предмета для публикации в канале."""
    return f"https://t.me/{config.WEB_BOT_USERNAME}?start=subj_{subject_id}"


def promo_data(subject_id: int):
    """Что показать в карточке: предмет, счётчики, первые бесплатные уроки."""
    subj = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'",
                       (subject_id,))
    if not subj:
        return None
    subj = dict(subj)
    rows = db.fetchall(
        "SELECT l.id, l.title, l.is_paid, l.status FROM lessons l "
        "JOIN sections s ON s.id = l.section_id "
        "WHERE s.subject_id=? AND l.status='open' "
        "ORDER BY s.sort_order, s.id, l.sort_order, l.id", (subject_id,))
    lessons = [dict(r) for r in rows]
    free = [l for l in lessons if not l["is_paid"]]
    paid = [l for l in lessons if l["is_paid"]]
    sections = db.fetchone(
        "SELECT COUNT(*) AS c FROM sections WHERE subject_id=?", (subject_id,))["c"]
    return {"subject": subj, "lessons": lessons, "free": free, "paid": paid,
            "sections": sections}


def build_promo(subject_id: int):
    """Текст + клавиатура витрины. Возвращает (text, kb) или (None, None)."""
    data = promo_data(subject_id)
    if not data:
        return None, None
    subj, free, paid = data["subject"], data["free"], data["paid"]

    lines = [f"📚 <b>{utils.escape_html(subj['title'])}</b>"]
    if (subj.get("description") or "").strip():
        lines.append(utils.escape_html(subj["description"].strip()))
    lines.append("")
    lines.append(f"📂 Разделов: <b>{data['sections']}</b>   "
                 f"📖 Уроков: <b>{len(data['lessons'])}</b>")
    if free:
        lines.append(f"🆓 Бесплатных уроков: <b>{len(free)}</b> — можно начать прямо сейчас")
    if paid:
        lines.append(f"💎 По подписке: <b>{len(paid)}</b>")

    if free:
        lines.append("\n<b>Открыто бесплатно:</b>")
        for l in free[:5]:
            lines.append(f"🆓 {utils.escape_html(l['title'])}")
        if len(free) > 5:
            lines.append(f"<i>…и ещё {len(free) - 5}</i>")
    if paid:
        lines.append("\n<b>Что внутри по подписке:</b>")
        for l in paid[:5]:
            lines.append(f"💎 {utils.escape_html(l['title'])}")
        if len(paid) > 5:
            lines.append(f"<i>…и ещё {len(paid) - 5}</i>")

    lines.append("\n👇 Открой каталог и посмотри уроки целиком.")

    rows = [[InlineKeyboardButton(text="📚 Открыть каталог предмета",
                                  url=f"{site_url()}/learn/{subject_id}")]]
    if free:
        rows.append([InlineKeyboardButton(
            text="🆓 Начать с бесплатного урока",
            url=f"{site_url()}/learn/lesson/{free[0]['id']}")])
    if paid:
        rows.append([InlineKeyboardButton(text="💎 Оформить Премиум",
                                          callback_data="m:premium")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_subject_promo(message: Message, subject_id: int) -> bool:
    """Отправить витрину. False — если предмет не найден или скрыт."""
    text, kb = build_promo(subject_id)
    if not text:
        return False
    try:
        from webapp import shortcuts as sc
        subj = db.fetchone("SELECT * FROM subjects WHERE id=?", (subject_id,))
        if subj is not None and sc.subject_mode(dict(subj)) == sc.PRIVATE:
            return False   # приватный предмет по ссылке не показываем
    except Exception:
        pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    return True
