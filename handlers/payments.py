"""
Платежи Telegram Stars.
- Покупка теста / раздела (−20% при 5+ платных) / подарка / повтора ошибок
- «Сделать платным» в Мои тесты (цена ₸ + ⭐️)
- Мои покупки, /refund (только админ)
"""
import json
import logging
import secrets

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (Message, CallbackQuery, LabeledPrice,
                            PreCheckoutQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from filters import IsAdmin
from services import payment_service as ps

router = Router(name="payments")
log = logging.getLogger(__name__)


class PriceStates(StatesGroup):
    waiting_tenge = State()
    waiting_stars = State()


class GiftStates(StatesGroup):
    waiting_username = State()


def _payload(**kw) -> str:
    return json.dumps(kw, separators=(',', ':'))


# ===================== ПОКУПКА ТЕСТА =====================

@router.callback_query(F.data.startswith("buy:test:"))
async def cb_buy_test(call: CallbackQuery, bot: Bot):
    test_id = int(call.data.split(":")[2])
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test or not (test.get('price_stars') or 0):
        await call.answer("Тест недоступен для покупки.", show_alert=True)
        return
    if ps.has_paid_access(test_id, call.from_user.id):
        await call.answer("✅ Уже куплено! Открывай тест.", show_alert=True)
        return
    await call.answer()
    stars = test['price_stars']
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=test['title'][:32],
            description=f"Доступ к тесту навсегда · {stars} ⭐️",
            payload=_payload(k="test", t=test_id),
            currency="XTR",
            prices=[LabeledPrice(label=test['title'][:32], amount=stars)],
        )
    except Exception as e:
        log.exception("invoice test: %s", e)
        await call.message.answer(f"⚠️ Не смог создать счёт: {e}")


# ===================== ПОКУПКА РАЗДЕЛА =====================

@router.callback_query(F.data.startswith("buy:cat:"))
async def cb_buy_category(call: CallbackQuery, bot: Bot):
    cat_id = int(call.data.split(":")[2])
    # Закрытый раздел не продаём через кнопку: доступ к нему админ выдаёт
    # сам, а название и состав не должны раскрываться посторонним.
    from services import category_service as _cs
    if not _cs.has_access(cat_id, call.from_user.id):
        await call.answer("Предложение недоступно.", show_alert=True)
        return
    offer = ps.get_section_offer(cat_id, call.from_user.id)
    if not offer:
        await call.answer("Предложение недоступно.", show_alert=True)
        return
    await call.answer()
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"Раздел: {offer['name']}"[:32],
            description=(f"{offer['tests_count']} платных тестов со скидкой 20% "
                          f"(вместо {offer['full_price']} ⭐️)"),
            payload=_payload(k="cat", c=cat_id),
            currency="XTR",
            prices=[LabeledPrice(label=f"Раздел {offer['name']}"[:32],
                                  amount=offer['price'])],
        )
    except Exception as e:
        log.exception("invoice cat: %s", e)
        await call.message.answer(f"⚠️ Не смог создать счёт: {e}")


# ===================== ПОДАРОК =====================

@router.callback_query(F.data.startswith("buy:gift:"))
async def cb_buy_gift(call: CallbackQuery, state: FSMContext):
    test_id = int(call.data.split(":")[2])
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test or not (test.get('price_stars') or 0):
        await call.answer("Тест недоступен.", show_alert=True)
        return
    await call.answer()
    # Сразу просим @username друга (без выбора способа)
    await state.set_state(GiftStates.waiting_username)
    await state.update_data(gift_test_id=test_id)
    await call.message.answer(
        f"🎁 <b>Подарить тест «{utils.escape_html(test['title'])}»</b>\n"
        f"Цена: {test['price_stars']} ⭐️\n\n"
        "👤 Введи <b>@username</b> друга, которому хочешь подарить.\n"
        "<i>Друг должен был хотя бы раз запускать этого бота.</i>\n\n"
        "/cancel — отмена",
        parse_mode="HTML")


@router.callback_query(F.data == "gift:cancel")
async def cb_gift_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Отменено.")
    except Exception:
        pass
    await call.answer()


