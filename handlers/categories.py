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
from filters import IsAdmin, IsOwner

router = Router(name="categories")
log = logging.getLogger(__name__)


class CategoryStates(StatesGroup):
    waiting_name = State()
    waiting_user = State()      # кому выдать доступ к приватному разделу
    waiting_rename = State()    # новое название раздела


# ========= Меню разделов (админ) =========

@router.callback_query(F.data == "adm:categories", IsOwner())
async def cb_adm_categories(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, id")

    from services import category_service as cs
    text = ("📂 <b>Разделы каталога</b>\n\n"
            "Разделы = предметы. ⭐️ обязательный (виден всем), "
            "🎓 профильный (ученик выбирает сам), "
            "🔒 приватный (в выборе профильных его не видно).\n\n")
    if not cats:
        text += "<i>Пока нет ни одного раздела.</i>\n\nНажмите ➕ чтобы создать первый."
    else:
        text += "<b>Существующие:</b>\n"
        for c in cats:
            cnt = db.fetchone(
                "SELECT COUNT(*) AS c FROM tests WHERE category_id=? AND status='active' AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0",
                (c['id'],))['c']
            mark = "⭐️" if c.get('is_required') else "🎓"
            lock = "🔒 " if cs.is_private(dict(c)) else ""
            chosen = cs.chosen_count(c['id'])
            text += (f"{mark} {lock}{c.get('emoji') or '📚'} "
                     f"<b>{utils.escape_html(c['name'])}</b> — "
                     f"{cnt} тестов, выбрали {chosen}\n")

    # Тесты, не привязанные ни к одному разделу, — тоже нужно уметь смотреть
    # и приватизировать здесь же, а не только в отдельном разделе «Мои тесты».
    no_cat = db.fetchone(
        "SELECT COUNT(*) AS c FROM tests WHERE category_id IS NULL AND status='active' "
        "AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0")['c']

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать раздел", callback_data="cat:create")
    if cats:
        for c in cats[:20]:
            mark = "⭐️" if c.get('is_required') else "🎓"
            lock = "🔒" if cs.is_private(dict(c)) else ""
            kb.button(text=f"{mark}{lock} {c['name'][:26]}",
                      callback_data=f"cat:open:{c['id']}")
    if no_cat:
        text += f"\n📭 Без раздела — {no_cat} тестов\n"
        kb.button(text=f"📭 Без раздела ({no_cat})", callback_data="cat:tests:none:0")
    kb.button(text="↩️ Назад", callback_data="m:admin")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("cat:open:"), IsAdmin())
