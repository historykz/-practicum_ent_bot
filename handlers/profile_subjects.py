"""
Выбор профильных предметов (после языка) + смена в профиле.

Логика:
- Профильные предметы = НЕ обязательные разделы (категории).
- Строго 2 предмета.
- В каталоге юзер видит: обязательные разделы + свои профильные.
"""
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils

router = Router(name="profile_subjects")
log = logging.getLogger(__name__)

REQUIRED_COUNT = 2  # строго 2 профильных


def _resolve_lang(tg_id: int) -> str:
    u = db.fetchone("SELECT language FROM users WHERE tg_id=?", (tg_id,))
    return (u.get('language') if u else 'ru') or 'ru'


def get_optional_categories() -> list:
    """Категории-предметы которые можно выбрать (НЕ обязательные)."""
    return db.fetchall(
        "SELECT * FROM test_categories WHERE COALESCE(is_required,0)=0 "
        "ORDER BY sort_order, id")


def get_required_categories() -> list:
    """Обязательные категории — видны всем."""
    return db.fetchall(
        "SELECT * FROM test_categories WHERE COALESCE(is_required,0)=1 "
        "ORDER BY sort_order, id")


def _subjects_text(lang: str, selected_count: int, from_profile: bool) -> str:
    if lang == "kz":
        base = (
            "🎓 <b>Бейіндік пәндерді таңда</b>\n\n"
            "ҰБТ-да сен 2 бейіндік пән тапсырасың "
            "(міндетті Қазақстан тарихы мен Математикалық "
            "сауаттылықтан бөлек).\n\n"
            "Дәл <b>2</b> пәнді белгіле 👇\n\n"
            "❓ <b>Пәнің тізімде жоқ па?</b> «Басқа» таңда — "
            "барлық тесттер ашылады. «Басқа» 2 пәнге саналады.")
    else:
        base = (
            "🎓 <b>Выбери профильные предметы</b>\n\n"
            "На ЕНТ ты сдаёшь 2 профильных предмета "
            "(помимо обязательных История Казахстана и "
            "Математическая грамотность).\n\n"
            "Отметь ровно <b>2</b> галочками 👇\n\n"
            "❓ <b>Нет твоего предмета?</b> Выбери «Другое» — "
            "это откроет тебе ВСЕ доступные тесты. "
            "«Другое» считается за 2 предмета.")
    base += f"\n\n✅ {'Таңдалды' if lang == 'kz' else 'Выбрано'}: " \
            f"<b>{selected_count}/{REQUIRED_COUNT}</b>"
    return base


def _selected_weight(selected: set) -> int:
    """Вес выбора: 'other' = 2, обычный предмет = 1."""
    w = 0
    for s in selected:
        w += 2 if s == 'other' else 1
    return w


def _subjects_kb(selected: set, lang: str, from_profile: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    cats = get_optional_categories()
    has_other = 'other' in selected
    for c in cats:
        mark = "✅" if c['id'] in selected else "▫️"
        emoji = c.get('emoji') or '📚'
        kb.button(text=f"{mark} {emoji} {c['name']}",
                  callback_data=f"psub:tog:{c['id']}")
    # Кнопка «Другое»
    omark = "✅" if has_other else "▫️"
    kb.button(text=f"{omark} ❓ Другое (предмета нет в списке)",
              callback_data="psub:tog:other")
    # Кнопка продолжить — когда вес == REQUIRED_COUNT (2)
    if _selected_weight(selected) == REQUIRED_COUNT:
        if from_profile:
            kb.button(text="💾 Сохранить", callback_data="psub:save")
        else:
            kb.button(text="🚀 Начать тестирование", callback_data="psub:save")
    kb.adjust(1)
    return kb.as_markup()


async def show_subjects_screen(call: CallbackQuery, state: FSMContext,
                                from_profile: bool = False):
    """Показать экран выбора профильных."""
    lang = _resolve_lang(call.from_user.id)
    cats = get_optional_categories()
    if not cats:
        # Нет профильных категорий — пропускаем сразу в меню
        await _go_to_menu(call, lang)
        return
    # Текущие выбранные (при смене из профиля — подставим уже выбранные)
    data = await state.get_data()
    selected = set(data.get('psub_selected') or [])
    if from_profile and not selected:
        selected = set(utils.get_profile_subjects(call.from_user.id))
        await state.update_data(psub_selected=list(selected))
    await state.update_data(psub_from_profile=from_profile)

    text = _subjects_text(lang, len(selected), from_profile)
    kb = _subjects_kb(selected, lang, from_profile)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("psub:tog:"))