@router.message(GiftStates.waiting_username)
async def msg_gift_username(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    uname = (message.text or '').strip().lstrip('@')
    if not uname:
        await message.answer("Введи @username или /cancel")
        return
    friend = db.fetchone(
        "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (uname,))
    if not friend:
        await message.answer(
            f"⚠️ Пользователь @{uname} не найден в боте.\n"
            "Он должен хотя бы раз запустить бота. Или подари по ссылке.")
        return
    data = await state.get_data()
    test_id = data.get('gift_test_id')
    await state.clear()
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        await message.answer("Тест не найден.")
        return
    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=f"🎁 Подарок @{uname}"[:32],
            description=f"Тест «{test['title']}» для @{uname}",
            payload=_payload(k="giftuser", t=test_id, to=friend['tg_id']),
            currency="XTR",
            prices=[LabeledPrice(label="Подарок другу",
                                  amount=test['price_stars'])],
        )
    except Exception as e:
        await message.answer(f"⚠️ Не смог создать счёт: {e}")


# ===================== ПОВТОР ОШИБОК ЗА ЗВЁЗДЫ =====================

@router.callback_query(F.data.startswith("buyredo:"))
async def cb_buy_redo(call: CallbackQuery, bot: Bot):
    """Платный повтор ошибок (после 1 бесплатного)."""
    try:
        attempt_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    a = db.fetchone("SELECT * FROM test_attempts WHERE id=?", (attempt_id,))
    if not a:
        await call.answer("Попытка не найдена.", show_alert=True)
        return
    await call.answer()
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title="🔁 Повтор ошибок",
            description="Повторить вопросы где ошибся",
            payload=_payload(k="redo", a=attempt_id, t=a['test_id']),
            currency="XTR",
            prices=[LabeledPrice(label="Повтор ошибок",
                                  amount=ps.REDO_PRICE_STARS)],
        )
    except Exception as e:
        await call.message.answer(f"⚠️ Не смог создать счёт: {e}")


# ===================== CHECKOUT =====================

@router.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)


