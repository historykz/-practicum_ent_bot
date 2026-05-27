"""
Закрытый (приватный) доступ к тестам.

- /opens          — главное меню (только админ)
- Приватные тесты не видны в каталоге, поиске, дуэлях
- Видны и проходимы только тем, кому админ выдал доступ
"""
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, Message,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from filters import IsAdmin

router = Router(name="private_access")
log = logging.getLogger(__name__)


class PrivateAccessStates(StatesGroup):
    waiting_user_for_grant = State()
    waiting_days_for_grant = State()
    waiting_test_for_grant = State()
    waiting_user_for_revoke = State()


# ============ Public helpers (используются в других модулях) ============

def is_test_private(test: dict) -> bool:
    """Тест приватный?"""
    if not test:
        return False
    return bool(test.get('is_private'))


def user_has_private_access(test_id: int, user_tg_id: int) -> bool:
    """Есть ли у пользователя доступ к приватному тесту? Проверяет срок."""
    if not user_tg_id:
        return False
    if utils.is_admin(user_tg_id):
        return True
    row = db.fetchone(
        """SELECT expires_at FROM private_test_access
           WHERE test_id=? AND user_tg_id=?""",
        (test_id, user_tg_id))
    if not row:
        return False
    expires = dict(row).get('expires_at')
    if not expires:
        return True  # бессрочный
    from datetime import datetime
    try:
        exp_dt = datetime.fromisoformat(expires)
        return exp_dt > datetime.utcnow()
    except Exception:
        return True


def list_user_private_tests(user_tg_id: int) -> list[dict]:
    """Список приватных тестов, доступных конкретному пользователю (не истёкшие)."""
    if utils.is_admin(user_tg_id):
        rows = db.fetchall(
            "SELECT * FROM tests WHERE is_private=1 AND status='active' ORDER BY id DESC")
    else:
        from datetime import datetime
        now_iso = datetime.utcnow().isoformat()
        rows = db.fetchall(
            """SELECT t.* FROM tests t
               JOIN private_test_access p ON p.test_id = t.id
               WHERE t.is_private=1 AND t.status='active' AND p.user_tg_id=?
                 AND (p.expires_at IS NULL OR p.expires_at > ?)
               ORDER BY t.id DESC""",
            (user_tg_id, now_iso))
    return [dict(r) for r in rows]


# ============ /opens ============

