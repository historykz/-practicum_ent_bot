"""
Меню выбора режима, проверка доступа/оплаты, запуск Карточек/Заучивания,
платежи Stars за прохождения и повторы, восстановление незавершённых сессий.
"""
import json
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, LabeledPrice,
                            PreCheckoutQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from filters import IsAdmin
from services import modes_service as ms
from handlers import flashcards as fc
from handlers import learning as ln

router = Router(name="modes")
log = logging.getLogger(__name__)

MODE_NAMES = {'flashcards': '🃏 Карточки', 'learning': '🧠 Заучивание'}
MODE_SHORT = {'fc': 'flashcards', 'ln': 'learning'}
# Обратный маппинг: полное имя → короткий код для callback
MODE_CODE = {'flashcards': 'fc', 'learning': 'ln'}


def _pl(**kw):
    return json.dumps(kw, separators=(',', ':'))


# ===================== ВХОД В РЕЖИМ =====================

@router.callback_query(F.data.startswith("mode:fc:"))
async def cb_mode_fc(call: CallbackQuery, bot: Bot):
    await _enter_mode(call, bot, 'flashcards')


@router.callback_query(F.data.startswith("mode:ln:"))
async def cb_mode_ln(call: CallbackQuery, bot: Bot):
    await _enter_mode(call, bot, 'learning')


async def _enter_mode(call: CallbackQuery, bot: Bot, mode: str):
    test_id = int(call.data.split(":")[2])
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        await call.answer("Тест не найден.", show_alert=True)
        return
    # Только в личке
    if call.message.chat.type != "private":
        await call.answer("Этот режим доступен только в личке бота.",
                          show_alert=True)
        return
    # Режим включён?
    if not ms.is_mode_enabled(test_id, mode):
        await call.answer(f"{MODE_NAMES[mode]} отключён для этого теста.",
                          show_alert=True)
        return
    user = db.fetchone("SELECT * FROM users WHERE tg_id=?", (call.from_user.id,))
    uid = user['id'] if user else 0

    # Платный тест — сначала купить сам тест
    if test.get('is_paid') and not utils.has_paid_access(uid, test_id=test_id):
        await call.answer(
            "🔒 Сначала нужно купить сам тест, потом будут доступны режимы.",
            show_alert=True)
        return

    # Приватный — нужен доступ
    if test.get('is_private'):
        from handlers import private_access as _pa
        if not _pa.user_has_private_access(test_id, call.from_user.id) \
                and not utils.is_admin(call.from_user.id):
            await call.answer("❌ Нет доступа к приватному тесту.", show_alert=True)
            return

    await call.answer()

    # Незавершённая сессия?
    sess = db.fetchone(
        "SELECT * FROM mode_sessions WHERE user_tg_id=? AND test_id=? "
        "AND mode=? AND status='active' ORDER BY id DESC LIMIT 1",
        (call.from_user.id, test_id, mode))
    if sess:
        idx = sess['current_index']
        qids = json.loads(sess['question_ids'])
        kb = InlineKeyboardBuilder()
        kb.button(text="▶️ Продолжить", callback_data=f"mresume:{MODE_CODE[mode]}:{test_id}")
        kb.button(text="🔄 Начать заново", callback_data=f"mrestart:{MODE_CODE[mode]}:{test_id}")
        kb.button(text="❌ Завершить сессию", callback_data=f"mclose:{MODE_CODE[mode]}:{test_id}")
        kb.adjust(1)
        await bot.send_message(
            call.message.chat.id,
            f"📌 <b>У вас есть незавершённое прохождение</b>\n"
            f"{MODE_NAMES[mode]} · «{utils.escape_html(test['title'])}»\n\n"
            f"Вопрос: {idx+1} из {len(qids)}",
            reply_markup=kb.as_markup(), parse_mode="HTML")
        return

    # Бесплатный доступ (админ/премиум/free-тест)?
    if ms.free_access(test_id, call.from_user.id, uid):
        await _launch(bot, call.message.chat.id, call.from_user.id, test_id, mode)
        return

    # Платный режим — проверяем прохождения
    remaining = ms.remaining_passes(call.from_user.id, test_id, mode)
    if remaining > 0:
        await _show_start_or_buy(bot, call.message.chat.id, call.from_user.id,
                                  test_id, mode, remaining)
    else:
        await _show_buy(bot, call.message.chat.id, test_id, mode, 0)


