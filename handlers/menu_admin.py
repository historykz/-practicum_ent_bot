"""«🏠 Главное меню бота» (v100): владелец сам добавляет, убирает, переименовывает,
переставляет, временно показывает кнопки главного меню и правит тексты разделов.
Логика — services/main_menu.py (общая с админкой сайта /admin/menu).
Здесь же кнопки «разделов со своим текстом» для учеников (mm:<номер>)."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import utils
from filters import IsOwner
from services import main_menu as mm
from services import trash

router = Router(name="menu_admin")
log = logging.getLogger(__name__)
esc = utils.escape_html


class MenuStates(StatesGroup):
    value = State()          # что именно ждём — в state["mm_await"]


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, text, kb=None):
    is_call = hasattr(target, "data") and hasattr(target, "message")
    msg = target.message if is_call else target
    if is_call:
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


def _audit(actor, action, title, details=""):
    trash.audit(actor, "", "bot", action, "menu_button", "Главное меню: " + title, None, details)


# ───────────────────────── ученик: раздел своей кнопки ─────────────────────────

@router.callback_query(F.data.startswith("mm:"))
async def cb_section(call: CallbackQuery, user: dict = None):
    try:
        b = await asyncio.to_thread(mm.get_button, int(call.data.split(":")[1]))
    except (ValueError, IndexError):
        b = None
    if not b or b["kind"] != "text" or not mm.is_live(b):
        await call.answer("Этот раздел сейчас недоступен.", show_alert=True)
        return
    await call.answer()
    lang = ((user or {}).get("language") or "ru") if user else "ru"
    await mm.show_section(call.message, b, lang)


# ───────────────────────── владелец: список кнопок ─────────────────────────

def _menu_screen():
    rows = mm.all_buttons()
    lines = ["🏠 <b>Главное меню бота</b>\n",
             "Нажмите на кнопку, чтобы её переименовать, выключить, передвинуть, показать на время или удалить. "
             "«🛠 Admin» всегда последняя и видна только админам.\n"]
    kb = []
    for b in rows:
        icon = "👁" if mm.is_live(b) else "🚫"
        lines.append(f"{icon} {esc(mm.label(b, 'ru'))} <i>· {mm.KINDS.get(b['kind'], b['kind'])} · {mm.status(b)}"
                     f"{' · на всю ширину' if b['full_row'] else ' · пополам'}</i>")
        kb.append([_btn(f"{icon} {mm.label(b, 'ru')}"[:60], f"mmadm:card:{b['id']}")])
    kb += [[_btn("➕ Добавить кнопку", "mmadm:new")],
           [_btn("📝 Тексты меню и разделов", "mmadm:texts")],
           [_btn("👁 Показать меню как у ученика", "mmadm:preview")],
           [_btn("↩️ Вернуть встроенные кнопки как было", "mmadm:reset")],
           [_btn("↩️ В админку", "m:admin")]]
    return "\n".join(lines), _kb(kb)


@router.callback_query(F.data == "mmadm:menu", IsOwner())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *await asyncio.to_thread(_menu_screen))


@router.callback_query(F.data == "mmadm:preview", IsOwner())
async def cb_preview(call: CallbackQuery):
    await call.answer()
    await call.message.answer(mm.menu_text("ru"), parse_mode="HTML", reply_markup=mm.keyboard("ru", False))


@router.callback_query(F.data == "mmadm:reset", IsOwner())
async def cb_reset(call: CallbackQuery):
    await call.answer()
    await _show(call, "↩️ <b>Вернуть встроенные кнопки как было?</b>\n\nНадписи, порядок и видимость встроенных кнопок "
                      "станут прежними. Ваши кнопки останутся.",
                _kb([[_btn("✅ Вернуть", "mmadm:resetok"), _btn("❌ Отмена", "mmadm:menu")]]))


@router.callback_query(F.data == "mmadm:resetok", IsOwner())
async def cb_reset_ok(call: CallbackQuery):
    await asyncio.to_thread(mm.reset_builtins)
    _audit(call.from_user.id, "settings", "встроенные кнопки как было")
    await call.answer("Готово")
    await _show(call, *await asyncio.to_thread(_menu_screen))


# ───────────────────────── карточка кнопки ─────────────────────────

def _card(b: dict):
    i = b["id"]
    lines = [f"🔘 <b>{esc(mm.label(b, 'ru'))}</b>",
             f"Вид: {mm.KINDS.get(b['kind'], b['kind'])} · {mm.status(b)}",
             f"Казахская надпись: {esc(b.get('label_kz') or 'как русская')}",
             f"Ширина: {'на всю ширину' if b['full_row'] else 'пополам (по две в ряд)'}",
             f"Показывать: с {b.get('show_from') or 'сейчас'} до {b.get('show_until') or 'бессрочно'} (время Астаны)"]
    if b["kind"] in ("link", "webapp"):
        lines.append(f"Ссылка: {esc(b.get('url') or '—')}")
    if b["kind"] == "action":
        lines.append(f"Открывает: {mm.ACTIONS.get(b.get('action'), b.get('action'))}")
    if b["kind"] == "text":
        body = (b.get("body_ru") or "").strip()
        lines.append(f"Текст раздела: {esc(body[:300]) + ('…' if len(body) > 300 else '') if body else 'пусто'}")
        links = mm.section_links(b)
        lines.append("Кнопки под текстом: " + (", ".join(esc(t) for t, _ in links) if links else "нет"))
        if b.get("photo_file_id"):
            lines.append("🖼 Есть картинка")
    rows = [[_btn("✏️ Надпись", f"mmadm:ask:{i}:label_ru"), _btn("✏️ По-казахски", f"mmadm:ask:{i}:label_kz")],
            [_btn("⏸ Выключить" if b["enabled"] else "▶️ Включить", f"mmadm:tg:{i}:enabled"),
             _btn("↔️ Пополам" if b["full_row"] else "⬛ На всю ширину", f"mmadm:tg:{i}:full_row")],
            [_btn("⬆️ Выше", f"mmadm:mv:{i}:up"), _btn("⬇️ Ниже", f"mmadm:mv:{i}:down")],
            [_btn("⏰ Показывать до…", f"mmadm:ask:{i}:show_until"), _btn("⏳ Показывать с…", f"mmadm:ask:{i}:show_from")]]
    if b["kind"] in ("link", "webapp"):
        rows.append([_btn("🔗 Ссылка", f"mmadm:ask:{i}:url")])
    if b["kind"] == "text":
        rows.append([_btn("📝 Текст раздела", f"mmadm:ask:{i}:body_ru"), _btn("📝 Текст по-казахски", f"mmadm:ask:{i}:body_kz")])
        rows.append([_btn("🔗 Кнопки под текстом", f"mmadm:ask:{i}:links"), _btn("👁 Как увидит ученик", f"mm:{i}")])
    if b["kind"] == "action":
        rows.append([_btn("🔀 Какой раздел открывает", f"mmadm:act:{i}")])
    if b["kind"] != "builtin":
        rows.append([_btn("🗑 Удалить", f"mmadm:del:{i}")])
    rows.append([_btn("↩️ К меню", "mmadm:menu")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data.startswith("mmadm:card:"), IsOwner())
async def cb_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    b = await asyncio.to_thread(mm.get_button, int(call.data.split(":")[2]))
    await call.answer()
    if not b:
        await _show(call, *await asyncio.to_thread(_menu_screen))
        return
    await _show(call, *_card(b))


@router.callback_query(F.data.startswith("mmadm:tg:"), IsOwner())
async def cb_toggle(call: CallbackQuery):
    _, _, bid, field = call.data.split(":")
    b = await asyncio.to_thread(mm.get_button, int(bid))
    if not b:
        await call.answer("Кнопка не найдена", show_alert=True)
        return
    await asyncio.to_thread(mm.update, int(bid), **{field: 0 if b[field] else 1})
    _audit(call.from_user.id, "settings", mm.label(b, "ru"), f"{field}: {'выкл' if b[field] else 'вкл'}")
    await call.answer("Сохранено")
    await _show(call, *_card(await asyncio.to_thread(mm.get_button, int(bid))))


@router.callback_query(F.data.startswith("mmadm:mv:"), IsOwner())
async def cb_move(call: CallbackQuery):
    _, _, bid, d = call.data.split(":")
    await asyncio.to_thread(mm.move, int(bid), d)
    await call.answer("Передвинута")
    await _show(call, *_card(await asyncio.to_thread(mm.get_button, int(bid))))


@router.callback_query(F.data.startswith("mmadm:act:"), IsOwner())
async def cb_action(call: CallbackQuery):
    parts = call.data.split(":")
    bid = int(parts[2])
    if len(parts) > 3:
        action = ":".join(parts[3:])
        try:
            await asyncio.to_thread(mm.update, bid, action=action)
        except ValueError as e:
            await call.answer(str(e), show_alert=True)
            return
        await call.answer("Сохранено")
        await _show(call, *_card(await asyncio.to_thread(mm.get_button, bid)))
        return
    await call.answer()
    await _show(call, "🔀 <b>Какой раздел бота открывает кнопка?</b>",
                _kb([[_btn(v, f"mmadm:act:{bid}:{k}")] for k, v in mm.ACTIONS.items()] + [[_btn("↩️ Назад", f"mmadm:card:{bid}")]]))


@router.callback_query(F.data.startswith("mmadm:del:"), IsOwner())
async def cb_delete(call: CallbackQuery):
    bid = int(call.data.split(":")[2])
    await call.answer()
    await _show(call, "🗑 <b>Удалить кнопку?</b>\n\n" + trash.ASK_MSG,
                _kb([[_btn("🗑 Удалить", f"mmadm:delok:{bid}"), _btn("❌ Отмена", f"mmadm:card:{bid}")]]))


@router.callback_query(F.data.startswith("mmadm:delok:"), IsOwner())
async def cb_delete_ok(call: CallbackQuery):
    try:
        await asyncio.to_thread(mm.delete, int(call.data.split(":")[2]), call.from_user.id, "bot")
        await call.answer(trash.MOVED_MSG, show_alert=True)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
    await _show(call, *await asyncio.to_thread(_menu_screen))


# ───────────────────────── ввод текста ─────────────────────────

ASK = {
    "label_ru": "✏️ Пришлите новую надпись на кнопке (до 64 символов, можно с эмодзи). "
                "Для встроенной кнопки «-» вернёт прежнюю надпись.",
    "label_kz": "✏️ Пришлите надпись по-казахски. «-» — как русская.",
    "url": "🔗 Пришлите ссылку: https://… или @имя (канал, менеджер, сайт). Для страницы мини-приложения — путь вида /learn/5.",
    "show_until": "⏰ До какого момента показывать кнопку? Например <code>31.12.2026</code> или <code>2026-12-31 18:00</code> "
                  "(время Астаны). «-» — бессрочно. После этого кнопка исчезнет из меню сама.",
    "show_from": "⏳ С какого момента показывать кнопку? Например <code>01.12.2026 09:00</code>. «-» — сразу.",
    "body_ru": "📝 Пришлите текст раздела одним сообщением. Жирный, ссылки и эмодзи сохранятся. "
               "Можно прислать картинку с подписью — она будет вверху раздела.",
    "body_kz": "📝 Пришлите текст раздела по-казахски. «-» — как русский.",
    "links": "🔗 Кнопки под текстом раздела, каждая с новой строки:\n<code>Купить курс | https://t.me/manager</code>\n"
             "<code>Наш канал | @smart_ent</code>\n«-» — без кнопок.",
}


@router.callback_query(F.data.startswith("mmadm:ask:"), IsOwner())
async def cb_ask(call: CallbackQuery, state: FSMContext):
    _, _, bid, field = call.data.split(":")
    await state.set_state(MenuStates.value)
    await state.update_data(mm_await={"bid": int(bid), "field": field})
    await call.answer()
    await call.message.answer(ASK[field] + "\n\n/cancel — отмена", parse_mode="HTML")


@router.message(MenuStates.value, IsOwner())
async def msg_value(message: Message, state: FSMContext):
    data = await state.get_data()
    w = data.get("mm_await") or {}
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("❌ Отменено. Раздел: «Админка» → «🏠 Главное меню бота».")
        return
    raw = message.html_text if (message.text or message.caption) else ""
    plain = (message.text or message.caption or "").strip()
    # новая кнопка: мастер
    if w.get("new"):
        return await _new_step(message, state, w, raw, plain)
    # тексты меню и разделов
    if w.get("text_key"):
        value = "" if plain == "-" else raw
        try:
            await asyncio.to_thread(mm.set_text, w["text_key"], w["lang"], value)
        except ValueError as e:
            await message.answer(str(e))
            return
        await state.clear()
        _audit(message.from_user.id, "save", mm.TEXTS[w["text_key"]], w["lang"])
        await message.answer("✅ Сохранено." + (" Вернулся прежний текст." if not value else ""))
        await _show(message, *_texts_screen())
        return
    bid, field = w.get("bid"), w.get("field")
    b = await asyncio.to_thread(mm.get_button, bid or 0)
    if not b:
        await state.clear()
        await message.answer("Кнопка не найдена.")
        return
    try:
        if field == "body_ru" and message.photo:
            await asyncio.to_thread(mm.update, bid, photo_file_id=message.photo[-1].file_id, body_ru=raw)
        elif field in ("body_ru", "body_kz"):
            await asyncio.to_thread(mm.update, bid, **{field: "" if plain == "-" else raw})
        elif field == "links":
            await asyncio.to_thread(mm.update, bid, links="" if plain == "-" else mm.parse_links(plain))
        elif field == "label_kz" and plain == "-":
            await asyncio.to_thread(mm.update, bid, label_kz="")
        elif field == "label_ru" and plain == "-" and b["kind"] == "builtin":
            await asyncio.to_thread(mm.update, bid, label_ru="")      # прежняя надпись
        else:
            await asyncio.to_thread(mm.update, bid, **{field: plain})
    except ValueError as e:
        await message.answer("⚠️ " + str(e) + "\nПопробуйте ещё раз или /cancel.")
        return
    await state.clear()
    _audit(message.from_user.id, "save", mm.label(b, "ru"), field)
    await message.answer("✅ Сохранено.")
    await _show(message, *_card(await asyncio.to_thread(mm.get_button, bid)))


# ───────────────────────── новая кнопка ─────────────────────────

@router.callback_query(F.data == "mmadm:new", IsOwner())
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, "➕ <b>Новая кнопка главного меню</b>\n\nЧто она будет делать?",
                _kb([[_btn("📝 Открывать раздел со своим текстом", "mmadm:nk:text")],
                     [_btn("🔗 Открывать ссылку (канал, менеджер, сайт)", "mmadm:nk:link")],
                     [_btn("📱 Открывать страницу мини-приложения", "mmadm:nk:webapp")],
                     [_btn("🔀 Открывать готовый раздел бота", "mmadm:nk:action")],
                     [_btn("❌ Отмена", "mmadm:menu")]]))


@router.callback_query(F.data.startswith("mmadm:nk:"), IsOwner())
async def cb_new_kind(call: CallbackQuery, state: FSMContext):
    kind = call.data.split(":")[2]
    await state.set_state(MenuStates.value)
    await state.update_data(mm_await={"new": True, "kind": kind, "step": "label"})
    await call.answer()
    await call.message.answer("✏️ Надпись на кнопке? Например: <code>🎓 Купить курсы ЕНТ</code>\n\n/cancel — отмена",
                              parse_mode="HTML")


async def _new_step(message: Message, state: FSMContext, w: dict, raw: str, plain: str):
    kind, step = w["kind"], w["step"]
    if step == "label":
        if not plain or len(plain) > 64:
            await message.answer("Нужна надпись до 64 символов.")
            return
        w["label"] = plain
        if kind == "action":
            await state.update_data(mm_await=w)
            await state.set_state(None)
            await message.answer("🔀 Какой раздел бота открывает кнопка?", reply_markup=_kb(
                [[_btn(v, f"mmadm:na:{k}")] for k, v in mm.ACTIONS.items()] + [[_btn("❌ Отмена", "mmadm:menu")]]))
            return
        w["step"] = "content"
        await state.update_data(mm_await=w)
        await message.answer({"link": ASK["url"], "webapp": ASK["url"], "text": ASK["body_ru"]}[kind]
                             + "\n\n/cancel — отмена", parse_mode="HTML")
        return
    # step == content
    try:
        if kind == "text":
            bid = await asyncio.to_thread(mm.create, "text", w["label"], body=raw,
                                          photo=message.photo[-1].file_id if message.photo else "")
        else:
            bid = await asyncio.to_thread(mm.create, kind, w["label"], url=plain)
    except ValueError as e:
        await message.answer("⚠️ " + str(e) + "\nПопробуйте ещё раз или /cancel.")
        return
    await state.clear()
    _audit(message.from_user.id, "create", w["label"], mm.KINDS[kind])
    await message.answer("✅ Кнопка добавлена в конец меню. Ниже можно переставить её, сделать пополам, "
                         "показать на время или добавить кнопки-ссылки под текст.")
    await _show(message, *_card(await asyncio.to_thread(mm.get_button, bid)))


@router.callback_query(F.data.startswith("mmadm:na:"), IsOwner())
async def cb_new_action(call: CallbackQuery, state: FSMContext):
    w = (await state.get_data()).get("mm_await") or {}
    action = call.data[len("mmadm:na:"):]
    if not w.get("label"):
        await call.answer("Начните заново", show_alert=True)
        return
    try:
        bid = await asyncio.to_thread(mm.create, "action", w["label"], action=action)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return
    await state.clear()
    _audit(call.from_user.id, "create", w["label"], mm.ACTIONS.get(action, action))
    await call.answer("Добавлена")
    await _show(call, *_card(await asyncio.to_thread(mm.get_button, bid)))


# ───────────────────────── тексты ─────────────────────────

def _texts_screen():
    lines = ["📝 <b>Тексты меню и разделов</b>\n",
             "Пришлите новый текст — он сразу появится в боте. «-» вернёт прежний текст.\n"]
    rows = []
    for key, title in mm.TEXTS.items():
        own = bool(mm.get_text(key, "ru"))
        lines.append(f"• {title}: {'свой текст' if own else 'как было'}")
        rows.append([_btn(f"✏️ {title}", f"mmadm:tx:{key}:ru"), _btn("по-казахски", f"mmadm:tx:{key}:kz")])
    lines.append("\nТексты «Конспекты ЕНТ» — в «📚 Тексты и автонапоминания», текст Премиума — в «💎 Премиум».")
    rows.append([_btn("↩️ К меню", "mmadm:menu")])
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "mmadm:texts", IsOwner())
async def cb_texts(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *_texts_screen())


@router.callback_query(F.data.startswith("mmadm:tx:"), IsOwner())
async def cb_text_edit(call: CallbackQuery, state: FSMContext):
    _, _, key, lang = call.data.split(":")
    if key not in mm.TEXTS:
        await call.answer()
        return
    if key == "support_text" and lang == "kz":
        await call.answer("Текст техподдержки один для обоих языков.", show_alert=True)
        return
    await state.set_state(MenuStates.value)
    await state.update_data(mm_await={"text_key": key, "lang": lang})
    await call.answer()
    cur = mm.get_text(key, lang) or mm.default_text(key, lang)
    hint = "\n\nВ тексте можно оставить {manager} — подставится ник менеджера." if key in ("help_text", "support_text") else ""
    await call.message.answer(f"✏️ <b>{mm.TEXTS[key]}</b> ({'казахский' if lang == 'kz' else 'русский'}). Сейчас так:")
    await call.message.answer(cur or "—", parse_mode="HTML", disable_web_page_preview=True)
    await call.message.answer("Пришлите новый текст одним сообщением. «-» — вернуть прежний." + hint + "\n\n/cancel — отмена")
