"""
Админ-панель кампаний: сообщение кнопки «КОНСПЕКТЫ ЕНТ» и автонапоминания.

Две независимые настройки в одном экране-родителе. Правка одной не трогает
другую: у них разные кампании, разные тексты и разные кнопки.
"""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
import utils
from filters import IsAdmin, IsOwner
from handlers.notes_promo import build_message
from services import reminder_service as rs

router = Router(name="reminders_admin")
log = logging.getLogger(__name__)

TITLES = {rs.MANUAL: "📚 Сообщение раздела «КОНСПЕКТЫ ЕНТ»",
          rs.REMINDER: "📚 Автонапоминание о конспектах"}

COOLDOWNS = ((86400, "1 день"), (172800, "2 дня"), (259200, "3 дня"),
             (432000, "5 дней"), (604800, "7 дней"))
DELAYS = ((300, "5 мин"), (600, "10 мин"), (900, "15 мин"), (1800, "30 мин"))


class RemStates(StatesGroup):
    waiting_text = State()
    waiting_button = State()
    waiting_url = State()
    waiting_cooldown = State()


def _btn(t, d):
    return InlineKeyboardButton(text=t, callback_data=d)


def _short(s: str, n: int = 90) -> str:
    s = (s or "").replace("\n", " ")
    return utils.escape_html(s[:n] + ("…" if len(s) > n else ""))


# ===================== общий экран кампании =====================

def _screen(key: str):
    c = rs.get_campaign(key)
    is_rem = key == rs.REMINDER
    lines = [f"<b>{TITLES[key]}</b>", ""]
    if is_rem:
        lines.append("Статус: " + ("🟢 Включено" if c["enabled"] else "🔴 Выключено"))
        lines.append(f"Версия кампании: <code>{rs.campaign_label(key)}</code>")
        lines.append(f"Частота: <b>{rs.human_cooldown(c['cooldown_seconds'])}</b>")
        lines.append(f"Пауза после активности: <b>"
                     f"{rs.human_cooldown(c['safe_delay_seconds'])}</b>")
    else:
        lines.append("Работает всегда и для всех — Премиум не проверяется.")
    lines += ["", f"✏️ Текст: <i>{_short(c['message_text'])}</i>",
              f"🔘 Кнопка: <b>{utils.escape_html(c['button_text'])}</b>",
              f"🔗 Ссылка: <code>{utils.escape_html(c['button_url'])}</code>"]

    rows = [[_btn("✏️ Изменить текст", f"rem:text:{key}")],
            [_btn("🔘 Изменить текст кнопки", f"rem:btn:{key}"),
             _btn("🔗 Изменить ссылку", f"rem:url:{key}")],
            [_btn("👁 Предпросмотр", f"rem:prev:{key}")]]
    if is_rem:
        rows.append([_btn("🔴 Выключить автонапоминания" if c["enabled"]
                          else "🟢 Включить автонапоминания", f"rem:toggle:{key}")])
        rows.append([_btn("⏱ Частота отправки", f"rem:cool:{key}"),
                     _btn("🕒 Пауза", f"rem:delay:{key}")])
        rows.append([_btn("🧪 Отправить тест себе", f"rem:test:{key}")])
        rows.append([_btn("📊 Статистика", f"rem:stats:{key}"),
                     _btn("🚀 Новая кампания", f"rem:newask:{key}")])
    rows.append([_btn("⬅️ Назад", "rem:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                     disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML",
                                  disable_web_page_preview=True)


@router.callback_query(F.data == "adm:notes_msgs", IsAdmin())
@router.callback_query(F.data == "rem:home", IsAdmin())
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rem = rs.get_campaign(rs.REMINDER)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📚 Сообщение кнопки «КОНСПЕКТЫ ЕНТ»", f"rem:open:{rs.MANUAL}")],
        [_btn(("🟢 " if rem["enabled"] else "🔴 ") + "Автонапоминание о конспектах",
              f"rem:open:{rs.REMINDER}")],
        [_btn("↩️ В админку", "m:admin")],
    ])
    await _show(call,
                "📚 <b>Конспекты: тексты и напоминания</b>\n\n"
                "Здесь два независимых сообщения:\n\n"
                "• то, что бот отвечает на кнопку «КОНСПЕКТЫ ЕНТ» — работает всегда;\n"
                "• автонапоминание — его можно включать и выключать.\n\n"
                "Правка одного не меняет другое.", kb)
    await call.answer()


