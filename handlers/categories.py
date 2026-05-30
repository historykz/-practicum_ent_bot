"""
Управление разделами (категориями) тестов.

Админ может:
- Создать раздел («Биология», «История» и т.д.)
- Удалить раздел
- Посмотреть список

Юзер в каталоге сначала видит разделы, потом тесты внутри раздела.
"""
import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin

router = Router(name="categories")
log = logging.getLogger(__name__)


class CategoryStates(StatesGroup):
    waiting_name = State()


# ========= Меню разделов (админ) =========

@router.callback_query(F.data == "adm:categories", IsAdmin())
async def cb_adm_categories(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, id")

    text = ("📂 <b>Разделы каталога</b>\n\n"
            "Разделы = предметы. ⭐️ = обязательный (виден всем), "
            "🎓 = профильный (юзер выбирает сам).\n\n")
    if not cats:
        text += "<i>Пока нет ни одного раздела.</i>\n\nНажмите ➕ чтобы создать первый."
    else:
        text += "<b>Существующие:</b>\n"
        for c in cats:
            cnt = db.fetchone(
                "SELECT COUNT(*) AS c FROM tests WHERE category_id=? AND status='active'",
                (c['id'],))['c']
            mark = "⭐️" if c.get('is_required') else "🎓"
            text += f"{mark} {c.get('emoji') or '📚'} <b>{utils.escape_html(c['name'])}</b> — {cnt} тестов\n"

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать раздел", callback_data="cat:create")
    if cats:
        for c in cats[:20]:
            mark = "⭐️" if c.get('is_required') else "🎓"
            kb.button(text=f"{mark} {c['name'][:28]}",
                      callback_data=f"cat:open:{c['id']}")
    kb.button(text="↩️ Назад", callback_data="m:admin")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("cat:open:"), IsAdmin())
async def cb_cat_open(call: CallbackQuery):
    """Карточка раздела с управлением."""
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    c = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
    if not c:
        await call.answer("Не найден.", show_alert=True)
        return
    is_req = bool(c.get('is_required'))
    type_label = "⭐️ Обязательный (виден всем)" if is_req else "🎓 Профильный (выбирается)"
    cnt = db.fetchone(
        "SELECT COUNT(*) AS c FROM tests WHERE category_id=? AND status='active'",
        (cat_id,))['c']
    text = (f"📂 <b>{c.get('emoji') or '📚'} {utils.escape_html(c['name'])}</b>\n\n"
            f"Тип: {type_label}\n"
            f"Тестов: {cnt}")
    kb = InlineKeyboardBuilder()
    if is_req:
        kb.button(text="🎓 Сделать профильным", callback_data=f"cat:req:{cat_id}:0")
    else:
        kb.button(text="⭐️ Сделать обязательным", callback_data=f"cat:req:{cat_id}:1")
    kb.button(text="🗑 Удалить раздел", callback_data=f"cat:del:{cat_id}")
    kb.button(text="↩️ К разделам", callback_data="adm:categories")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("cat:req:"), IsAdmin())
async def cb_cat_toggle_required(call: CallbackQuery):
    """Переключить обязательный/профильный."""
    try:
        _, _, cat_id, val = call.data.split(":")
        cat_id = int(cat_id)
        val = int(val)
    except (ValueError, IndexError):
        await call.answer()
        return
    db.execute("UPDATE test_categories SET is_required=? WHERE id=?", (val, cat_id))
    await call.answer("✅ Обновлено" if val else "✅ Теперь профильный")
    # Перерисуем карточку
    fake = type('F', (), {'data': f"cat:open:{cat_id}", 'message': call.message,
                          'from_user': call.from_user, 'bot': call.bot,
                          'answer': call.answer})()
    await cb_cat_open(fake)


@router.callback_query(F.data == "cat:create", IsAdmin())
async def cb_cat_create(call: CallbackQuery, state: FSMContext):
    await state.set_state(CategoryStates.waiting_name)
    await call.message.answer(
        "📂 <b>Создание нового раздела</b>\n\n"
        "Введите название раздела.\n\n"
        "Можно с эмодзи в начале — оно будет иконкой раздела.\n\n"
        "<b>Примеры:</b>\n"
        "<code>🧬 Биология</code>\n"
        "<code>📜 История Казахстана</code>\n"
        "<code>📐 Математическая грамотность</code>\n"
        "<code>🌍 География</code>",
        parse_mode="HTML")
    await call.answer()


