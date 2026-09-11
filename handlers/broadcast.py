"""
Глобальная рассылка всем пользователям — экраны владельца бота.

Новое сообщение → кнопки → предпросмотр → подтверждение → отправка в фоне
с прогрессом → статистика → удаление у всех. Сама отправка, счёт и удаление —
в services/broadcast_service.py.
"""
import asyncio
import logging
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import utils
from filters import IsOwner
from services import broadcast_service as bs

router = Router(name="broadcast")
log = logging.getLogger(__name__)


class BcStates(StatesGroup):
    waiting_message = State()
    waiting_buttons = State()


BUTTONS_HELP = (
    "🔘 <b>Кнопки под сообщением</b>\n\n"
    "Пришлите кнопки текстом. Каждая строка — ряд, в одном ряду кнопки через "
    "<code>|</code>:\n\n"
    "<code>Открыть сайт - https://example.com\n"
    "Менеджер - @manager | Канал - t.me/channel\n"
    "Приложение - app</code>\n\n"
    "Ссылки: <code>https://…</code>, <code>t.me/…</code>, <code>@username</code>, "
    "<code>app</code> — наше мини-приложение, <code>сайт</code> — наш сайт.\n"
    f"До {bs.MAX_BUTTONS} кнопок, до {bs.MAX_PER_ROW} в ряду."
)


def _kb(*buttons) -> "InlineKeyboardMarkup":
    kb = InlineKeyboardBuilder()
    for text, data in buttons:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


async def _show(call: CallbackQuery, text: str, kb) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                     disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML",
                                  disable_web_page_preview=True)


def _bid(call: CallbackQuery) -> Optional[int]:
    try:
        return int(call.data.split(":")[2])
    except (IndexError, ValueError):
        return None


# ───────────────────────── главный экран ─────────────────────────

@router.callback_query(F.data.in_({"adm:broadcast", "bc:home"}), IsOwner())
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    aud = await asyncio.to_thread(bs.audience)
    act = await asyncio.to_thread(bs.active)
    lines = [
        "📣 <b>Рассылка всем пользователям</b>\n",
        f"👥 В базе: <b>{bs._n(aud['all'])}</b>",
        f"📨 Получат рассылку: <b>{bs._n(aud['targets'])}</b>",
    ]
    if aud["known_blocked"]:
        lines.append(f"⛔️ Из них раньше блокировали бота: {bs._n(aud['known_blocked'])} — "
                     f"им тоже отправим: вдруг разблокировали")
    if aud["banned"]:
        lines.append(f"🚷 Забанены админом: {bs._n(aud['banned'])} — им не отправляем")
    lines.append("\nЛюбое сообщение: текст, фото, видео, файл, голосовое, кружок, GIF, "
                 "опрос — с форматированием и кнопками. Можно переслать пост из канала. "
                 "После отправки — статистика и удаление у всех (в течение 48 часов).")
    buttons = [("✉️ Новая рассылка", "bc:new"), ("📜 История рассылок", "bc:list")]
    if act:
        lines.append(f"\n⏳ Сейчас идёт рассылка №{act['id']}.")
        buttons.insert(0, (f"📊 Рассылка №{act['id']} — прогресс", f"bc:st:{act['id']}"))
    buttons.append(("↩️ В админку", "m:admin"))
    await _show(call, "\n".join(lines), _kb(*buttons))
    await call.answer()


@router.callback_query(F.data == "bc:new", IsOwner())
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(BcStates.waiting_message)
    await _show(call,
                "✉️ <b>Новая рассылка</b>\n\nПришлите сообщение, которое получат все: "
                "текст, фото, видео, файл, голосовое, кружок, GIF или опрос. "
                "Форматирование, эмодзи и подпись сохранятся как есть. Можно переслать "
                "пост из своего канала — пометки «переслано» у людей не будет.\n\n"
                "Кнопки добавим на следующем шаге.",
                _kb(("❌ Отмена", "bc:home")))
    await call.answer()