@router.message(F.successful_payment)
async def on_payment(message: Message, bot: Bot):
    sp = message.successful_payment
    charge = sp.telegram_payment_charge_id
    stars = sp.total_amount
    tg_id = message.from_user.id
    try:
        pl = json.loads(sp.invoice_payload)
    except Exception:
        pl = {}
    kind = pl.get('k')

    if kind == "test":
        test_id = pl.get('t')
        ps.grant_purchase(tg_id, 'test', stars, charge, test_id=test_id)
        test = db.fetchone("SELECT title FROM tests WHERE id=?", (test_id,))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Открыть тест",
                                  callback_data=f"opentest:{test_id}")]])
        await message.answer(
            f"✅ <b>Куплено!</b>\n\n"
            f"💎 «{utils.escape_html(test['title'] if test else '')}»\n"
            f"Доступ открыт навсегда 🎉",
            reply_markup=kb, parse_mode="HTML")

    elif kind == "cat":
        cat_id = pl.get('c')
        ps.grant_purchase(tg_id, 'category', stars, charge, category_id=cat_id)
        cat = db.fetchone("SELECT name FROM test_categories WHERE id=?", (cat_id,))
        await message.answer(
            f"✅ <b>Куплен весь раздел!</b>\n\n"
            f"📂 «{utils.escape_html(cat['name'] if cat else '')}»\n"
            f"Все платные тесты раздела открыты навсегда 🎉\n\n"
            f"Открой «📚 Тесты» и проходи!",
            parse_mode="HTML")

    elif kind == "unit_lesson":
        lesson_id = pl.get('l')
        from services import unit_sales as us
        if not us.sale_enabled_for_lesson(lesson_id):
            # оплата уже прошла — доступ всё равно выдаём, но помечаем в логе
            log.warning("Оплачен урок %s при выключенной продаже", lesson_id)
        us.grant_lesson(tg_id, lesson_id, charge)
        ps.grant_purchase(tg_id, 'unit_lesson', stars, charge)
        les = db.fetchone("SELECT title FROM lessons WHERE id=?", (lesson_id,))
        await message.answer(
            f"✅ <b>Урок куплен!</b>\n\n"
            f"📖 «{utils.escape_html(les['title'] if les else '')}»\n"
            f"Конспект, тест и материалы урока открыты 🎉",
            parse_mode="HTML")

    elif kind == "unit_section":
        section_id = pl.get('s')
        from services import unit_sales as us
        if not us.sale_enabled_for_section(section_id):
            log.warning("Оплачен раздел %s при выключенной продаже", section_id)
        us.grant_section(tg_id, section_id, charge)
        ps.grant_purchase(tg_id, 'unit_section', stars, charge)
        sec = db.fetchone("SELECT title FROM sections WHERE id=?", (section_id,))
        await message.answer(
            f"✅ <b>Раздел куплен целиком!</b>\n\n"
            f"📚 «{utils.escape_html(sec['title'] if sec else '')}»\n"
            f"Все уроки и конспекты раздела открыты 🎉",
            parse_mode="HTML")

    elif kind == "giftuser":
        test_id = pl.get('t')
        to_tg = pl.get('to')
        ps.grant_purchase(tg_id, 'gift', stars, charge,
                           test_id=test_id, gifted_to=to_tg)
        test = db.fetchone("SELECT title FROM tests WHERE id=?", (test_id,))
        title = utils.escape_html(test['title'] if test else '')
        await message.answer(
            f"✅ <b>Подарок отправлен!</b>\n\n"
            f"🎁 «{title}» подарен другу. Ему пришло уведомление.",
            parse_mode="HTML")
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 Открыть тест",
                                      callback_data=f"opentest:{test_id}")]])
            gifter = message.from_user.username
            who = f"@{gifter}" if gifter else "Друг"
            await bot.send_message(
                to_tg,
                f"🎁 <b>{who} подарил тебе тест!</b>\n\n"
                f"💎 «{title}»\n"
                f"Доступ открыт навсегда 🎉",
                reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            log.warning("gift notify: %s", e)

    elif kind == "giftlink":
        test_id = pl.get('t')
        code = secrets.token_hex(5)
        pid = ps.grant_purchase(tg_id, 'gift', stars, charge, test_id=test_id)
        db.execute("UPDATE purchases SET gift_code=? WHERE id=?", (code, pid))
        test = db.fetchone("SELECT title FROM tests WHERE id=?", (test_id,))
        title = utils.escape_html(test['title'] if test else '')
        bot_un = getattr(config, 'BOT_USERNAME', '') or ''
        if not bot_un:
            try:
                bot_un = (await bot.get_me()).username
            except Exception:
                bot_un = ''
        link = f"https://t.me/{bot_un}?start=gift_{code}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Принять подарок", url=link)]])
        await message.answer(
            "✅ <b>Оплачено!</b> Перешли это сообщение другу 👇",
            parse_mode="HTML")
        await message.answer(
            f"🎁 <b>Тебе хотят подарить тест «{title}»!</b>\n"
            f"Нажми чтобы получить доступ навсегда 👇",
            reply_markup=kb, parse_mode="HTML")

    elif kind == "redo":
        attempt_id = pl.get('a')
        test_id = pl.get('t')
        ps.grant_purchase(tg_id, 'redo', stars, charge, test_id=test_id)
        from services import test_runner
        new_attempt = test_runner.create_redo_attempt(attempt_id)
        if not new_attempt:
            await message.answer("🎉 Нет ошибок для повтора! Звёзды вернём.")
            try:
                await bot.refund_star_payment(
                    user_id=tg_id, telegram_payment_charge_id=charge)
            except Exception:
                pass
            return
        await message.answer(
            "✅ Оплачено! 🔁 <b>Повтор ошибок</b> — поехали!",
            parse_mode="HTML")
        import asyncio
        await asyncio.sleep(1)
        await test_runner.send_current_question(
            bot, new_attempt, message.chat.id)
    elif kind in ("modepass", "moderedo"):
        # Платежи режимов Карточки/Заучивание
        from handlers import modes as _modes
        await _modes.handle_mode_payment(message, bot, pl, charge, stars)
    elif kind == "premium":
        # Подписка Премиум
        from handlers import premium as _prem
        await _prem.handle_premium_payment(message, bot, pl, charge, stars)
    else:
        log.warning("unknown payment payload: %s", sp.invoice_payload)
        await message.answer("✅ Оплата получена.")