async def _show_start_or_buy(bot, chat_id, user_tg_id, test_id, mode, remaining):
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начать", callback_data=f"mstart:{MODE_CODE[mode]}:{test_id}")
    kb.button(text="➕ Купить ещё", callback_data=f"mbuy:{MODE_CODE[mode]}:{test_id}")
    kb.button(text="🔙 Назад", callback_data=f"opentest:{test_id}")
    kb.adjust(1)
    await bot.send_message(
        chat_id,
        f"{MODE_NAMES[mode]}\n\nДоступно прохождений: <b>{remaining}</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML")


async def _show_buy(bot, chat_id, test_id, mode, remaining):
    p1 = ms.price_for(test_id, mode, '1')
    p10 = ms.price_for(test_id, mode, '10')
    predo = ms.price_for(test_id, mode, 'redo')
    kb = InlineKeyboardBuilder()
    kb.button(text=f"1 раз — {p1} ⭐️", callback_data=f"mpay:{MODE_CODE[mode]}:{test_id}:1")
    kb.button(text=f"10 раз — {p10} ⭐️", callback_data=f"mpay:{MODE_CODE[mode]}:{test_id}:10")
    kb.button(text="🔙 Назад", callback_data=f"opentest:{test_id}")
    kb.adjust(1)
    test = db.fetchone("SELECT title FROM tests WHERE id=?", (test_id,))
    # Подробное объяснение как пользоваться
    if mode == 'flashcards':
        how = (
            "🃏 <b>Как работают Карточки:</b>\n"
            "• Видишь вопрос → жмёшь «🔄 Повернуть» → видишь ответ\n"
            "• Отмечаешь «✅ Знаю» или «❌ Не знаю»\n"
            "• В конце можно повторить незнакомые\n"
            "• Удобно для быстрого запоминания (как Quizlet)")
    else:
        how = (
            "🧠 <b>Как работает Заучивание:</b>\n"
            "• Видишь вопрос → <b>пишешь ответ текстом</b>\n"
            "• Бот проверяет (регистр и мелкие опечатки не важны)\n"
            "• Не знаешь — жми «💡 Показать ответ» или «⏭ Пропустить»\n"
            "• В конце — статистика и повтор ошибок")
    await bot.send_message(
        chat_id,
        f"{MODE_NAMES[mode]}\n«{utils.escape_html(test['title'] if test else '')}»\n\n"
        f"{how}\n\n"
        f"💰 <b>Цены:</b>\n"
        f"• 1 прохождение — {p1} ⭐️\n"
        f"• 10 прохождений — {p10} ⭐️ (выгоднее!)\n"
        f"• Повтор ошибок — {predo} ⭐️\n\n"
        f"<i>Прохождение — это один полный проход теста в этом режиме. "
        f"Списывается когда начинаешь. Повтор ошибок не тратит прохождение.</i>\n\n"
        f"Осталось прохождений: {remaining}",
        reply_markup=kb.as_markup(), parse_mode="HTML")


# ===================== ЗАПУСК =====================

async def _launch(bot, chat_id, user_tg_id, test_id, mode, is_redo=False,
                   redo_ids=None):
    """Запустить режим (создать сессию). Списывает прохождение если платно."""
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    shuffle = bool(test.get('shuffle_questions')) if test else False

    if redo_ids is not None:
        qids = redo_ids
    else:
        qids = ms.get_question_ids(test_id, shuffle=shuffle)
    if not qids:
        await bot.send_message(chat_id, "⚠️ В тесте нет вопросов.")
        return

    # Списание прохождения (не для redo, не для free)
    uid_row = db.fetchone("SELECT id FROM users WHERE tg_id=?", (user_tg_id,))
    uid = uid_row['id'] if uid_row else 0
    if not is_redo and not ms.free_access(test_id, user_tg_id, uid):
        if not ms.use_one_pass(user_tg_id, test_id, mode):
            await bot.send_message(chat_id, "⚠️ Нет доступных прохождений.")
            return

    if mode == 'flashcards':
        await fc.start_flashcards(bot, chat_id, user_tg_id, test_id, qids,
                                   is_redo=is_redo)
    else:
        await ln.start_learning(bot, chat_id, user_tg_id, test_id, qids,
                                 is_redo=is_redo)