async def cb_toggle_subject(call: CallbackQuery, state: FSMContext):
    raw = call.data.split(":")[2]
    # 'other' или число
    item = 'other' if raw == 'other' else None
    if item is None:
        try:
            item = int(raw)
        except ValueError:
            await call.answer()
            return
    data = await state.get_data()
    selected = set(data.get('psub_selected') or [])
    lang = _resolve_lang(call.from_user.id)

    if item in selected:
        selected.discard(item)
    else:
        # Вес: other=2, обычный=1. Лимит = REQUIRED_COUNT (2)
        add_weight = 2 if item == 'other' else 1
        if _selected_weight(selected) + add_weight > REQUIRED_COUNT:
            await call.answer(
                "⚠️ Можно выбрать только 2 предмета (или «Другое» = 2).\n"
                "Сначала сними галочку." if lang == "ru"
                else "⚠️ Тек 2 пән (немесе «Басқа» = 2).\n"
                     "Алдымен біреуін алып таста.",
                show_alert=True)
            return
        # other нельзя совмещать с другими (он и так =2)
        if item == 'other':
            selected = {'other'}
        else:
            selected.discard('other')
            selected.add(item)
    await state.update_data(psub_selected=list(selected))

    from_profile = data.get('psub_from_profile', False)
    text = _subjects_text(lang, _selected_weight(selected), from_profile)
    kb = _subjects_kb(selected, lang, from_profile)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "psub:save")
async def cb_save_subjects(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get('psub_selected') or [])
    lang = _resolve_lang(call.from_user.id)
    if _selected_weight(set(selected)) != REQUIRED_COUNT:
        await call.answer("Нужно выбрать 2 предмета (или «Другое»).",
                          show_alert=True)
        return
    # Сохраняем (включая 'other' как строку)
    utils.set_profile_subjects(call.from_user.id, selected)
    from_profile = data.get('psub_from_profile', False)
    await state.update_data(psub_selected=None, psub_from_profile=None)

    # Покажем что выбрано
    names = []
    for cid in selected:
        if cid == 'other':
            names.append("❓ Другое (все тесты)")
            continue
        c = db.fetchone("SELECT name, emoji FROM test_categories WHERE id=?", (cid,))
        if c:
            names.append(f"{c.get('emoji') or '📚'} {c['name']}")
    chosen = ", ".join(names)

    if from_profile:
        msg = (f"✅ Профильные предметы обновлены:\n<b>{chosen}</b>" if lang == "ru"
               else f"✅ Бейіндік пәндер жаңартылды:\n<b>{chosen}</b>")
        await call.answer("✅ Сохранено")
        try:
            await call.message.edit_text(msg, parse_mode="HTML")
        except Exception:
            pass
        # Вернём в профиль
        await _go_to_menu(call, lang)
    else:
        await call.answer("✅")
        await _go_to_menu(call, lang)


async def _go_to_menu(call: CallbackQuery, lang: str):
    """Перейти в главное меню."""
    from locales import t
    from keyboards import main_menu_kb
    try:
        await call.message.answer(
            t("main_menu", lang),
            reply_markup=main_menu_kb(lang, utils.is_admin(call.from_user.id)),
            parse_mode="HTML")
    except Exception as e:
        log.warning("go_to_menu: %s", e)


# Точка входа из профиля — «Сменить профильные предметы»
@router.callback_query(F.data == "m:change_subjects")
async def cb_change_subjects(call: CallbackQuery, state: FSMContext):
    await state.update_data(psub_selected=None)
    await show_subjects_screen(call, state, from_profile=True)
    await call.answer()