@router.message(BcStates.waiting_message, IsOwner())
async def msg_source(message: Message, state: FSMContext):
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("Рассылка отменена.")
        return
    if getattr(message, "media_group_id", None):
        await message.answer(
            "Альбом разослать одним сообщением нельзя: Telegram копирует сообщения "
            "по одному. Пришлите одно фото или видео с подписью.")
        return
    kind = bs.message_kind(message)
    if not kind:
        await message.answer("Такое сообщение Telegram не даёт скопировать. Пришлите текст, "
                             "фото, видео, файл, голосовое, кружок, GIF или опрос.")
        return
    preview = (message.text or message.caption or "").strip()
    bid = await asyncio.to_thread(bs.create, message.from_user.id, message.chat.id,
                                  message.message_id, kind, preview)
    await state.clear()
    await message.answer(
        f"✅ Сообщение принято ({bs.KIND_TITLES.get(kind, kind)}).\n\n"
        f"Добавить под ним кнопки-ссылки — на сайт, аккаунт, канал, приложение?",
        reply_markup=_kb(("➕ Добавить кнопки", f"bc:btn:{bid}"),
                         ("👀 Без кнопок — к предпросмотру", f"bc:prev:{bid}"),
                         ("❌ Отмена", f"bc:cancel:{bid}")))


# ───────────────────────── кнопки ─────────────────────────

@router.callback_query(F.data.startswith("bc:btn:"), IsOwner())
async def cb_buttons(call: CallbackQuery, state: FSMContext):
    bid = _bid(call)
    b = await asyncio.to_thread(bs.get, bid) if bid else None
    if not b or b["status"] != bs.DRAFT:
        await call.answer("Черновик не найден или уже отправлен.", show_alert=True)
        return
    await state.set_state(BcStates.waiting_buttons)
    await state.update_data(bc_bid=bid)
    await _show(call, BUTTONS_HELP, _kb(("↩️ Назад к предпросмотру", f"bc:prev:{bid}")))
    await call.answer()