@router.callback_query(F.data.startswith("rem:open:"), IsAdmin())
async def cb_open(call: CallbackQuery, state: FSMContext):
    await state.clear()
    key = call.data.split(":")[2]
    text, kb = await asyncio.to_thread(_screen, key)
    await _show(call, text, kb)
    await call.answer()


# ===================== правка текстов =====================

async def _ask(call: CallbackQuery, state: FSMContext, st: State, key: str, prompt: str):
    await state.set_state(st)
    await state.update_data(rem_key=key)
    await call.message.answer(prompt + "\n\n/cancel — отмена", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("rem:text:"), IsAdmin())
async def cb_text(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    c = rs.get_campaign(key)
    await _ask(call, state, RemStates.waiting_text, key,
               "✏️ Пришлите новый текст сообщения.\n\n"
               "Можно с HTML-разметкой: <code>&lt;b&gt;жирный&lt;/b&gt;</code>.\n\n"
               f"Сейчас:\n<blockquote>{utils.escape_html(c['message_text'])}</blockquote>")


@router.callback_query(F.data.startswith("rem:btn:"), IsAdmin())
async def cb_btn(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    c = rs.get_campaign(key)
    await _ask(call, state, RemStates.waiting_button, key,
               f"🔘 Пришлите новую подпись кнопки.\n\nСейчас: <b>{utils.escape_html(c['button_text'])}</b>")


@router.callback_query(F.data.startswith("rem:url:"), IsAdmin())
async def cb_url(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    c = rs.get_campaign(key)
    await _ask(call, state, RemStates.waiting_url, key,
               "🔗 Пришлите новую ссылку кнопки.\n\n"
               f"Сейчас:\n<code>{utils.escape_html(c['button_url'])}</code>")


async def _save(message: Message, state: FSMContext, field: str, value):
    data = await state.get_data()
    key = data.get("rem_key") or rs.MANUAL
    await asyncio.to_thread(rs.update_campaign, key, **{field: value})
    await state.clear()
    text, kb = await asyncio.to_thread(_screen, key)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=kb,
                         parse_mode="HTML", disable_web_page_preview=True)


@router.message(RemStates.waiting_text, IsAdmin())
async def msg_text(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear(); await message.answer("❌ Отменено."); return
    if not (message.html_text or message.text):
        await message.answer("Нужен текст сообщения."); return
    await _save(message, state, "message_text",
                (message.html_text or message.text).strip()[:3500])


@router.message(RemStates.waiting_button, IsAdmin())
async def msg_button(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear(); await message.answer("❌ Отменено."); return
    t = (message.text or "").strip()
    if not t or len(t) > 64:
        await message.answer("Подпись кнопки — до 64 символов."); return
    await _save(message, state, "button_text", t)


@router.message(RemStates.waiting_url, IsAdmin())
async def msg_url(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear(); await message.answer("❌ Отменено."); return
    u = (message.text or "").strip()
    if not (u.startswith("https://") or u.startswith("http://")):
        await message.answer("Ссылка должна начинаться с https://"); return
    await _save(message, state, "button_url", u)


# ===================== предпросмотр и тест =====================

@router.callback_query(F.data.startswith("rem:prev:"), IsAdmin())
async def cb_preview(call: CallbackQuery):
    key = call.data.split(":")[2]
    text, kb = await asyncio.to_thread(build_message, key)
    await call.message.answer("👁 <b>Так это увидит пользователь:</b>", parse_mode="HTML")
    await call.message.answer(text, reply_markup=kb, parse_mode="HTML",
                              disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("rem:test:"), IsAdmin())
async def cb_test(call: CallbackQuery, bot: Bot):
    """Настоящая отправка себе — тем же путём, что уходит людям."""
    key = call.data.split(":")[2]
    camp = await asyncio.to_thread(rs.get_campaign, key)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=camp["button_text"], url=camp["button_url"])]])
    try:
        await bot.send_message(call.from_user.id, camp["message_text"],
                               reply_markup=kb, parse_mode="HTML",
                               disable_web_page_preview=True)
        await call.answer("Отправил вам в личку")
    except Exception as e:
        await call.answer(f"Не смог отправить: {e}", show_alert=True)


# ===================== включение, частота, пауза =====================

@router.callback_query(F.data.startswith("rem:toggle:"), IsAdmin())
async def cb_toggle(call: CallbackQuery):
    key = call.data.split(":")[2]
    c = await asyncio.to_thread(rs.get_campaign, key)
    await asyncio.to_thread(rs.update_campaign, key, enabled=0 if c["enabled"] else 1)
    text, kb = await asyncio.to_thread(_screen, key)
    await _show(call, text, kb)
    await call.answer("Готово")


@router.callback_query(F.data.startswith("rem:cool:"), IsAdmin())
async def cb_cool(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    rows = [[_btn(label, f"rem:coolset:{key}:{sec}")] for sec, label in COOLDOWNS]
    rows.append([_btn("✏️ Своё значение (в часах)", f"rem:coolown:{key}")])
    rows.append([_btn("⬅️ Назад", f"rem:open:{key}")])
    await _show(call, "⏱ <b>Не чаще одного раза в…</b>\n\n"
                      "Реже — спокойнее для людей. Чаще — назойливее.",
                InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("rem:coolset:"), IsAdmin())
async def cb_cool_set(call: CallbackQuery):
    _, _, key, sec = call.data.split(":")
    await asyncio.to_thread(rs.update_campaign, key, cooldown_seconds=int(sec))
    text, kb = await asyncio.to_thread(_screen, key)
    await _show(call, text, kb)
    await call.answer("Сохранено")


@router.callback_query(F.data.startswith("rem:coolown:"), IsAdmin())
async def cb_cool_own(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    await _ask(call, state, RemStates.waiting_cooldown, key,
               "⏱ Введите число часов между напоминаниями.\n\nНапример: <code>72</code>")


@router.message(RemStates.waiting_cooldown, IsAdmin())
async def msg_cooldown(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear(); await message.answer("❌ Отменено."); return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 24 * 365):
        await message.answer("Введите число часов, например 72."); return
    await _save(message, state, "cooldown_seconds", int(raw) * 3600)


@router.callback_query(F.data.startswith("rem:delay:"), IsAdmin())
async def cb_delay(call: CallbackQuery):
    key = call.data.split(":")[2]
    rows = [[_btn(label, f"rem:delayset:{key}:{sec}")] for sec, label in DELAYS]
    rows.append([_btn("⬅️ Назад", f"rem:open:{key}")])
    await _show(call, "🕒 <b>Пауза после активности</b>\n\n"
                      "Сколько ждать после того, как человек закончил тест или "
                      "другое действие, прежде чем показать напоминание. Чтобы "
                      "сообщение не прилетало через секунду после последнего вопроса.",
                InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("rem:delayset:"), IsAdmin())
async def cb_delay_set(call: CallbackQuery):
    _, _, key, sec = call.data.split(":")
    await asyncio.to_thread(rs.update_campaign, key, safe_delay_seconds=int(sec))
    text, kb = await asyncio.to_thread(_screen, key)
    await _show(call, text, kb)
    await call.answer("Сохранено")


# ===================== статистика и новая кампания =====================

@router.callback_query(F.data.startswith("rem:stats:"), IsAdmin())
async def cb_stats(call: CallbackQuery):
    key = call.data.split(":")[2]
    s = await asyncio.to_thread(rs.stats, key)
    c = s["campaign"]
    text = (
        "📊 <b>Автонапоминания о конспектах</b>\n\n"
        f"Статус: {'🟢 Включено' if c['enabled'] else '🔴 Выключено'}\n"
        f"Кампания: <code>{s['label']}</code>\n"
        f"Частота: <b>{rs.human_cooldown(c['cooldown_seconds'])}</b>\n"
        f"Пауза после активности: <b>{rs.human_cooldown(c['safe_delay_seconds'])}</b>\n\n"
        f"👥 Всего пользователей: <b>{s['total_users']}</b>\n"
        f"📬 Получили эту версию: <b>{s['got_current_version']}</b>\n"
        f"📨 Отправлено всего: <b>{s['sent_total']}</b>\n"
        f"📅 Отправлено сегодня: <b>{s['sent_today']}</b>\n"
        f"⏸ Отложено: <b>{s['deferred']}</b> (из них из-за теста/дуэли: "
        f"<b>{s['deferred_busy']}</b>)\n"
        f"🚫 Заблокировали бота: <b>{s['blocked']}</b>\n"
        f"🕐 Последняя отправка: <b>{s['last_sent_at']}</b>\n\n"
        f"🔘 Кнопка: <b>{utils.escape_html(c['button_text'])}</b>\n"
        f"🔗 Ссылка: <code>{utils.escape_html(c['button_url'])}</code>")
    await _show(call, text, InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔄 Обновить", f"rem:stats:{key}")],
        [_btn("⬅️ Назад", f"rem:open:{key}")]]))
    await call.answer()


@router.callback_query(F.data.startswith("rem:newask:"), IsAdmin())
async def cb_new_ask(call: CallbackQuery):
    key = call.data.split(":")[2]
    await _show(call,
                "🚀 <b>Создать новую версию автонапоминания?</b>\n\n"
                "Люди, которые уже видели прошлую версию, снова попадут в очередь — "
                "но по обычному правилу частоты, разом никому ничего не уйдёт.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("✅ Да, создать", f"rem:new:{key}")],
                    [_btn("❌ Отмена", f"rem:open:{key}")]]))
    await call.answer()


