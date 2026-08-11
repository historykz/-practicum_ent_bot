"""
Анонс нового теста пользователям (по профилю или всем).
Кнопка «📢 Анонсировать тест» в карточке теста админа.
- Бесплатный: «пройди обязательно» + кнопка
- Платный: + цены + купить/подарить/менеджер
- Приватный: анонс запрещён
- Профильная категория → только юзерам с ней в профиле (+ 'other')
- Обязательная категория → всем
"""
import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.types import (CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from filters import IsAdmin

router = Router(name="announce")
log = logging.getLogger(__name__)


def _target_users(test: dict) -> tuple:
    """
    Возвращает (profile_users, all_users, cat_info).
    profile_users — юзеры по профилю категории (None если категория
    обязательная/нет — тогда профильная рассылка не отличается от всех).
    """
    all_rows = db.fetchall(
        "SELECT tg_id, language FROM users WHERE tg_id IS NOT NULL "
        "AND COALESCE(is_blocked,0)=0")
    cat = None
    if test.get('category_id'):
        cat = db.fetchone("SELECT * FROM test_categories WHERE id=?",
                           (test['category_id'],))
    if not cat or cat.get('is_required'):
        return None, all_rows, cat
    cid = str(cat['id'])
    profile = []
    for u in all_rows:
        row = db.fetchone("SELECT profile_subjects FROM users WHERE tg_id=?",
                           (u['tg_id'],))
        subs = (row.get('profile_subjects') or '') if row else ''
        parts = [p.strip() for p in subs.split(',') if p.strip()]
        if cid in parts or 'other' in parts:
            profile.append(u)
    return profile, all_rows, cat


@router.callback_query(F.data.startswith("admannounce:"), IsAdmin())
async def cb_announce_ask(call: CallbackQuery):
    test_id = int(call.data.split(":")[1])
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        await call.answer()
        return
    if test.get('is_private'):
        await call.answer(
            "🔐 Тест приватный — анонс не отправляется.", show_alert=True)
        return
    qcount = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test_id,))['c']
    if qcount == 0:
        await call.answer("⚠️ В тесте нет вопросов.", show_alert=True)
        return
    profile_users, all_users, cat = _target_users(test)
    kb = InlineKeyboardBuilder()
    if profile_users is not None:
        cname = cat['name'] if cat else ''
        kb.button(
            text=f"🎯 Только профильным ({cname}) — {len(profile_users)} чел.",
            callback_data=f"annsend:{test_id}:profile")
    kb.button(text=f"👥 Всем пользователям — {len(all_users)} чел.",
              callback_data=f"annsend:{test_id}:all")
    kb.button(text="❌ Не отправлять", callback_data="annsend:cancel")
    kb.adjust(1)
    await call.message.answer(
        f"📢 <b>Анонс теста «{utils.escape_html(test['title'])}»</b>\n"
        f"📚 {qcount} вопросов · "
        f"{'💎 платный' if test.get('is_paid') else '🆓 бесплатный'}\n\n"
        f"Кому отправить уведомление?",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "annsend:cancel", IsAdmin())
async def cb_announce_cancel(call: CallbackQuery):
    try:
        await call.message.edit_text("❌ Анонс отменён.")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("annsend:"), IsAdmin())
async def cb_announce_send(call: CallbackQuery, bot: Bot):
    parts = call.data.split(":")
    if parts[1] == "cancel":
        return
    test_id = int(parts[1])
    mode = parts[2]
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        await call.answer()
        return
    profile_users, all_users, cat = _target_users(test)
    targets = profile_users if (mode == "profile" and profile_users is not None) \
              else all_users
    await call.answer()
    try:
        await call.message.edit_text(
            f"📤 Отправляю анонс {len(targets)} пользователям…")
    except Exception:
        pass
    sent = await _broadcast_new_test(bot, test, targets, cat)
    try:
        await call.message.edit_text(
            f"✅ Анонс отправлен!\n📨 Доставлено: {sent} из {len(targets)}")
    except Exception:
        pass


async def _broadcast_new_test(bot: Bot, test: dict, targets: list,
                                cat: dict) -> int:
    qcount = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
        (test['id'],))['c']
    cat_name = (cat['name'] if cat else None) or 'новой теме'
    title = utils.escape_html(test['title'])
    is_paid = bool(test.get('is_paid'))
    stars = test.get('price_stars') or 0
    tenge = test.get('price') or 0
    time_per_q = test.get('time_per_question') or 30

    bot_un = getattr(config, 'BOT_USERNAME', '') or ''
    if not bot_un:
        try:
            bot_un = (await bot.get_me()).username
        except Exception:
            bot_un = ''
    pass_url = f"https://t.me/{bot_un}?start=test_{test['id']}"

    sent = 0
    for u in targets:
        lang = u.get('language') or 'ru'
        if is_paid:
            price_parts = []
            if stars:
                price_parts.append(f"{stars} ⭐️")
            if tenge:
                price_parts.append(f"{tenge} ₸")
            price_str = " или ".join(price_parts) if price_parts else "—"
            if lang == 'kz':
                text = (f"🆕 <b>{cat_name} бойынша жаңа тест шықты!</b>\n\n"
                        f"💎 «{title}»\n"
                        f"📚 {qcount} сұрақ  ·  ⏱ әр сұраққа {time_per_q} сек\n"
                        f"💰 Бағасы: {price_str}\n\n"
                        f"Міндетті түрде өтіп көр! 🔥")
            else:
                text = (f"🆕 <b>Вышел новый тест по {cat_name}!</b>\n\n"
                        f"💎 «{title}»\n"
                        f"📚 {qcount} вопросов  ·  ⏱ {time_per_q} сек на вопрос\n"
                        f"💰 Цена: {price_str}\n\n"
                        f"Пройди обязательно! 🔥")
            rows = []
            if stars:
                rows.append([InlineKeyboardButton(
                    text=f"⭐️ Купить тест — {stars} ⭐️",
                    callback_data=f"buy:test:{test['id']}")])
                rows.append([InlineKeyboardButton(
                    text=f"🎁 Купить другу — {stars} ⭐️",
                    callback_data=f"buy:gift:{test['id']}")])
            rows.append([InlineKeyboardButton(
                text="💬 Написать менеджеру",
                url=f"https://t.me/{config.MANAGER_USERNAME}")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
        else:
            if lang == 'kz':
                text = (f"🆕 <b>{cat_name} бойынша жаңа тест шықты!</b>\n\n"
                        f"«{title}»\n"
                        f"📚 {qcount} сұрақ  ·  ⏱ әр сұраққа {time_per_q} сек\n"
                        f"🆓 Тегін\n\n"
                        f"Міндетті түрде өтіп көр! 💪")
            else:
                text = (f"🆕 <b>Вышел новый тест по {cat_name}!</b>\n\n"
                        f"«{title}»\n"
                        f"📚 {qcount} вопросов  ·  ⏱ {time_per_q} сек на вопрос\n"
                        f"🆓 Бесплатно\n\n"
                        f"Пройди обязательно! 💪")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 Пройти тест", url=pass_url)]])
        try:
            await bot.send_message(u['tg_id'], text, reply_markup=kb,
                                     parse_mode="HTML",
                                     disable_web_page_preview=True)
            sent += 1
        except Exception:
            pass
        if sent and sent % 25 == 0:
            await asyncio.sleep(1)
    return sent