@router.callback_query(F.data.startswith("mstart:"))
async def cb_mstart(call: CallbackQuery, bot: Bot):
    _, sm, tid = call.data.split(":")
    mode = MODE_SHORT[sm]
    await call.answer()
    await _launch(bot, call.message.chat.id, call.from_user.id, int(tid), mode)


@router.callback_query(F.data.startswith("mresume:"))
async def cb_mresume(call: CallbackQuery, bot: Bot):
    _, sm, tid = call.data.split(":")
    mode = MODE_SHORT[sm]
    await call.answer()
    sess = db.fetchone(
        "SELECT * FROM mode_sessions WHERE user_tg_id=? AND test_id=? "
        "AND mode=? AND status='active' ORDER BY id DESC LIMIT 1",
        (call.from_user.id, int(tid), mode))
    if not sess:
        await bot.send_message(call.message.chat.id, "Сессия не найдена.")
        return
    # Продолжить — не списываем прохождение
    if mode == 'flashcards':
        await fc._render(bot, call.message.chat.id, sess, new=True)
    else:
        await ln._render_question(bot, call.message.chat.id, sess, new=True)


@router.callback_query(F.data.startswith("mrestart:"))
async def cb_mrestart(call: CallbackQuery, bot: Bot):
    _, sm, tid = call.data.split(":")
    mode = MODE_SHORT[sm]
    test_id = int(tid)
    # Закрыть старую (попытка считается использованной)
    db.execute(
        "UPDATE mode_sessions SET status='finished' WHERE user_tg_id=? "
        "AND test_id=? AND mode=? AND status='active'",
        (call.from_user.id, test_id, mode))
    await call.answer()
    # Новый запуск — нужно новое прохождение (если платно)
    uid_row = db.fetchone("SELECT id FROM users WHERE tg_id=?", (call.from_user.id,))
    uid = uid_row['id'] if uid_row else 0
    if ms.free_access(test_id, call.from_user.id, uid):
        await _launch(bot, call.message.chat.id, call.from_user.id, test_id, mode)
    else:
        rem = ms.remaining_passes(call.from_user.id, test_id, mode)
        if rem > 0:
            await _launch(bot, call.message.chat.id, call.from_user.id, test_id, mode)
        else:
            await _show_buy(bot, call.message.chat.id, test_id, mode, 0)


@router.callback_query(F.data.startswith("mclose:"))
async def cb_mclose(call: CallbackQuery, bot: Bot):
    _, sm, tid = call.data.split(":")
    mode = MODE_SHORT[sm]
    db.execute(
        "UPDATE mode_sessions SET status='finished' WHERE user_tg_id=? "
        "AND test_id=? AND mode=? AND status='active'",
        (call.from_user.id, int(tid), mode))
    await call.answer("Сессия завершена.")
    try:
        await call.message.edit_text("❌ Сессия завершена.")
    except Exception:
        pass


# ===================== ПОВТОР ОШИБОК (за 2⭐️) =====================

@router.callback_query(F.data.startswith("fcredo:"))
async def cb_fc_redo(call: CallbackQuery, bot: Bot):
    test_id = int(call.data.split(":")[1])
    await _redo_invoice(call, bot, test_id, 'flashcards')


@router.callback_query(F.data.startswith("lnredo:"))
async def cb_ln_redo(call: CallbackQuery, bot: Bot):
    test_id = int(call.data.split(":")[1])
    await _redo_invoice(call, bot, test_id, 'learning')