async def cb_cat_open(call: CallbackQuery, state: FSMContext = None):
    """Карточка раздела с управлением.

    Сюда же ведёт кнопка «Отмена» из переименования, поэтому снимаем режим
    ожидания ввода: иначе следующее сообщение админа бот принял бы за новое
    название раздела.
    """
    if state:
        await state.clear()
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    c = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
    if not c:
        await call.answer("Не найден.", show_alert=True)
        return
    from services import category_service as cs
    c = dict(c)
    is_req = bool(c.get('is_required'))
    private = cs.is_private(c)
    st = cs.stats(cat_id)
    type_label = "⭐️ Обязательный (виден всем)" if is_req else "🎓 Профильный (выбирается)"
    priv_label = ("🔒 Приватный — в списке профильных его не видно"
                  if private else "🌍 Открытый — виден всем при выборе")

    text = (f"📂 <b>{c.get('emoji') or '📚'} {utils.escape_html(c['name'])}</b>\n\n"
            f"Тип: {type_label}\n"
            f"Доступ: {priv_label}\n\n"
            f"📝 Тестов: <b>{st['tests']}</b> (активных {st['tests_active']})\n"
            f"❓ Вопросов: <b>{st['questions']}</b>\n"
            f"👥 Выбрали профильным: <b>{st['chosen']}</b>")
    if private:
        text += f"\n🔑 Выдан доступ: <b>{st['access']}</b>"

    kb = InlineKeyboardBuilder()
    if private:
        kb.button(text="🌍 Сделать открытым", callback_data=f"cat:priv:{cat_id}:0")
        kb.button(text="🔑 Кому открыт доступ", callback_data=f"cat:acc:{cat_id}:0")
        kb.button(text="➕ Выдать доступ", callback_data=f"cat:grant:{cat_id}")
    else:
        kb.button(text="🔒 Сделать приватным", callback_data=f"cat:priv:{cat_id}:1")
    kb.button(text=f"👥 Кто выбрал ({st['chosen']})", callback_data=f"cat:users:{cat_id}:0")
    kb.button(text=f"📝 Тесты раздела ({st['tests']})", callback_data=f"cat:tests:{cat_id}:0")
    if is_req:
        kb.button(text="🎓 Сделать профильным", callback_data=f"cat:req:{cat_id}:0")
    else:
        kb.button(text="⭐️ Сделать обязательным", callback_data=f"cat:req:{cat_id}:1")
    kb.button(text="✏️ Переименовать", callback_data=f"cat:rename:{cat_id}")
    kb.button(text="⬆️ Выше", callback_data=f"cat:move:{cat_id}:up")
    kb.button(text="⬇️ Ниже", callback_data=f"cat:move:{cat_id}:down")
    kb.button(text="🗑 Удалить раздел", callback_data=f"cat:del:{cat_id}")
    kb.button(text="↩️ К разделам", callback_data="adm:categories")
    kb.adjust(1, 1, 1, 1, 1, 1, 2, 1, 1)
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
    # Заодно убираем выданные доступы и выбор у учеников — иначе от удалённого
    # раздела остаются записи, которые уже никому ничего не открывают.
    try:
        from services import category_service as _cs
        _cs.remove_choice_all(cid)
        db.execute("DELETE FROM category_access WHERE category_id=?", (cid,))
    except Exception:
        pass
    await call.answer("✅ Раздел удалён", show_alert=True)
    # Возврат в меню разделов: объекты aiogram неизменяемы, поэтому просто
    # перерисовываем список здесь же.
    cats = db.fetchall("SELECT * FROM test_categories ORDER BY sort_order, id")
    text = "📂 <b>Разделы каталога</b>\n\n"
    if not cats:
        text += "<i>Разделов нет.</i>"
    else:
        for c in cats:
            cnt = db.fetchone(
                "SELECT COUNT(*) AS c FROM tests WHERE category_id=? AND status='active' AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0",
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
    # Показываем только тесты где есть хотя бы 1 вопрос
    q_filter = "AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0"
    if language:
        rows = db.fetchall(
            f"""SELECT * FROM tests WHERE category_id=? AND status='active'
               AND COALESCE(is_private,0)=0 AND language=? {q_filter}
               ORDER BY id DESC""", (category_id, language))
    else:
        rows = db.fetchall(
            f"""SELECT * FROM tests WHERE category_id=? AND status='active'
               AND COALESCE(is_private,0)=0 {q_filter}
               ORDER BY id DESC""", (category_id,))
    return [dict(r) for r in rows]


def get_tests_without_category(language: str = None) -> list[dict]:
    q_filter = "AND (SELECT COUNT(*) FROM questions WHERE test_id=tests.id) > 0"
    if language:
        rows = db.fetchall(
            f"""SELECT * FROM tests WHERE category_id IS NULL AND status='active'
               AND COALESCE(is_private,0)=0 AND language=? {q_filter}
               ORDER BY id DESC""", (language,))
    else:
        rows = db.fetchall(
            f"""SELECT * FROM tests WHERE category_id IS NULL AND status='active'
               AND COALESCE(is_private,0)=0 {q_filter}
               ORDER BY id DESC""")
    return [dict(r) for r in rows]


# ========= Приватность, участники и доступы =========

PAGE = 8            # сколько человек показываем на одном экране

def _with_data(call: CallbackQuery, data: str):
    """Перерисовать другой экран тем же обработчиком.

    У aiogram 3 объекты событий неизменяемы (frozen), поэтому call.data
    присвоить нельзя — подсовываем лёгкую подмену, как это уже сделано
    в переключателе «обязательный/профильный».
    """
    return type('F', (), {'data': data, 'message': call.message,
                          'from_user': call.from_user, 'bot': call.bot,
                          'answer': call.answer})()




def _who(u: dict) -> str:
    """Как показать человека в списке: имя, @ник и id."""
    name = (u.get("first_name") or "").strip()
    uname = (u.get("username") or "").strip()
    parts = []
    if name:
        parts.append(utils.escape_html(name[:26]))
    if uname:
        parts.append(f"@{utils.escape_html(uname[:26])}")
    if not parts:
        parts.append("без имени")
    return f"{' · '.join(parts)} (<code>{u['tg_id']}</code>)"


@router.callback_query(F.data.startswith("cat:priv:"), IsAdmin())
async def cb_cat_private(call: CallbackQuery):
    """Сделать раздел приватным или снова открытым."""
    from services import category_service as cs
    try:
        _, _, raw_id, raw_val = call.data.split(":")
        cat_id, val = int(raw_id), raw_val == "1"
    except (ValueError, IndexError):
        await call.answer()
        return
    cs.set_private(cat_id, val)
    if val:
        await call.answer("Раздел скрыт из выбора профильных.", show_alert=True)
    else:
        await call.answer("Раздел снова виден всем.", show_alert=True)
    await cb_cat_open(_with_data(call, f"cat:open:{cat_id}"))


@router.callback_query(F.data.startswith("cat:users:"), IsAdmin())
async def cb_cat_users(call: CallbackQuery, state: FSMContext = None):
    """Кто выбрал этот раздел профильным. Страницами, с удалением.

    Сюда ведёт «Отмена» из выдачи доступа — снимаем ожидание ввода, чтобы
    следующее сообщение админа не было понято как ник ученика.
    """
    if state:
        await state.clear()
    from services import category_service as cs
    try:
        parts = call.data.split(":")
        cat_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await call.answer()
        return

    cat = cs.get(cat_id)
    if not cat:
        await call.answer("Раздел не найден.", show_alert=True)
        return

    users = cs.chosen_by(cat_id)
    total = len(users)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = users[page * PAGE:(page + 1) * PAGE]

    head = (f"👥 <b>Выбрали «{utils.escape_html(cat['name'])}»</b>\n\n"
            f"Всего: <b>{total}</b>")
    if not total:
        head += "\n\nПока никто не выбрал этот раздел профильным."
    else:
        head += f"  ·  страница {page + 1} из {pages}\n\n"
        head += "\n".join(f"{page * PAGE + i + 1}. {_who(u)}"
                          for i, u in enumerate(chunk))
        head += "\n\n<i>Нажмите на человека, чтобы убрать у него этот предмет.</i>"

    kb = InlineKeyboardBuilder()
    for u in chunk:
        label = (u.get("first_name") or u.get("username") or str(u["tg_id"]))[:24]
        kb.button(text=f"❌ {label}",
                  callback_data=f"cat:unpick:{cat_id}:{u['tg_id']}:{page}")
    if pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=f"cat:users:{cat_id}:{page - 1}")
        if page < pages - 1:
            kb.button(text="➡️", callback_data=f"cat:users:{cat_id}:{page + 1}")
    if total:
        kb.button(text="🧹 Убрать у всех", callback_data=f"cat:unpickall:{cat_id}")
    kb.button(text="↩️ К разделу", callback_data=f"cat:open:{cat_id}")
    kb.adjust(1)

    try:
        await call.message.edit_text(head, parse_mode="HTML",
                                     reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("cat:unpick:"), IsAdmin())
