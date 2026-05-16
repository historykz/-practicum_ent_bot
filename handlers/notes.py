"""Хендлеры конспектов."""
import logging

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery

import config
import database as db
import utils
from locales import t
from keyboards import (notes_list_kb, note_card_kb, note_page_kb,
                        paid_note_kb, subscription_kb, back_kb)
from services import notes_service, subscription_service

router = Router(name="notes")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "m:notes")
async def cb_notes_menu(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    notes = notes_service.list_active_notes(lang)
    if not notes:
        text = t("notes_catalog", lang) + "\n\n" + t("no_notes", lang)
    else:
        text = t("notes_catalog", lang)
    try:
        await call.message.edit_text(text, reply_markup=notes_list_kb(notes, lang, 0))
    except Exception:
        await call.message.answer(text, reply_markup=notes_list_kb(notes, lang, 0))
    await call.answer()


@router.callback_query(F.data.startswith("notes_page:"))
async def cb_notes_page(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    notes = notes_service.list_active_notes(lang)
    try:
        await call.message.edit_reply_markup(reply_markup=notes_list_kb(notes, lang, page))
    except Exception:
        pass
    await call.answer()


async def show_note_card(bot: Bot, chat_id: int, user_tg_id: int,
                          note_id: int, lang: str):
    note = notes_service.get_note(note_id)
    if not note or note['status'] != 'active':
        await bot.send_message(chat_id, t("note_not_found", lang),
                               reply_markup=back_kb(lang, "m:notes"))
        return
    user = utils.get_user_by_tg(user_tg_id)
    if not user:
        await bot.send_message(chat_id, t("error_generic", lang))
        return
    has_access = notes_service.user_has_note_access(user['id'], note)
    has_hw = notes_service.get_homework(note_id) is not None
    pages = notes_service.get_pages(note_id)
    text = t("note_card", lang,
             title=utils.escape_html(note['title']),
             description=utils.escape_html(note['description'] or ""),
             subject=utils.escape_html(note['subject'] or "—"),
             category=utils.escape_html(note['category'] or "—"),
             pages_count=len(pages))

    if has_access:
        await bot.send_message(text=text, chat_id=chat_id,
                                reply_markup=note_card_kb(note_id, lang, has_hw, True))
        return

    # Нет доступа
    await bot.send_message(chat_id, text)
    if note['access_type'] == 'premium':
        await bot.send_message(chat_id, t("premium_note_card", lang),
                                reply_markup=paid_note_kb(note_id, lang,
                                                          config.MANAGER_USERNAME, True))
    else:
        await bot.send_message(chat_id,
                                t("paid_note_card", lang, price=note['price'],
                                  manager=config.MANAGER_USERNAME),
                                reply_markup=paid_note_kb(note_id, lang,
                                                          config.MANAGER_USERNAME, False))


@router.callback_query(F.data.startswith("note:") & ~F.data.startswith("note:pages_done"))
async def cb_note(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 2:
        await call.answer()
        return
    try:
        note_id = int(parts[1])
    except ValueError:
        await call.answer()
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await show_note_card(call.bot, call.message.chat.id, call.from_user.id,
                         note_id, lang)
    await call.answer()


@router.callback_query(F.data.startswith("noteread:"))
async def cb_note_read(call: CallbackQuery, user: dict):
    lang = user.get('language') or 'ru'
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer()
        return
    try:
        note_id = int(parts[1])
        page_idx = int(parts[2])
    except ValueError:
        await call.answer()
        return

    note = notes_service.get_note(note_id)
    if not note:
        await call.answer(t("note_not_found", lang), show_alert=True)
        return

    # Проверка доступа
    if not notes_service.user_has_note_access(user['id'], note):
        await call.answer(t("noaccess_note", lang), show_alert=True)
        return

    # Подписка на канал
    channel = subscription_service.get_required_channel_for_note(note_id)
    if channel:
        ok = await subscription_service.check_user_subscription(
            call.bot, channel, call.from_user.id)
        if not ok:
            await call.message.answer(
                t("must_subscribe", lang),
                reply_markup=subscription_kb(channel, lang, f"checksub:note:{note_id}")
            )
            await call.answer()
            return

    pages = notes_service.get_pages(note_id)
    if not pages:
        await call.answer(t("note_not_found", lang), show_alert=True)
        return

    if page_idx < 0 or page_idx >= len(pages):
        # Финал
        await call.message.answer(t("note_finished", lang),
                                  reply_markup=back_kb(lang, "m:notes"))
        await call.answer()
        return

    page = pages[page_idx]
    text = (f"{t('note_page', lang, n=page_idx+1, total=len(pages))}\n\n"
            f"{page['content']}")
    kb = note_page_kb(note_id, page_idx, len(pages), lang)
    try:
        if page.get('image_file_id'):
            await call.message.answer_photo(page['image_file_id'], caption=text[:1024],
                                              reply_markup=kb,
                                              protect_content=config.PROTECT_CONTENT)
        else:
            await call.message.answer(text, reply_markup=kb,
                                       protect_content=config.PROTECT_CONTENT)
    except Exception as e:
        log.warning("Note read error: %s", e)

    notes_service.update_progress(user['id'], note_id, page_idx + 1)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer()