@router.callback_query(F.data.startswith("rem:new:"), IsAdmin())
async def cb_new(call: CallbackQuery):
    key = call.data.split(":")[2]
    c = await asyncio.to_thread(rs.new_version, key, call.from_user.id)
    text, kb = await asyncio.to_thread(_screen, key)
    await _show(call, f"🚀 Создана кампания <code>{rs.campaign_label(key)}</code>.\n\n"
                      + text, kb)
    await call.answer("Готово")


# ===================== карточка пользователя =====================

@router.callback_query(F.data.startswith("rem:user:"), IsAdmin())
async def cb_user(call: CallbackQuery):
    tg_id = int(call.data.split(":")[2])
    rep = await asyncio.to_thread(rs.user_report, tg_id)
    if rep.get("never"):
        body = "Напоминаний ещё не получал."
    else:
        body = (f"Последнее: <b>{rep['last_sent_at']}</b>\n"
                f"Всего отправлено: <b>{rep['send_count']}</b>\n"
                f"Статус: <b>{rep['last_status']}</b>"
                + (f" ({rep['last_skip_reason']})" if rep.get("last_skip_reason") else "")
                + f"\nСледующее не раньше: <b>{rep['next_allowed_at']}</b>")
    await _show(call,
                f"📚 <b>Конспекты / напоминания</b>\n"
                f"Пользователь: <code>{tg_id}</code>\n"
                f"Кампания: <code>{rep['campaign']}</code>\n\n{body}",
                InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("♻️ Сбросить напоминания", f"rem:resetask:{tg_id}")],
                    [_btn("⬅️ Назад", "rem:home")]]))
    await call.answer()


@router.callback_query(F.data.startswith("rem:resetask:"), IsOwner())
async def cb_reset_ask(call: CallbackQuery):
    tg_id = int(call.data.split(":")[2])
    await _show(call,
                f"♻️ Сбросить историю напоминаний пользователю <code>{tg_id}</code>?\n\n"
                "Он снова сможет получить сообщение при ближайшей проверке.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("✅ Да, сбросить", f"rem:reset:{tg_id}")],
                    [_btn("❌ Отмена", f"rem:user:{tg_id}")]]))
    await call.answer()


@router.callback_query(F.data.startswith("rem:reset:"), IsOwner())
async def cb_reset(call: CallbackQuery):
    tg_id = int(call.data.split(":")[2])
    await asyncio.to_thread(rs.reset_user, tg_id)
    await call.answer("Сброшено")
    call.data = f"rem:user:{tg_id}"
    await cb_user(call)