# ===================== ПРИНЯТЬ ПОДАРОК (deep-link) =====================

async def claim_gift(message: Message, code: str):
    """Вызывается из common.py при /start gift_<code>."""
    p = db.fetchone(
        "SELECT * FROM purchases WHERE gift_code=? AND kind='gift'", (code,))
    if not p:
        await message.answer("⚠️ Подарок не найден или уже принят.")
        return
    if p.get('gifted_to_tg_id'):
        if p['gifted_to_tg_id'] == message.from_user.id:
            await message.answer("✅ Этот подарок уже у тебя! Открывай тест.")
        else:
            await message.answer("⚠️ Этот подарок уже принял другой человек.")
        return
    if p['user_tg_id'] == message.from_user.id:
        await message.answer(
            "🙂 Это твой подарок для друга — перешли сообщение ему.")
        return
    db.execute("UPDATE purchases SET gifted_to_tg_id=? WHERE id=?",
                (message.from_user.id, p['id']))
    test = db.fetchone("SELECT title FROM tests WHERE id=?", (p['test_id'],))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Открыть тест",
                              callback_data=f"opentest:{p['test_id']}")]])
    await message.answer(
        f"🎁 <b>Подарок принят!</b>\n\n"
        f"💎 «{utils.escape_html(test['title'] if test else '')}»\n"
        f"Доступ открыт навсегда 🎉",
        reply_markup=kb, parse_mode="HTML")


# ===================== МОИ ПОКУПКИ =====================

@router.callback_query(F.data == "profile:purchases")
async def cb_my_purchases(call: CallbackQuery):
    rows = ps.user_purchases(call.from_user.id)
    if not rows:
        await call.answer("Покупок пока нет 🙂", show_alert=True)
        return
    lines = ["🛒 <b>Мои покупки:</b>\n"]
    for p in rows[:30]:
        if p['kind'] == 'test':
            lines.append(f"• 💎 {p.get('test_title') or '—'} — {p['stars_amount']} ⭐️")
        elif p['kind'] == 'category':
            lines.append(f"• 📂 Весь раздел «{p.get('cat_name') or '—'}» — {p['stars_amount']} ⭐️")
        elif p['kind'] == 'gift':
            if p['user_tg_id'] == call.from_user.id:
                lines.append(f"• 🎁 Подарено: {p.get('test_title') or '—'} — {p['stars_amount']} ⭐️")
            else:
                lines.append(f"• 🎁 Подарок тебе: {p.get('test_title') or '—'}")
        elif p['kind'] == 'redo':
            lines.append(f"• 🔁 Повтор ошибок — {p['stars_amount']} ⭐️")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Назад", callback_data="m:profile")]])
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=kb,
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer("\n".join(lines), reply_markup=kb,
                                    parse_mode="HTML")
    await call.answer()


# ===================== ВОЗВРАТ (только админ) =====================

@router.message(Command("refunds"), IsAdmin())
async def cmd_refunds_list(message: Message):
    """Список последних покупок с кнопками возврата (удобно, без кода)."""
    rows = db.fetchall(
        """SELECT p.*, t.title AS test_title, u.username AS buyer_name
           FROM purchases p
           LEFT JOIN tests t ON t.id = p.test_id
           LEFT JOIN users u ON u.tg_id = p.user_tg_id
           WHERE p.charge_id IS NOT NULL AND p.charge_id != ''
           ORDER BY p.id DESC LIMIT 20""")
    if not rows:
        await message.answer("Покупок для возврата пока нет.")
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    lines = ["↩️ <b>Возврат звёзд</b>\nВыбери покупку для возврата:\n"]
    for p in rows:
        who = f"@{p['buyer_name']}" if p.get('buyer_name') else f"id{p['user_tg_id']}"
        kind_emoji = {'test':'💎','category':'📂','gift':'🎁','redo':'🔁'}.get(p['kind'],'•')
        what = p.get('test_title') or p['kind']
        label = f"{kind_emoji} {who} · {what[:20]} · {p['stars_amount']}⭐️"
        kb.button(text=label[:60], callback_data=f"dorefund:{p['id']}")
    kb.button(text="❌ Закрыть", callback_data="refund:close")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(),
                          parse_mode="HTML")