async def cb_cat_unpick(call: CallbackQuery):
    """Убрать предмет у одного человека."""
    from services import category_service as cs
    try:
        _, _, raw_cat, raw_user, raw_page = call.data.split(":")
        cat_id, tg_id, page = int(raw_cat), int(raw_user), int(raw_page)
    except (ValueError, IndexError):
        await call.answer()
        return
    ok = cs.remove_choice(cat_id, tg_id)
    await call.answer("Убрали у этого пользователя." if ok else "Уже не выбран.")
    await cb_cat_users(_with_data(call, f"cat:users:{cat_id}:{page}"))


@router.callback_query(F.data.startswith("cat:unpickall:"), IsAdmin())
async def cb_cat_unpick_all(call: CallbackQuery):
    """Снять предмет у всех — спрашиваем подтверждение."""
    from services import category_service as cs
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    cat = cs.get(cat_id)
    n = cs.chosen_count(cat_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Да, убрать у {n}",
              callback_data=f"cat:unpickok:{cat_id}")
    kb.button(text="❌ Отмена", callback_data=f"cat:users:{cat_id}:0")
    kb.adjust(1)
    await call.message.edit_text(
        f"🧹 <b>Убрать «{utils.escape_html(cat['name'] if cat else '')}» "
        f"у всех?</b>\n\n"
        f"Предмет перестанет быть профильным у <b>{n}</b> человек. "
        f"Их результаты и доступы к тестам останутся на месте — "
        f"меняется только выбор предметов.\n\n"
        f"Каждый сможет выбрать предмет заново сам, если раздел открытый.",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("cat:unpickok:"), IsAdmin())
