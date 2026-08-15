"""
Мгновенная реакция на отписку от обязательного канала.

Telegram присылает боту событие chat_member, как только человек выходит из
канала (бот должен быть админом канала). Ловим его сразу: гасим кэш проверки,
чтобы сайт и мини-приложение тут же закрыли материалы, и пишем человеку, что
для продолжения нужно вернуть подписку. Ждать, пока протухнет кэш, не нужно.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup

import database as db

router = Router(name="subscription_watch")
log = logging.getLogger(__name__)

GONE = {"left", "kicked"}
BACK = {"member", "administrator", "creator"}

MSG = ("🔒 <b>Для продолжения работы необходимо подписаться на наш канал.</b>\n\n"
       "Вы отписались, поэтому уроки, конспекты и тесты снова закрыты. "
       "Подпишитесь и нажмите «Я подписался» — доступ вернётся сразу.")


def _required_channels() -> set:
    """Каналы, подписка на которые обязательна (в нижнем регистре, без @)."""
    rows = db.fetchall(
        "SELECT channel_username FROM required_channels WHERE COALESCE(is_active,1)=1")
    return {(r["channel_username"] or "").lstrip("@").lower()
            for r in rows if r.get("channel_username")}


def drop_subscription_cache(tg_id: int) -> None:
    """Забыть, что мы про этого человека знали — пусть проверят заново."""
    try:
        from webapp import learning
        for key in [k for k in learning._sub_cache if k[0] == tg_id]:
            learning._sub_cache.pop(key, None)
    except Exception as e:
        log.warning("drop sub cache: %s", e)


@router.chat_member()
async def on_channel_member_changed(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    uname = (chat.username or "").lstrip("@").lower()
    if uname and uname not in _required_channels():
        return                      # канал не наш — не вмешиваемся

    tg_id = event.new_chat_member.user.id
    old = getattr(event.old_chat_member, "status", "")
    new = getattr(event.new_chat_member, "status", "")

    if new in GONE and old not in GONE:
        drop_subscription_cache(tg_id)
        link = f"https://t.me/{chat.username}" if chat.username else None
        rows = []
        if link:
            rows.append([InlineKeyboardButton(text="📢 Подписаться", url=link)])
        rows.append([InlineKeyboardButton(text="✅ Я подписался",
                                          callback_data="sub:recheck")])
        try:
            await bot.send_message(tg_id, MSG, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception as e:
            log.info("не смог написать %s об отписке: %s", tg_id, e)
        log.info("Пользователь %s отписался от @%s — доступ закрыт", tg_id, uname)

    elif new in BACK and old in GONE:
        drop_subscription_cache(tg_id)   # вернулся — сразу открываем
        log.info("Пользователь %s снова подписан на @%s", tg_id, uname)


@router.callback_query(F.data == "sub:recheck")
async def cb_recheck(call, bot: Bot):
    """Перепроверить подписку по нажатию, не дожидаясь ничего."""
    from services.subscription_service import check_all_subscriptions
    drop_subscription_cache(call.from_user.id)
    missing = await check_all_subscriptions(bot, call.from_user.id)
    if missing:
        await call.answer("Подписка пока не видна. Подпишитесь и нажмите ещё раз.",
                          show_alert=True)
        return
    await call.answer("Готово, доступ открыт!")
    try:
        await call.message.edit_text("✅ Подписка подтверждена — можно продолжать.")
    except Exception:
        pass