@router.callback_query(F.data == "refund:close", IsAdmin())
async def cb_refund_close(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("dorefund:"), IsAdmin())
async def cb_do_refund(call: CallbackQuery, bot: Bot):
    """Возврат по кнопке (с подтверждением)."""
    pid = int(call.data.split(":")[1])
    p = db.fetchone("SELECT * FROM purchases WHERE id=?", (pid,))
    if not p:
        await call.answer("Покупка не найдена.", show_alert=True)
        return
    # Подтверждение
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Да, вернуть {p['stars_amount']} ⭐️",
              callback_data=f"refundyes:{pid}")
    kb.button(text="❌ Отмена", callback_data="refund:close")
    kb.adjust(1)
    await call.message.answer(
        f"↩️ Вернуть <b>{p['stars_amount']} ⭐️</b> пользователю?\n"
        f"Доступ к тесту будет отозван.",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("refundyes:"), IsAdmin())
async def cb_refund_confirm(call: CallbackQuery, bot: Bot):
    pid = int(call.data.split(":")[1])
    p = db.fetchone("SELECT * FROM purchases WHERE id=?", (pid,))
    if not p:
        await call.answer("Уже возвращено или не найдено.", show_alert=True)
        return
    try:
        await bot.refund_star_payment(
            user_id=p['user_tg_id'],
            telegram_payment_charge_id=p['charge_id'])
    except Exception as e:
        await call.answer(f"Telegram отклонил: {e}", show_alert=True)
        return
    db.execute("DELETE FROM purchases WHERE id=?", (p['id'],))
    try:
        await call.message.edit_text(
            f"✅ Возврат {p['stars_amount']} ⭐️ выполнен, доступ отозван.")
    except Exception:
        pass
    await call.answer("✅ Возвращено", show_alert=True)


@router.message(Command("refund"), IsAdmin())
async def cmd_refund(message: Message, bot: Bot):
    parts = (message.text or '').split()
    if len(parts) < 2:
        await message.answer(
            "↩️ <b>Возврат звёзд</b>\n\n"
            "Проще всего: напиши <code>/refunds</code> — покажу список "
            "покупок с кнопками возврата (без ввода кода).\n\n"
            "Или вручную: <code>/refund код_платежа</code>",
            parse_mode="HTML")
        return
    charge = parts[1].strip()
    p = ps.find_purchase_by_charge(charge)
    if not p:
        await message.answer("⚠️ Покупка с таким charge_id не найдена.")
        return
    try:
        await bot.refund_star_payment(
            user_id=p['user_tg_id'],
            telegram_payment_charge_id=charge)
    except Exception as e:
        await message.answer(f"⚠️ Telegram отклонил возврат: {e}")
        return
    db.execute("DELETE FROM purchases WHERE id=?", (p['id'],))
    await message.answer(
        f"✅ Возврат {p['stars_amount']} ⭐️ выполнен, доступ отозван.")


# ===================== СДЕЛАТЬ ПЛАТНЫМ (адм) =====================