@router.message(Command("opens"), IsAdmin())
async def cmd_opens(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Выдать доступ", callback_data="opens:grant")
    kb.button(text="📋 Список с доступом", callback_data="opens:list")
    kb.button(text="🗑 Отозвать доступ", callback_data="opens:revoke")
    kb.button(text="🔐 Приватные тесты", callback_data="opens:tests")
    kb.button(text="↩️ Закрыть", callback_data="opens:close")
    kb.adjust(1)

    text = (
        "🔐 <b>Закрытый доступ</b>\n\n"
        "Управление приватными тестами. Эти тесты <b>не видны</b> "
        "обычным пользователям, Premium-юзерам, и не используются в дуэлях.\n\n"
        "Доступ выдаётся <b>лично</b> по @username или tg_id.\n\n"
        "Чтобы сделать тест приватным — создайте новый тест и в админ-панели "
        "пометьте его как приватный, либо используйте раздел «🔐 Приватные тесты»."
    )
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.message(Command("opens"))
async def cmd_opens_denied(message: Message):
    await message.answer("⛔ Команда доступна только администраторам.")


@router.callback_query(F.data == "opens:close", IsAdmin())
async def cb_opens_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


# ============ Список приватных тестов ============

@router.callback_query(F.data == "opens:tests", IsAdmin())
async def cb_opens_tests(call: CallbackQuery, state: FSMContext):
    await state.clear()
    tests = db.fetchall(
        "SELECT id, title, subject FROM tests WHERE is_private=1 ORDER BY id DESC LIMIT 30")

    if not tests:
        text = ("🔐 <b>Приватные тесты</b>\n\n"
                "<i>Пока нет приватных тестов.</i>\n\n"
                "Чтобы сделать тест приватным — откройте его в админке "
                "(<code>/admin</code> → 📚 Мои тесты → выбрать → 🔐 Сделать приватным).")
    else:
        lines = ["🔐 <b>Приватные тесты:</b>\n"]
        for t in tests:
            users_count = db.fetchone(
                "SELECT COUNT(*) AS c FROM private_test_access WHERE test_id=?",
                (t['id'],))['c']
            title = (t['title'] or '—')[:40]
            lines.append(f"• #{t['id']} — {utils.escape_html(title)} "
                          f"<i>({users_count} с доступом)</i>")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Назад", callback_data="opens:back")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


# ============ Выдача доступа ============

@router.callback_query(F.data == "opens:grant", IsAdmin())
async def cb_opens_grant(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PrivateAccessStates.waiting_user_for_grant)
    await call.message.answer(
        "👤 Введите <b>@username</b> или <b>tg_id</b> пользователей.\n\n"
        "<b>Можно сразу до 15 человек</b> — через запятую, пробел или с новой строки:\n"
        "<code>@vasya, @petya 12345</code>\n"
        "или\n"
        "<code>@vasya\n@petya\n12345</code>",
        parse_mode="HTML")
    await call.answer()


def _parse_users_bulk(text: str) -> list[str]:
    """Парсит массив идентификаторов из строки. Поддерживает запятые, пробелы, переносы."""
    import re
    raw = re.split(r'[,\s\n]+', text or "")
    out = []
    for r in raw:
        r = r.strip()
        if not r:
            continue
        # Уберём @ если есть в начале
        if r.startswith("@"):
            out.append(r)
        elif r.isdigit():
            out.append(r)
        else:
            out.append(r)  # на случай ника без @
    # Уникальные, сохраняя порядок
    seen = set()
    result = []
    for x in out:
        key = x.lower().lstrip("@")
        if key in seen:
            continue
        seen.add(key)
        result.append(x)
    return result


@router.message(PrivateAccessStates.waiting_user_for_grant, IsAdmin())
async def s_opens_grant_user(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    parsed = _parse_users_bulk(raw)

    if not parsed:
        await message.answer("❌ Не распознал ни одного пользователя. Попробуйте ещё раз.")
        return

    if len(parsed) > 15:
        await message.answer(
            f"⚠️ Можно максимум 15 человек за раз. Вы прислали {len(parsed)}. "
            f"Отправьте список покороче.")
        return

    # Резолвим каждого
    found_users = []  # [(tg_id, display_name), ...]
    not_found = []
    for ident in parsed:
        target_tg_id = None
        target_name = None
        if ident.isdigit():
            target_tg_id = int(ident)
            u = db.fetchone(
                "SELECT tg_id, username, first_name FROM users WHERE tg_id=?",
                (target_tg_id,))
            if u:
                ud = dict(u)
                target_name = ("@" + ud['username']) if ud.get('username') else (
                    ud.get('first_name') or f"id{target_tg_id}")
            else:
                target_name = f"id{target_tg_id}"
        else:
            target = utils.find_user_by_arg(ident)
            if target:
                target_tg_id = target['tg_id']
                target_name = ("@" + target['username']) if target.get('username') else (
                    target.get('first_name') or f"id{target_tg_id}")
            else:
                not_found.append(ident)
                continue
        found_users.append((target_tg_id, target_name))

    if not found_users:
        await message.answer(
            "❌ Ни одного пользователя не нашёл.\n"
            "Те, кто вводил по @username, должны были писать /start боту. "
            "Или вводите tg_id числом.")
        return

    # Сохраняем в state
    await state.update_data(grant_targets=found_users, grant_not_found=not_found)

    # Спрашиваем срок
    kb = InlineKeyboardBuilder()
    for label, days in [("⏱ 7 дней", 7), ("📅 30 дней", 30),
                         ("🗓 90 дней", 90), ("♾ Бессрочно", 0)]:
        kb.button(text=label, callback_data=f"opensdays:{days}")
    kb.button(text="✏️ Ввести вручную", callback_data="opensdays:custom")
    kb.button(text="❌ Отмена", callback_data="opens:back")
    kb.adjust(2)

    summary = "\n".join(
        f"• <b>{utils.escape_html(name)}</b> (<code>{tg}</code>)"
        for tg, name in found_users[:15])
    extra = ""
    if not_found:
        extra = f"\n\n⚠️ Не нашёл: {', '.join(not_found[:5])}"

    await state.set_state(PrivateAccessStates.waiting_days_for_grant)
    await message.answer(
        f"👥 Распознано: <b>{len(found_users)}</b>\n\n"
        f"{summary}{extra}\n\n"
        f"⏱ <b>На какой срок выдать доступ?</b>",
        reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("opensdays:"), IsAdmin())
async def cb_opens_days(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":", 1)[1]
    if arg == "custom":
        await call.message.answer(
            "Введите количество дней числом (1–365) или <code>0</code> для бессрочного доступа:",
            parse_mode="HTML")
        await call.answer()
        return
    try:
        days = int(arg)
    except ValueError:
        await call.answer()
        return
    await _proceed_to_test_choice(call.message, state, days, edit=False)
    await call.answer()


@router.message(PrivateAccessStates.waiting_days_for_grant, IsAdmin())
async def s_opens_days_input(message: Message, state: FSMContext):
    """Ручной ввод дней."""
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("❌ Введите число от 0 до 365 (0 — бессрочно).")
        return
    days = int(txt)
    if days > 365:
        await message.answer("❌ Максимум 365 дней.")
        return
    await _proceed_to_test_choice(message, state, days, edit=False)


async def _proceed_to_test_choice(message_or_msg, state: FSMContext, days: int, edit: bool):
    """Сохраняем срок и показываем список приватных тестов."""
    await state.update_data(grant_days=days)
    tests = db.fetchall(
        "SELECT id, title FROM tests WHERE is_private=1 AND status='active' ORDER BY id DESC LIMIT 30")
    if not tests:
        await message_or_msg.answer(
            "⚠️ Нет ни одного приватного теста.\n"
            "Сначала создайте/пометьте тест как приватный.")
        await state.clear()
        return
    kb = InlineKeyboardBuilder()
    for t in tests:
        title = (t['title'] or '—')[:50]
        kb.button(text=f"🔐 {title}", callback_data=f"opensgrant:{t['id']}")
    kb.button(text="⚡️ Дать ВСЕ приватные сразу", callback_data="opensgrant:all")
    kb.button(text="❌ Отмена", callback_data="opens:back")
    kb.adjust(1)
    duration_label = "бессрочно" if days == 0 else f"{days} дней"
    await state.set_state(PrivateAccessStates.waiting_test_for_grant)
    await message_or_msg.answer(
        f"⏱ Срок: <b>{duration_label}</b>\n\n"
        f"Выберите тест для выдачи доступа:",
        reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("opensgrant:"), IsAdmin())
async def cb_opens_grant_test(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    targets = data.get('grant_targets') or []
    days = data.get('grant_days', 0)

    if not targets:
        await call.answer("❌ Сессия истекла, начните заново.", show_alert=True)
        await state.clear()
        return

    arg = call.data.split(":", 1)[1]

    if arg == "all":
        tests_to_grant = db.fetchall(
            "SELECT id, title FROM tests WHERE is_private=1 AND status='active'")
        tests_to_grant = [dict(t) for t in tests_to_grant]
    else:
        try:
            test_id = int(arg)
        except ValueError:
            await call.answer()
            return
        test = db.fetchone("SELECT * FROM tests WHERE id=?", (test_id,))
        if not test:
            await call.answer("Тест не найден.", show_alert=True)
            return
        tests_to_grant = [dict(test)]

    # Вычисляем expires_at
    expires_at = None
    if days > 0:
        from datetime import datetime, timedelta
        expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat()

    granted_count = 0
    notified_count = 0

    for tg_id, name in targets:
        for tst in tests_to_grant:
            try:
                # UPSERT — если запись есть, обновляем expires_at
                db.execute(
                    """INSERT INTO private_test_access
                          (test_id, user_tg_id, granted_by, expires_at, notified_expired)
                       VALUES (?,?,?,?,0)
                       ON CONFLICT(test_id, user_tg_id) DO UPDATE SET
                          expires_at=excluded.expires_at,
                          granted_by=excluded.granted_by,
                          notified_expired=0""",
                    (tst['id'], tg_id, call.from_user.id, expires_at))
                granted_count += 1
            except Exception as e:
                log.warning("Ошибка выдачи доступа tg=%s test=%s: %s", tg_id, tst['id'], e)
        try:
            await _notify_user_grant(
                call.bot, tg_id,
                tests_to_grant[0] if len(tests_to_grant) == 1 else None,
                all_tests=(arg == "all"),
                days=days)
            notified_count += 1
        except Exception:
            pass

    duration_label = "бессрочно" if days == 0 else f"{days} дней"
    summary = (
        f"✅ <b>Доступ выдан!</b>\n\n"
        f"👥 Пользователей: <b>{len(targets)}</b>\n"
        f"🔐 Тестов: <b>{len(tests_to_grant)}</b>\n"
        f"⏱ Срок: <b>{duration_label}</b>\n"
        f"📊 Записей: <b>{granted_count}</b>\n"
        f"📩 Уведомлено: <b>{notified_count}</b>"
    )
    await call.message.answer(summary, parse_mode="HTML")
    await state.clear()
    await call.answer("✅")


async def _notify_user_grant(bot: Bot, target_tg_id: int,
                              test: dict = None, all_tests: bool = False,
                              days: int = 0):
    """Уведомляем пользователя о новом доступе."""
    duration_label = "♾ Доступ бессрочный" if days == 0 else f"⏱ Срок: <b>{days} дней</b>"
    try:
        if all_tests:
            text = (
                f"🎉 <b>Вам открыт закрытый доступ!</b>\n\n"
                f"Администратор открыл вам доступ ко <b>всем приватным тестам</b>.\n\n"
                f"{duration_label}\n\n"
                f"Перейдите в каталог тестов."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📚 Открыть каталог", callback_data="m:tests")
            ]])
        else:
            title = utils.escape_html(test.get('title') or '—')
            qcount = db.fetchone(
                "SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                (test['id'],))['c']
            text = (
                f"🎉 <b>Вам открыт закрытый доступ!</b>\n\n"
                f"🔐 Тест: <b>{title}</b>\n"
                f"📚 Вопросов: {qcount}\n"
                f"⏱ Время: {test.get('time_per_question') or 30} сек/вопрос\n\n"
                f"{duration_label}\n\n"
                f"Этот тест не виден другим пользователям — "
                f"только тем, кому администратор лично открыл."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔓 Открыть тест",
                                      callback_data=f"opentest:{test['id']}")
            ]])
        await bot.send_message(target_tg_id, text,
                                reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось уведомить tg_id=%s о доступе: %s", target_tg_id, e)


# ============ Список юзеров с доступом ============

@router.callback_query(F.data == "opens:list", IsAdmin())
async def cb_opens_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_opens_list(call, page=1)


@router.callback_query(F.data.startswith("opens:list:"), IsAdmin())
async def cb_opens_list_page(call: CallbackQuery, state: FSMContext):
    try:
        page = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        page = 1
    await _render_opens_list(call, page=page)


async def _render_opens_list(call: CallbackQuery, page: int = 1):
    """Список юзеров с приватным доступом, пагинация по 20."""
    PER_PAGE = 20
    # Получаем всех юзеров (DISTINCT) с группировкой по их тестам
    all_rows = db.fetchall("""
        SELECT p.user_tg_id, p.granted_at, p.test_id, p.expires_at,
               u.username, u.first_name,
               t.title AS test_title
        FROM private_test_access p
        LEFT JOIN users u ON u.tg_id = p.user_tg_id
        LEFT JOIN tests t ON t.id = p.test_id
        ORDER BY p.granted_at DESC
    """)

    if not all_rows:
        text = "📋 <b>Список пользователей с приватным доступом</b>\n\n<i>Пока никому не выдавали.</i>"
        kb = InlineKeyboardBuilder()
        kb.button(text="↩️ Назад", callback_data="opens:back")
        kb.adjust(1)
        try:
            await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                          parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
        await call.answer()
        return

    # Группируем по юзеру
    from collections import defaultdict
    by_user = defaultdict(list)
    users = {}
    for r in all_rows:
        tg = r['user_tg_id']
        users[tg] = r
        by_user[tg].append(r['test_title'] or f"#{r['test_id']}")

    user_list = list(by_user.items())  # [(tg_id, [tests])]
    total_users = len(user_list)
    total_pages = max(1, (total_users + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    page_users = user_list[start:end]

    lines = [
        f"📋 <b>Пользователи с приватным доступом</b>",
        f"Всего: {total_users}    Страница {page}/{total_pages}\n"
    ]
    for tg, tests in page_users:
        u = users[tg]
        name = ("@" + u['username']) if u['username'] else (u['first_name'] or f"id{tg}")
        lines.append(f"<b>{utils.escape_html(name)}</b>  (<code>{tg}</code>)")
        # Показываем максимум 2 теста на пользователя
        for t_name in tests[:2]:
            lines.append(f"  • {utils.escape_html(t_name[:35])}")
        if len(tests) > 2:
            lines.append(f"  • <i>…ещё {len(tests) - 2}</i>")
        lines.append("")
    text = "\n".join(lines)

    # Кнопки пагинации
    kb = InlineKeyboardBuilder()
    nav_row = []
    if page > 1:
        kb.button(text="‹ Назад", callback_data=f"opens:list:{page-1}")
    kb.button(text=f"{page}/{total_pages}", callback_data="opens:noop")
    if page < total_pages:
        kb.button(text="Вперёд ›", callback_data=f"opens:list:{page+1}")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="↩️ Назад в меню", callback_data="opens:back"))

    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "opens:noop", IsAdmin())