async def _redo_invoice(call, bot, test_id, mode):
    # Берём последний результат, список ошибочных id
    res = db.fetchone(
        "SELECT * FROM mode_results WHERE user_tg_id=? AND test_id=? AND mode=? "
        "ORDER BY id DESC LIMIT 1", (call.from_user.id, test_id, mode))
    if not res:
        await call.answer("Нет результата для повтора.", show_alert=True)
        return
    details = json.loads(res['details'] or '{}')
    err_ids = details.get('err_ids') or details.get('dont_ids') or []
    if not err_ids:
        await call.answer("Нет ошибок для повтора 🎉", show_alert=True)
        return
    uid_row = db.fetchone("SELECT id FROM users WHERE tg_id=?", (call.from_user.id,))
    uid = uid_row['id'] if uid_row else 0
    # Бесплатно для админа/премиум
    if ms.free_access(test_id, call.from_user.id, uid):
        await call.answer()
        await _launch(bot, call.message.chat.id, call.from_user.id, test_id,
                      mode, is_redo=True, redo_ids=err_ids)
        return
    price = ms.price_for(test_id, mode, 'redo')
    await call.answer()
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"Повтор ошибок ({MODE_NAMES[mode]})"[:32],
            description=f"Повторить {len(err_ids)} вопросов с ошибками",
            payload=_pl(k="moderedo", t=test_id, m=MODE_CODE[mode]),
            currency="XTR",
            prices=[LabeledPrice(label="Повтор ошибок", amount=price)])
    except Exception as e:
        await call.message.answer(f"⚠️ Не смог создать счёт: {e}")


# ===================== ПОКУПКА ПРОХОЖДЕНИЙ =====================

@router.callback_query(F.data.startswith("mbuy:"))
async def cb_mbuy(call: CallbackQuery, bot: Bot):
    _, sm, tid = call.data.split(":")
    mode = MODE_SHORT[sm]
    await call.answer()
    await _show_buy(bot, call.message.chat.id, int(tid), mode,
                    ms.remaining_passes(call.from_user.id, int(tid), mode))


@router.callback_query(F.data.startswith("mpay:"))
async def cb_mpay(call: CallbackQuery, bot: Bot):
    _, sm, tid, count = call.data.split(":")
    mode = MODE_SHORT[sm]
    test_id = int(tid)
    count = int(count)
    price = ms.price_for(test_id, mode, '1' if count == 1 else '10')
    await call.answer()
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"{MODE_NAMES[mode]} ×{count}"[:32],
            description=f"{count} прохождений режима",
            payload=_pl(k="modepass", t=test_id, m=sm, n=count),
            currency="XTR",
            prices=[LabeledPrice(label=f"{count} прохождений", amount=price)])
    except Exception as e:
        await call.message.answer(f"⚠️ Не смог создать счёт: {e}")


# Платежи режимов обрабатываются в payments.py (общий successful_payment),
# но т.к. там свой handler — обработаем тут отдельным pre_checkout не нужно
# (pre_checkout全局 в payments.py отвечает ok). Платёж придёт в payments.on_payment,
# который перенаправит к нам по kind.

async def handle_mode_payment(message: Message, bot: Bot, pl: dict,
                               charge: str, stars: int):
    """Вызывается из payments.on_payment для kind modepass/moderedo."""
    kind = pl.get('k')
    test_id = pl.get('t')
    mode = MODE_SHORT.get(pl.get('m'), 'flashcards')
    tg_id = message.from_user.id

    if kind == "modepass":
        count = pl.get('n', 1)
        ms.add_passes(tg_id, test_id, mode, count, charge)
        rem = ms.remaining_passes(tg_id, test_id, mode)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Начать",
                                  callback_data=f"mstart:{pl.get('m')}:{test_id}")]])
        await message.answer(
            f"✅ <b>Оплачено!</b>\n{MODE_NAMES[mode]} +{count} прохождений\n"
            f"Доступно: {rem}", reply_markup=kb, parse_mode="HTML")
    elif kind == "moderedo":
        # Запускаем повтор ошибок
        res = db.fetchone(
            "SELECT * FROM mode_results WHERE user_tg_id=? AND test_id=? "
            "AND mode=? ORDER BY id DESC LIMIT 1", (tg_id, test_id, mode))
        details = json.loads(res['details'] or '{}') if res else {}
        err_ids = details.get('err_ids') or details.get('dont_ids') or []
        await message.answer("✅ Оплачено! Повторяем ошибки 🔁")
        import asyncio
        await asyncio.sleep(0.6)
        await _launch(bot, message.chat.id, tg_id, test_id, mode,
                      is_redo=True, redo_ids=err_ids)