@router.callback_query(F.data.startswith("admpaid:"), IsAdmin())
async def cb_make_paid(call: CallbackQuery, state: FSMContext):
    test_id = int(call.data.split(":")[1])
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        await call.answer()
        return
    if test.get('is_paid'):
        stars = test.get('price_stars') or 0
        tenge = test.get('price') or 0
        kb = InlineKeyboardBuilder()
        warn = ""
        if stars == 0:
            warn = ("\n\n⚠️ <b>Звёзды не заданы!</b> Поэтому у учеников НЕТ "
                    "кнопки покупки за звёзды. Задай цену в звёздах ниже 👇")
            if tenge > 0:
                kb.button(
                    text=f"⚡️ Авто: {max(1, tenge // 2)} ⭐️ (тенге ÷ 2)",
                    callback_data=f"admstarauto:{test_id}")
        kb.button(text="✏️ Изменить цены (тенге + звёзды)",
                  callback_data=f"admprice:{test_id}")
        kb.button(text="🆓 Сделать бесплатным", callback_data=f"admfree:{test_id}")
        kb.button(text="↩️ Назад", callback_data=f"admtest:{test_id}")
        kb.adjust(1)
        await call.message.answer(
            f"💎 <b>Тест платный</b>\n"
            f"💵 {tenge} ₸ · ⭐️ {stars} звёзд{warn}",
            reply_markup=kb.as_markup(), parse_mode="HTML")
        await call.answer()
        return
    await state.set_state(PriceStates.waiting_tenge)
    await state.update_data(price_test_id=test_id)
    await call.message.answer(
        "💰 Введи цену в <b>ТЕНГЕ</b> (число):\n\n/cancel — отмена",
        parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admstarauto:"), IsAdmin())
async def cb_star_auto(call: CallbackQuery):
    """Быстро задать звёзды = тенге ÷ 2."""
    test_id = int(call.data.split(":")[1])
    test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
    if not test:
        await call.answer()
        return
    tenge = test.get('price') or 0
    stars = max(1, tenge // 2)
    db.execute("UPDATE tests SET is_paid=1, price_stars=? WHERE id=?",
                (stars, test_id))
    await call.answer(
        f"✅ Цена в звёздах: {stars} ⭐️\nТеперь у учеников есть кнопка покупки!",
        show_alert=True)


@router.callback_query(F.data.startswith("admprice:"), IsAdmin())
async def cb_change_price(call: CallbackQuery, state: FSMContext):
    test_id = int(call.data.split(":")[1])
    await state.set_state(PriceStates.waiting_tenge)
    await state.update_data(price_test_id=test_id)
    await call.message.answer(
        "💰 Введи новую цену в <b>ТЕНГЕ</b>:\n\n/cancel — отмена",
        parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admfree:"), IsAdmin())
async def cb_make_free(call: CallbackQuery):
    test_id = int(call.data.split(":")[1])
    db.execute("UPDATE tests SET is_paid=0, price=0, price_stars=0 WHERE id=?",
                (test_id,))
    await call.answer("🆓 Тест теперь бесплатный", show_alert=True)


@router.message(PriceStates.waiting_tenge, IsAdmin())
async def msg_price_tenge(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    try:
        tenge = int((message.text or '').strip())
        assert tenge >= 0
    except Exception:
        await message.answer("Введи число (тенге), например 250")
        return
    await state.update_data(price_tenge=tenge)
    await state.set_state(PriceStates.waiting_stars)
    await message.answer(
        f"💵 Цена: {tenge} ₸\n\n"
        "⭐️ Теперь введи цену в <b>ЗВЁЗДАХ</b> (число):\n"
        "<i>Подсказка: 1 ⭐️ ≈ 2 ₸</i>",
        parse_mode="HTML")


@router.message(PriceStates.waiting_stars, IsAdmin())
async def msg_price_stars(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    try:
        stars = int((message.text or '').strip())
        assert stars >= 0
    except Exception:
        await message.answer("Введи число (звёзды), например 25")
        return
    data = await state.get_data()
    test_id = data.get('price_test_id')
    tenge = data.get('price_tenge', 0)
    await state.clear()
    # Если обе цены 0 — тест становится бесплатным
    if tenge == 0 and stars == 0:
        db.execute(
            "UPDATE tests SET is_paid=0, price=0, price_stars=0 WHERE id=?",
            (test_id,))
        await message.answer(
            "🆓 Обе цены = 0, поэтому тест стал <b>бесплатным</b>.",
            parse_mode="HTML")
        return
    db.execute(
        "UPDATE tests SET is_paid=1, price=?, price_stars=? WHERE id=?",
        (tenge, stars, test_id))
    note = ""
    if stars == 0:
        note = ("\n\n⚠️ Звёзды = 0 — у учеников НЕ будет кнопки покупки "
                "за звёзды, только оплата через менеджера. "
                "Задай звёзды если хочешь продавать в боте.")
    await message.answer(
        f"✅ <b>Тест теперь платный!</b>\n\n"
        f"💵 {tenge} ₸  ·  ⭐️ {stars} звёзд{note}",
        parse_mode="HTML")