@router.message(BcStates.waiting_buttons, IsOwner())
async def msg_buttons(message: Message, state: FSMContext):
    bid = (await state.get_data()).get("bc_bid")
    if not bid or (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("Отменено.")
        return
    rows, errors = bs.parse_buttons(message.text or message.caption or "")
    if errors or not rows:
        err = "\n".join(f"• {utils.escape_html(e)}" for e in errors[:10]) or "• не нашёл ни одной кнопки"
        await message.answer(f"⚠️ Не получилось разобрать кнопки:\n{err}\n\n"
                             f"Пришлите кнопки ещё раз целиком.", parse_mode="HTML")
        return
    await asyncio.to_thread(bs.set_buttons, bid, rows)
    await state.clear()
    await _preview(message.bot, message.chat.id, bid)


@router.callback_query(F.data.startswith("bc:nobtn:"), IsOwner())
async def cb_no_buttons(call: CallbackQuery, state: FSMContext):
    bid = _bid(call)
    await state.clear()
    if bid:
        await asyncio.to_thread(bs.set_buttons, bid, [])
    await call.answer("Кнопки убраны")
    await _preview(call.bot, call.message.chat.id, bid)


# ───────────────────────── предпросмотр и отправка ─────────────────────────

async def _preview(bot, chat_id: int, bid: int) -> None:
    b = await asyncio.to_thread(bs.get, bid) if bid else None
    if not b or b["status"] != bs.DRAFT:
        await bot.send_message(chat_id, "Черновик не найден или уже отправлен.")
        return
    rows = bs.rows_of(b)
    await bot.send_message(chat_id, "👀 <b>Так увидят пользователи:</b>", parse_mode="HTML")
    try:
        await bot.copy_message(chat_id=chat_id, from_chat_id=b["from_chat_id"],
                               message_id=b["from_message_id"],
                               reply_markup=bs.markup_for(bid, rows, track=False))
    except Exception as e:
        await bot.send_message(
            chat_id, f"⚠️ Telegram не смог показать это сообщение: "
                     f"{utils.escape_html(str(e))[:200]}\n\nВозможно, исходное сообщение "
                     f"удалено или в кнопке неверная ссылка. Начните рассылку заново.",
            parse_mode="HTML", reply_markup=_kb(("✉️ Новая рассылка", "bc:new")))
        return
    aud = await asyncio.to_thread(bs.audience)
    buttons = [(f"✅ Отправить всем — {bs._n(aud['targets'])}", f"bc:send:{bid}"),
               ("✏️ Изменить кнопки" if rows else "➕ Добавить кнопки", f"bc:btn:{bid}")]
    if rows:
        buttons.append(("🧹 Убрать кнопки", f"bc:nobtn:{bid}"))
    buttons.append(("❌ Отмена", f"bc:cancel:{bid}"))
    await bot.send_message(
        chat_id,
        f"Получат: <b>{bs._n(aud['targets'])}</b> пользователей.\n\n"
        f"Проверьте текст и кнопки. После отправки изменить сообщение нельзя — "
        f"только удалить у всех, и только в течение 48 часов.",
        parse_mode="HTML", reply_markup=_kb(*buttons))


@router.callback_query(F.data.startswith("bc:prev:"), IsOwner())
async def cb_preview(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _preview(call.bot, call.message.chat.id, _bid(call))


@router.callback_query(F.data.startswith("bc:send:"), IsOwner())
async def cb_send(call: CallbackQuery):
    bid = _bid(call)
    b = await asyncio.to_thread(bs.get, bid) if bid else None
    if not b or b["status"] != bs.DRAFT:
        await call.answer("Эта рассылка уже отправлена или удалена.", show_alert=True)
        return
    aud = await asyncio.to_thread(bs.audience)
    await _show(call,
                f"⚠️ <b>Отправить сообщение {bs._n(aud['targets'])} пользователям?</b>\n\n"
                f"Остановить можно в любой момент. Удалить у всех — в течение 48 часов.",
                _kb((f"✅ Да, отправить — {bs._n(aud['targets'])}", f"bc:go:{bid}"),
                    ("↩️ Назад", f"bc:prev:{bid}")))
    await call.answer()


@router.callback_query(F.data.startswith("bc:go:"), IsOwner())
async def cb_go(call: CallbackQuery):
    bid = _bid(call)
    if not bid:
        await call.answer()
        return
    try:
        total = await asyncio.to_thread(bs.start, bid)
    except bs.BusyError as e:
        await call.answer(f"Уже идёт рассылка №{e.bid} — дождитесь окончания или "
                          f"остановите её.", show_alert=True)
        return
    if not total:
        # 0 — либо рассылку уже запускали (повторное нажатие), либо в базе
        # никого нет. Различаем по зафиксированным получателям, а не по
        # статусу: у завершённой рассылки статус тот же, что у пустой.
        b = await asyncio.to_thread(bs.get, bid)
        already = bool(b and (b.get("total") or 0) > 0)
        await call.answer("Эта рассылка уже запущена — повторно не отправляю." if already
                          else "Некому отправлять: в базе нет пользователей.", show_alert=True)
        return
    await call.answer("Запускаю рассылку…")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    text = await asyncio.to_thread(bs.progress_text, bid)
    kb = await asyncio.to_thread(bs.progress_kb, bid)
    msg = await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await asyncio.to_thread(bs.set_progress_message, bid, msg.chat.id, msg.message_id)
    bs.launch(call.bot, bid, "send")


@router.callback_query(F.data.startswith("bc:stop:"), IsOwner())
async def cb_stop(call: CallbackQuery):
    bid = _bid(call)
    ok = bid and await asyncio.to_thread(bs.request_stop, bid)
    await call.answer("⏹ Останавливаю — уже отправленное останется у людей."
                      if ok else "Рассылка уже не идёт.", show_alert=not ok)


@router.callback_query(F.data.startswith("bc:resume:"), IsOwner())
async def cb_resume(call: CallbackQuery):
    bid = _bid(call)
    b = await asyncio.to_thread(bs.get, bid) if bid else None
    if not b or b["status"] not in (bs.SENDING, bs.DELETING):
        await call.answer("Продолжать нечего.", show_alert=True)
        return
    started = bs.launch(call.bot, bid, "send" if b["status"] == bs.SENDING else "delete")
    await call.answer("▶️ Продолжаю с того же места" if started else "Уже идёт.")


# ───────────────────────── статистика, история ─────────────────────────

@router.callback_query(F.data.startswith("bc:st:"), IsOwner())
async def cb_stats(call: CallbackQuery):
    bid = _bid(call)
    s = await asyncio.to_thread(bs.stats, bid) if bid else {}
    if not s:
        await call.answer("Рассылка не найдена.", show_alert=True)
        return
    status = s["b"]["status"]
    buttons = []
    if status in (bs.SENDING, bs.DELETING):
        if bs.is_running(bid):
            if status == bs.SENDING:
                buttons.append(("⏹ Остановить", f"bc:stop:{bid}"))
        else:
            buttons.append(("▶️ Продолжить", f"bc:resume:{bid}"))
    if s["can_delete"]:
        buttons.append(("🗑 Удалить у всех", f"bc:del:{bid}"))
    buttons += [("🔄 Обновить", f"bc:st:{bid}"), ("📜 История рассылок", "bc:list"),
                ("↩️ Рассылка", "bc:home")]
    await _show(call, await asyncio.to_thread(bs.stats_text, bid), _kb(*buttons))
    await call.answer()


@router.callback_query(F.data == "bc:list", IsOwner())
async def cb_list(call: CallbackQuery):
    items = await asyncio.to_thread(bs.recent, 15)
    if not items:
        await _show(call, "📜 Рассылок пока не было.", _kb(("↩️ Рассылка", "bc:home")))
        await call.answer()
        return
    lines = ["📜 <b>История рассылок</b>\n"]
    buttons = []
    for r in items:
        when = bs._local(r.get("started_at") or r.get("created_at"))
        lines.append(f"<b>№{r['id']}</b> · {when} · {bs.BC_TITLES.get(r['status'], r['status'])}\n"
                     f"   доставлено {bs._n(r['delivered'])} из {bs._n(r['total'])}"
                     + (f" · «{utils.escape_html(r['preview'][:40])}…»" if r.get("preview") else ""))
        buttons.append((f"📊 №{r['id']} · {when[:5]}", f"bc:st:{r['id']}"))
    buttons.append(("↩️ Рассылка", "bc:home"))
    await _show(call, "\n".join(lines), _kb(*buttons))
    await call.answer()


@router.callback_query(F.data.startswith("bc:cancel:"), IsOwner())
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    bid = _bid(call)
    if bid:
        await asyncio.to_thread(bs.delete_draft, bid)
    await call.answer("Рассылка отменена")
    await cb_home(call, state)


# ───────────────────────── удаление у всех ─────────────────────────

@router.callback_query(F.data.startswith("bc:del:"), IsOwner())
async def cb_delete_ask(call: CallbackQuery):
    bid = _bid(call)
    s = await asyncio.to_thread(bs.stats, bid) if bid else {}
    if not s or not s["can_delete"]:
        await call.answer("Удалять нечего или прошло больше 48 часов.", show_alert=True)
        return
    await _show(call,
                f"🗑 <b>Удалить рассылку №{bid} у всех получателей?</b>\n\n"
                f"Сообщение исчезнет у {bs._n(s['delivered'] - s['deleted'])} человек. "
                + ("Отправка ещё идёт — сначала остановлю её. " if s["b"]["status"] == bs.SENDING else "")
                + "Отменить удаление будет нельзя.",
                _kb(("🗑 Да, удалить у всех", f"bc:delgo:{bid}"), ("↩️ Нет", f"bc:st:{bid}")))
    await call.answer()


@router.callback_query(F.data.startswith("bc:delgo:"), IsOwner())
async def cb_delete_go(call: CallbackQuery):
    bid = _bid(call)
    await call.answer("Удаляю…")
    res = await bs.prepare_delete(bid) if bid else "not_found"
    if res != "ok":
        text = {"busy": "Сейчас идёт другая рассылка — дождитесь её окончания.",
                "bad_status": "Эта рассылка уже удалена.",
                "not_found": "Рассылка не найдена."}.get(res, "Не получилось.")
        await call.message.answer(text)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    text = await asyncio.to_thread(bs.progress_text, bid)
    kb = await asyncio.to_thread(bs.progress_kb, bid)
    msg = await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await asyncio.to_thread(bs.set_progress_message, bid, msg.chat.id, msg.message_id)
    bs.launch(call.bot, bid, "delete")