async def cb_cat_unpick_all_ok(call: CallbackQuery):
    from services import category_service as cs
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    n = cs.remove_choice_all(cat_id)
    await call.answer(f"Убрали у {n} человек.", show_alert=True)
    await cb_cat_open(_with_data(call, f"cat:open:{cat_id}"))


@router.callback_query(F.data.startswith("cat:acc:"), IsAdmin())
async def cb_cat_access(call: CallbackQuery):
    """Кому выдан доступ к приватному разделу."""
    from services import category_service as cs
    try:
        parts = call.data.split(":")
        cat_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await call.answer()
        return

    cat = cs.get(cat_id)
    rows = cs.access_list(cat_id)
    total = len(rows)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE:(page + 1) * PAGE]

    text = (f"🔑 <b>Доступ к «{utils.escape_html(cat['name'] if cat else '')}»</b>\n\n"
            f"Всего: <b>{total}</b>")
    if not total:
        text += ("\n\nПока никому. Нажмите «Выдать доступ» и пришлите "
                 "@ник или ID человека.")
    else:
        text += f"  ·  страница {page + 1} из {pages}\n\n"
        text += "\n".join(f"{page * PAGE + i + 1}. {_who(u)}"
                          for i, u in enumerate(chunk))
        text += "\n\n<i>Нажмите на человека, чтобы забрать доступ.</i>"

    kb = InlineKeyboardBuilder()
    for u in chunk:
        label = (u.get("first_name") or u.get("username") or str(u["tg_id"]))[:24]
        kb.button(text=f"❌ {label}",
                  callback_data=f"cat:revoke:{cat_id}:{u['tg_id']}:{page}")
    if pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=f"cat:acc:{cat_id}:{page - 1}")
        if page < pages - 1:
            kb.button(text="➡️", callback_data=f"cat:acc:{cat_id}:{page + 1}")
    kb.button(text="➕ Выдать доступ", callback_data=f"cat:grant:{cat_id}")
    if total:
        kb.button(text="🧹 Забрать у всех", callback_data=f"cat:revokeall:{cat_id}")
    kb.button(text="↩️ К разделу", callback_data=f"cat:open:{cat_id}")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("cat:revoke:"), IsAdmin())