@router.message(CategoryStates.waiting_name, IsAdmin())
async def s_cat_name(message: Message, state: FSMContext):
    raw = (message.text or "").strip()[:60]
    if not raw:
        await message.answer("❌ Пустое название.")
        return

    # Извлекаем эмодзи если есть
    import re
    emoji = "📚"
    name = raw
    # Простой способ: если первый "символ" не буква/цифра — берём его как emoji
    parts = raw.split(maxsplit=1)
    if len(parts) == 2 and not parts[0].isalnum():
        emoji = parts[0][:4]
        name = parts[1].strip()
    elif len(raw) > 0 and not raw[0].isalnum() and not raw[0].isspace():
        # Эмодзи без пробела
        m = re.match(r'^([^\w\s]+)\s*(.*)$', raw)
        if m:
            emoji = m.group(1)[:4]
            name = m.group(2).strip() or raw

    if not name:
        name = raw

    try:
        db.execute(
            "INSERT INTO test_categories (name, emoji, created_by) VALUES (?,?,?)",
            (name[:60], emoji, message.from_user.id))
    except Exception as e:
        if "UNIQUE" in str(e):
            await message.answer(f"❌ Раздел «{utils.escape_html(name)}» уже существует.")
        else:
            await message.answer(f"❌ Ошибка: {e}")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"✅ Раздел создан!\n\n"
        f"{emoji} <b>{utils.escape_html(name)}</b>\n\n"
        f"Теперь при создании теста его можно отнести к этому разделу.",
        parse_mode="HTML")


@router.callback_query(F.data.startswith("cat:del:"), IsAdmin())
async def cb_cat_del(call: CallbackQuery):
    try:
        cid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cid,))
    if not cat:
        await call.answer("Раздел не найден.", show_alert=True)
        return
    # Сколько тестов в разделе
    cnt = db.fetchone("SELECT COUNT(*) AS c FROM tests WHERE category_id=?", (cid,))['c']

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"cat:delconfirm:{cid}")
    kb.button(text="❌ Отмена", callback_data="adm:categories")
    kb.adjust(1)
    await call.message.answer(
        f"🗑 Удалить раздел <b>{utils.escape_html(cat['name'])}</b>?\n\n"
        f"📚 Тестов в разделе: <b>{cnt}</b>\n\n"
        f"⚠️ Сами тесты <b>НЕ удаляются</b>, они останутся в боте, "
        f"но без раздела (попадут в «Без раздела»).",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("cat:delconfirm:"), IsAdmin())
async def cb_cat_delconfirm(call: CallbackQuery):
    try:
        cid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    db.execute("UPDATE tests SET category_id=NULL WHERE category_id=?", (cid,))
    db.execute("DELETE FROM test_categories WHERE id=?", (cid,))
    await call.answer("✅ Раздел удалён", show_alert=True)
    # Возврат в меню разделов
    call.data = "adm:categories"
    from aiogram.fsm.context import FSMContext
    state = FSMContext(storage=None, key=None) if False else None
    # Просто перерисуем — заменим callback
    fake_state = type('S', (), {'clear': lambda self: None, 'set_state': lambda self, *a, **k: None})()
    # Проще — переотправим
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, id")
    text = "📂 <b>Разделы каталога</b>\n\n"
    if not cats:
        text += "<i>Разделов нет.</i>"
    else:
        for c in cats:
            cnt = db.fetchone(
                "SELECT COUNT(*) AS c FROM tests WHERE category_id=? AND status='active'",
                (c['id'],))['c']
            text += f"{c.get('emoji') or '📚'} <b>{utils.escape_html(c['name'])}</b> — {cnt} тестов\n"
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать раздел", callback_data="cat:create")
    kb.button(text="↩️ Назад", callback_data="m:admin")
    kb.adjust(1)
    try:
        await call.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


# ========= Каталог разделов для юзера =========

def get_categories() -> list[dict]:
    rows = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, id")
    return [dict(r) for r in rows]


def get_tests_in_category(category_id: int, language: str = None) -> list[dict]:
    if language:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE category_id=? AND status='active'
               AND COALESCE(is_private,0)=0 AND language=?
               ORDER BY id DESC""", (category_id, language))
    else:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE category_id=? AND status='active'
               AND COALESCE(is_private,0)=0
               ORDER BY id DESC""", (category_id,))
    return [dict(r) for r in rows]


def get_tests_without_category(language: str = None) -> list[dict]:
    if language:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE category_id IS NULL AND status='active'
               AND COALESCE(is_private,0)=0 AND language=?
               ORDER BY id DESC""", (language,))
    else:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE category_id IS NULL AND status='active'
               AND COALESCE(is_private,0)=0
               ORDER BY id DESC""")
    return [dict(r) for r in rows]
