"""
Реферальная программа — интерфейс для пользователя.
Показывает ссылку, статистику, кнопку приглашения.
"""
import logging

from aiogram import Router, F, Bot
from aiogram.types import (CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from services import referral_reward_service as rrs

router = Router(name="referral")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "m:referral")
async def cb_referral_menu(call: CallbackQuery, user: dict):
    await show_referral(call, user)
    await call.answer()


async def show_referral(call: CallbackQuery, user: dict):
    """Показать реферальную программу пользователю."""
    tg_id = call.from_user.id
    bot_un = getattr(config, 'BOT_USERNAME', '') or ''
    if not bot_un:
        try:
            bot_un = (await call.bot.get_me()).username
        except Exception:
            bot_un = ''
    link = rrs.get_referral_link(bot_un, tg_id)
    stats = rrs.count_referrals(tg_id)
    counted = stats['counted']
    total = stats['total']
    need = rrs._friends_needed()
    till_reward = need - (counted % need) if counted % need != 0 else need
    if counted > 0 and counted % need == 0:
        till_reward = need  # ровно получил, до следующей снова 10

    progress = counted % need
    bar = "🟩" * progress + "⬜️" * (need - progress)

    text = (
        f"🎁 <b>Пригласи друзей — получи Премиум!</b>\n\n"
        f"За каждые <b>{need} друзей</b>, которые перейдут по твоей ссылке "
        f"и <b>подпишутся на канал</b>, ты получаешь "
        f"<b>{rrs._reward_days()} дней Премиума</b> бесплатно! 🏆\n\n"
        f"📊 <b>Твоя статистика:</b>\n"
        f"👥 Приглашено всего: {total}\n"
        f"✅ Засчитано (подписаны): <b>{counted}</b>\n\n"
        f"Прогресс до награды:\n{bar}\n"
        f"Осталось пригласить: <b>{till_reward}</b>\n\n"
        f"🔗 <b>Твоя ссылка:</b>\n<code>{link}</code>"
    )
    kb = InlineKeyboardBuilder()
    # Кнопка поделиться через инлайн
    kb.button(text="📤 Пригласить друзей",
              switch_inline_query=f"ref")
    kb.button(text="🔄 Обновить статистику", callback_data="m:referral")
    kb.button(text="🔙 Назад", callback_data="m:menu")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                      parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