async def cb_cat_revoke(call: CallbackQuery):
    from services import category_service as cs
    try:
        _, _, raw_cat, raw_user, raw_page = call.data.split(":")
        cat_id, tg_id, page = int(raw_cat), int(raw_user), int(raw_page)
    except (ValueError, IndexError):
        await call.answer()
        return
    cs.revoke(cat_id, tg_id)
    await call.answer("Доступ забрали.")
    await cb_cat_access(_with_data(call, f"cat:acc:{cat_id}:{page}"))


@router.callback_query(F.data.startswith("cat:revokeall:"), IsAdmin())
async def cb_cat_revoke_all(call: CallbackQuery):
    from services import category_service as cs
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    for u in cs.access_list(cat_id):
        cs.revoke(cat_id, u["tg_id"])
    await call.answer("Доступ забрали у всех.", show_alert=True)
    await cb_cat_access(_with_data(call, f"cat:acc:{cat_id}:0"))


@router.callback_query(F.data.startswith("cat:grant:"), IsAdmin())
async def cb_cat_grant(call: CallbackQuery, state: FSMContext):
    """Просим прислать @ник или ID человека."""
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    await state.update_data(grant_cat=cat_id)
    await state.set_state(CategoryStates.waiting_user)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"cat:open:{cat_id}")
    await call.message.edit_text(
        "➕ <b>Кому выдать доступ?</b>\n\n"
        "Пришлите <b>@ник</b> или числовой <b>ID</b> человека.\n"
        "Можно сразу несколько — через запятую или с новой строки.\n\n"
        "<i>Человек должен был хотя бы раз запустить бота — иначе его "
        "нет в базе.</i>",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await call.answer()


@router.message(CategoryStates.waiting_user, IsAdmin())
async def msg_cat_grant(message: Message, state: FSMContext):
    from services import category_service as cs
    data = await state.get_data()
    cat_id = data.get("grant_cat")
    await state.clear()
    if not cat_id:
        return

    raw = (message.text or "").replace("\n", ",")
    added, missing = [], []
    for chunk in raw.split(","):
        token = chunk.strip().lstrip("@")
        if not token:
            continue
        if token.isdigit():
            user = db.fetchone("SELECT tg_id, username, first_name FROM users "
                               "WHERE tg_id=?", (int(token),))
        else:
            user = db.fetchone("SELECT tg_id, username, first_name FROM users "
                               "WHERE LOWER(username)=LOWER(?)", (token,))
        if not user:
            missing.append(token)
            continue
        cs.grant(cat_id, user["tg_id"], message.from_user.id)
        added.append(_who(dict(user)))

    lines = []
    if added:
        lines.append("✅ <b>Доступ выдан:</b>\n" + "\n".join(added))
    if missing:
        lines.append("⚠️ <b>Не нашёл в базе:</b> " + ", ".join(missing)
                     + "\n<i>Проверьте ник или попросите человека запустить бота.</i>")
    if not lines:
        lines.append("Ничего не разобрал. Пришлите @ник или ID.")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Список доступов", callback_data=f"cat:acc:{cat_id}:0")
    kb.button(text="↩️ К разделу", callback_data=f"cat:open:{cat_id}")
    kb.adjust(1)
    await message.answer("\n\n".join(lines), parse_mode="HTML",
                         reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("cat:rename:"), IsAdmin())
async def cb_cat_rename(call: CallbackQuery, state: FSMContext):
    from services import category_service as cs
    try:
        cat_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    cat = cs.get(cat_id)
    await state.update_data(rename_cat=cat_id)
    await state.set_state(CategoryStates.waiting_rename)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"cat:open:{cat_id}")
    await call.message.edit_text(
        f"✏️ <b>Новое название</b>\n\n"
        f"Сейчас: {cat.get('emoji') or '📚'} "
        f"{utils.escape_html(cat['name'] if cat else '')}\n\n"
        f"Пришлите новое название. Можно вместе со значком, например:\n"
        f"<code>🧬 Биология</code>",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await call.answer()


@router.message(CategoryStates.waiting_rename, IsAdmin())
async def msg_cat_rename(message: Message, state: FSMContext):
    from services import category_service as cs
    data = await state.get_data()
    cat_id = data.get("rename_cat")
    await state.clear()
    if not cat_id:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое название — оставил как было.")
        return
    emoji = None
    parts = text.split(" ", 1)
    if len(parts) == 2 and not parts[0].isalnum() and len(parts[0]) <= 4:
        emoji, text = parts[0], parts[1].strip()
    ok, msg = cs.rename(cat_id, text, emoji)
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К разделу", callback_data=f"cat:open:{cat_id}")
    if ok:
        answer = (f"✅ Теперь раздел называется: "
                  f"{emoji or ''} {utils.escape_html(text)}")
    else:
        answer = f"⚠️ {utils.escape_html(msg)}\nНазвание оставил прежним."
    await message.answer(answer, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("cat:move:"), IsAdmin())
async def cb_cat_move(call: CallbackQuery):
    from services import category_service as cs
    try:
        _, _, raw_id, direction = call.data.split(":")
        cat_id = int(raw_id)
    except (ValueError, IndexError):
        await call.answer()
        return
    cs.move(cat_id, direction)
    await call.answer("Порядок изменён.")
    await cb_cat_open(_with_data(call, f"cat:open:{cat_id}"))


# ========= Тесты раздела: приватность галочками =========
#
# Раньше приватность отдельного теста можно было включить только по одному —
# открыть карточку теста в «Мои тесты» и переключить там. Здесь — тот же
# признак (tests.is_private), но сразу списком по всему разделу: тапнул —
# галочка встала и тест стал приватным, тапнул ещё раз — снял.

def _mine_only(tg_id: int) -> bool:
    """Админ уровня 1 видит и трогает только свои тесты — как в «Мои тесты»
    (handlers/admin.py). Владелец и уровень 2+ видят всё."""
    return utils.admin_level(tg_id) < 2


def _owns_test(tg_id: int, test_id: int) -> bool:
    if not _mine_only(tg_id):
        return True
    user = db.fetchone("SELECT id FROM users WHERE tg_id=?", (tg_id,))
    row = db.fetchone("SELECT created_by FROM tests WHERE id=?", (test_id,))
    return bool(user and row and row["created_by"] == user["id"])


def _tests_bucket(arg: str, tg_id: int = None):
    """Список тестов раздела (или тестов без раздела) + название для шапки.

    Админ уровня 1 видит здесь только СВОИ тесты — так же, как в «Мои
    тесты» (handlers/admin.py): без этого он мог бы массово менять
    приватность тестов, созданных другими админами.
    """
    mine = tg_id is not None and _mine_only(tg_id)
    own = " AND t.created_by = (SELECT id FROM users WHERE tg_id=?)" if mine else ""
    own_p = (tg_id,) if mine else ()
    if arg == "none":
        rows = db.fetchall(
            f"SELECT t.id, t.title, t.is_private, t.is_paid FROM tests t "
            f"WHERE t.category_id IS NULL AND t.status='active'{own} "
            f"ORDER BY t.id DESC", own_p)
        title = "📭 Без раздела"
    else:
        try:
            cat_id = int(arg)
        except ValueError:
            return "", []
        cat = db.fetchone("SELECT * FROM test_categories WHERE id=?", (cat_id,))
        rows = db.fetchall(
            f"SELECT t.id, t.title, t.is_private, t.is_paid FROM tests t "
            f"WHERE t.category_id=? AND t.status='active'{own} "
            f"ORDER BY t.id DESC", (cat_id, *own_p))
        title = f"{cat.get('emoji') or '📚'} {cat['name']}" if cat else "Раздел"
    return title, [dict(r) for r in rows]


@router.callback_query(F.data.startswith("cat:tests:"), IsAdmin())
async def cb_cat_tests_list(call: CallbackQuery):
    parts = call.data.split(":")
    arg = parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    title, tests = _tests_bucket(arg, call.from_user.id)
    if not tests:
        await call.answer("В этом разделе пока нет тестов.", show_alert=True)
        return

    total = len(tests)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = tests[page * PAGE:(page + 1) * PAGE]
    priv_count = sum(1 for t in tests if t.get("is_private"))

    text = (f"📝 <b>Тесты — {utils.escape_html(title)}</b>\n\n"
            f"Всего: <b>{total}</b> · приватных: <b>{priv_count}</b>"
            f"  ·  страница {page + 1} из {pages}\n\n"
            f"Тапните тест, чтобы поставить или снять 🔒 — так же, как галочку.")

    kb = InlineKeyboardBuilder()
    for t in chunk:
        mark = "🔒" if t.get("is_private") else "▫️"
        tag = "💎" if t.get("is_paid") else ""
        kb.button(text=f"{mark} {tag}{t['title'][:42]}",
                  callback_data=f"cat:tpriv:{t['id']}:{arg}:{page}")
    if pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=f"cat:tests:{arg}:{page - 1}")
        if page < pages - 1:
            kb.button(text="➡️", callback_data=f"cat:tests:{arg}:{page + 1}")
    kb.button(text="🔒 Приватизировать все", callback_data=f"cat:tprivall:{arg}:1")
    kb.button(text="🌍 Открыть все", callback_data=f"cat:tprivall:{arg}:0")
    back = f"cat:open:{arg}" if arg != "none" else "adm:categories"
    kb.button(text="↩️ Назад", callback_data=back)
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("cat:tpriv:"), IsAdmin())
async def cb_cat_test_toggle(call: CallbackQuery):
    """Тап по тесту в списке — включить/выключить его приватность."""
    parts = call.data.split(":")
    try:
        tid = int(parts[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    arg = parts[3] if len(parts) > 3 else "none"
    page = parts[4] if len(parts) > 4 else "0"
    row = db.fetchone("SELECT is_private FROM tests WHERE id=?", (tid,))
    if not row:
        await call.answer("Тест не найден.", show_alert=True)
        return
    # Проверяем владельца здесь же, а не только в списке — список можно
    # обойти прямым вызовом (например через другой Telegram-клиент), а
    # менять чужие тесты уровню 1 нельзя, как и в «Мои тесты».
    if not _owns_test(call.from_user.id, tid):
        await call.answer("Это не ваш тест — менять его нельзя.", show_alert=True)
        return
    new_value = 0 if row["is_private"] else 1
    db.execute("UPDATE tests SET is_private=? WHERE id=?", (new_value, tid))
    await call.answer("🔒 Стал приватным" if new_value else "🌍 Снова открыт")
    await cb_cat_tests_list(_with_data(call, f"cat:tests:{arg}:{page}"))


@router.callback_query(F.data.startswith("cat:tprivall:"), IsAdmin())
async def cb_cat_tests_bulk(call: CallbackQuery):
    """Приватизировать или открыть сразу все тесты раздела — когда нужно
    срочно закрыть весь раздел целиком, не тыкая по одному."""
    parts = call.data.split(":")
    arg = parts[2]
    try:
        new_value = int(parts[3])
    except (ValueError, IndexError):
        await call.answer()
        return
    # Тот же список, что видит админ, — уровень 1 массово меняет только
    # свои тесты, чужие в выборку не попадут (см. _tests_bucket).
    _, tests = _tests_bucket(arg, call.from_user.id)
    if not tests:
        await call.answer("Пусто.", show_alert=True)
        return
    ids = [t["id"] for t in tests]
    placeholders = ",".join("?" * len(ids))
    db.execute(f"UPDATE tests SET is_private=? WHERE id IN ({placeholders})",
               (new_value, *ids))
    await call.answer(f"Готово: {len(ids)} тестов "
                      f"{'приватизировано' if new_value else 'открыто'}.",
                      show_alert=True)
    await cb_cat_tests_list(_with_data(call, f"cat:tests:{arg}:0"))