async def cb_opens_noop(call: CallbackQuery):
    await call.answer()


# ============ Отзыв доступа ============

@router.callback_query(F.data == "opens:revoke", IsAdmin())
async def cb_opens_revoke(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PrivateAccessStates.waiting_user_for_revoke)
    await call.message.answer(
        "🗑 Введите <b>@username</b> или <b>tg_id</b> пользователей, "
        "у которых хотите забрать приватный доступ.\n\n"
        "<b>Можно сразу до 15 человек</b> через запятую, пробел или с новой строки:\n"
        "<code>@vasya, @petya 12345</code>",
        parse_mode="HTML")
    await call.answer()


@router.message(PrivateAccessStates.waiting_user_for_revoke, IsAdmin())
async def s_opens_revoke(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    parsed = _parse_users_bulk(raw)

    if not parsed:
        await message.answer("❌ Не распознал ни одного пользователя.")
        return

    if len(parsed) > 15:
        await message.answer(
            f"⚠️ Максимум 15 человек за раз. Вы прислали {len(parsed)}.")
        return

    # Резолвим
    resolved = []
    not_found = []
    for ident in parsed:
        tg_id = None
        if ident.isdigit():
            tg_id = int(ident)
        else:
            target = utils.find_user_by_arg(ident)
            if target:
                tg_id = target['tg_id']
        if tg_id:
            resolved.append(tg_id)
        else:
            not_found.append(ident)

    if not resolved:
        await message.answer("❌ Никого не нашёл.")
        return

    total_revoked = 0
    affected_users = 0
    for tg in resolved:
        before = db.fetchone(
            "SELECT COUNT(*) AS c FROM private_test_access WHERE user_tg_id=?",
            (tg,))['c']
        if before > 0:
            db.execute("DELETE FROM private_test_access WHERE user_tg_id=?", (tg,))
            total_revoked += before
            affected_users += 1
            # Уведомление
            try:
                await message.bot.send_message(
                    tg,
                    "ℹ️ Администратор отозвал у вас приватный доступ к тестам.")
            except Exception:
                pass

    extra = f"\n\n⚠️ Не нашёл: {', '.join(not_found[:5])}" if not_found else ""
    await message.answer(
        f"✅ <b>Доступ отозван</b>\n\n"
        f"👥 Пользователей затронуто: <b>{affected_users}</b>\n"
        f"🔐 Записей удалено: <b>{total_revoked}</b>{extra}",
        parse_mode="HTML")
    await state.clear()


# ============ Назад в меню ============

@router.callback_query(F.data == "opens:back", IsAdmin())
async def cb_opens_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Выдать доступ", callback_data="opens:grant")
    kb.button(text="📋 Список с доступом", callback_data="opens:list")
    kb.button(text="🗑 Отозвать доступ", callback_data="opens:revoke")
    kb.button(text="🔐 Приватные тесты", callback_data="opens:tests")
    kb.button(text="↩️ Закрыть", callback_data="opens:close")
    kb.adjust(1)
    text = (
        "🔐 <b>Закрытый доступ</b>\n\n"
        "Управление приватными тестами."
    )
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


# ============ Фоновая проверка истечения доступа ============

async def expiry_check_loop(bot: Bot):
    """
    Раз в час проверяет истёкшие приватные доступы.
    Уведомляет юзеров и помечает запись.
    """
    import asyncio
    from datetime import datetime
    while True:
        try:
            now = datetime.utcnow().isoformat()
            expired = db.fetchall(
                """SELECT p.id, p.user_tg_id, p.test_id, t.title
                   FROM private_test_access p
                   LEFT JOIN tests t ON t.id = p.test_id
                   WHERE p.expires_at IS NOT NULL
                     AND p.expires_at < ?
                     AND COALESCE(p.notified_expired, 0) = 0
                   LIMIT 200""", (now,))
            for r in expired:
                title = utils.escape_html(r['title'] or '—')
                try:
                    await bot.send_message(
                        r['user_tg_id'],
                        f"⏳ <b>Истёк срок приватного доступа</b>\n\n"
                        f"🔐 Тест: <b>{title}</b>\n\n"
                        f"Для продления — обратитесь к администратору.",
                        parse_mode="HTML")
                except Exception:
                    pass
                # Помечаем чтобы не уведомлять снова и удаляем запись
                try:
                    db.execute(
                        "DELETE FROM private_test_access WHERE id=?", (r['id'],))
                except Exception:
                    pass
        except Exception as e:
            log.warning("expiry_check_loop error: %s", e)
        await asyncio.sleep(3600)  # раз в час
