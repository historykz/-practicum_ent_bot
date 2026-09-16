"""
Хендлер резервного копирования.
Админка → 💾 Резервная копия:
  - Скачать резервную копию (ZIP)
  - Восстановить из файла (заменить / добавить)
  - Найти вопрос по серийному номеру
"""
import asyncio
import os
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (Message, CallbackQuery, FSInputFile,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
import utils
from filters import IsAdmin, IsOwner
from services import backup_service

router = Router(name="backup")
log = logging.getLogger(__name__)


class BackupStates(StatesGroup):
    waiting_file = State()
    waiting_findq = State()
    waiting_formula_txt = State()


def _menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬇️ Скачать резервную копию", callback_data="bkp:download")
    kb.button(text="♻️ Восстановить бэкап", callback_data="adm:restore")
    kb.button(text="📋 Журнал восстановлений", callback_data="rst:log")
    kb.button(text="🔍 Найти вопрос по номеру", callback_data="bkp:findq")
    kb.button(text="↩️ В админку", callback_data="m:admin")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "adm:maintenance", IsOwner())
async def cb_maintenance(call: CallbackQuery):
    row = db.fetchone("SELECT value FROM settings WHERE key='maintenance_mode'")
    is_on = row and str(row.get('value')) == '1'
    status = "🔴 ВКЛЮЧЕН (бот не работает для юзеров)" if is_on \
             else "🟢 ВЫКЛЮЧЕН (бот работает)"
    text = (
        "🔧 <b>Режим обслуживания</b>\n\n"
        f"Сейчас: {status}\n\n"
        "Когда включён — обычные пользователи получают сообщение "
        "«бот на обслуживании» и не могут проходить тесты. "
        "Админы работают как обычно."
    )
    kb = InlineKeyboardBuilder()
    if is_on:
        kb.button(text="🟢 Включить бота обратно", callback_data="adm:maint:off")
    else:
        kb.button(text="🔴 Приостановить бота", callback_data="adm:maint:on")
    kb.button(text="↩️ В админку", callback_data="m:admin")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, reply_markup=kb.as_markup(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup(),
                                    parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:maint:"), IsAdmin())
async def cb_maint_toggle(call: CallbackQuery):
    action = call.data.split(":")[2]
    val = '1' if action == 'on' else '0'
    db.execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES ('maintenance_mode',?)",
        (val,))
    await call.answer("🔴 Бот приостановлен" if val == '1'
                      else "🟢 Бот снова работает", show_alert=True)
    await cb_maintenance(call)


@router.callback_query(F.data == "adm:stats", IsOwner())
async def cb_stats(call: CallbackQuery):
    await call.answer()
    from services import stats_service
    try:
        text = stats_service.build_stats_text()
    except Exception as e:
        log.exception("stats: %s", e)
        text = f"⚠️ Ошибка статистики: {e}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Анализ сложности",
                              callback_data="adm:hardest")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:stats"),
         InlineKeyboardButton(text="↩️ В админку", callback_data="m:admin")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "adm:hardest", IsAdmin())
async def cb_hardest(call: CallbackQuery):
    """Показать анализ сложных тестов и вопросов."""
    await call.answer()
    from services import difficulty_service as _ds
    try:
        text = _ds.build_hardest_report('ru')
    except Exception as e:
        text = f"⚠️ Ошибка анализа: {e}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:hardest")],
        [InlineKeyboardButton(text="↩️ К статистике", callback_data="adm:stats")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "adm:backup", IsOwner())
async def cb_backup_menu(call: CallbackQuery):
    c = backup_service.backup_counts()
    text = (
        "💾 <b>Резервная копия / Восстановление</b>\n\n"
        f"Сейчас в базе:\n"
        f"• Разделов: <b>{c['categories']}</b>\n"
        f"• Тестов: <b>{c['tests']}</b>\n"
        f"• Вопросов: <b>{c['questions']}</b>\n"
        f"• Фото в вопросах: <b>{c['media']}</b>\n"
        f"• Пользователей: <b>{c.get('users', 0)}</b>\n"
        f"• Предметов на сайте: <b>{c.get('subjects', 0)}</b>\n"
        f"• Уроков с конспектами: <b>{c.get('lessons', 0)}</b>\n\n"
        "💾 «Скачать» пришлёт <b>ВСЁ сразу</b>: бота и сайт "
        "(предметы, уроки, конспекты, доступы, журнал просмотров).\n"
        "На сайт за второй частью заходить не нужно."
    )
    try:
        await call.message.edit_text(text, reply_markup=_menu_kb(),
                                       parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=_menu_kb(),
                                    parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "bkp:download", IsAdmin())
async def cb_download(call: CallbackQuery, bot: Bot):
    """Одна общая копия платформы, нарезанная на части ≤19 МБ.

    Раньше приходило два разных архива — бота и сайта — и при восстановлении
    их путали. Теперь бэкап один: в нём вся база (а значит и бот, и сайт)
    вместе с загруженными файлами. Если он не влезает в лимит Telegram,
    режем на части; для восстановления нужны все.
    """
    await call.answer()
    status = await call.message.answer("💾 Собираю полную копию платформы…")

    def _build():
        from webapp import learning as _lg
        from services import restore_service as _rs
        zip_path = _lg._build_backup_zip_sync()
        parts = _rs.split_backup(zip_path)
        return zip_path, parts

    try:
        zip_path, parts = await asyncio.to_thread(_build)
    except Exception as e:
        log.exception("backup create: %s", e)
        await status.edit_text(f"⚠️ Ошибка создания бэкапа: {e}")
        return

    from services import restore_service as rs
    total_size = os.path.getsize(zip_path)
    n = len(parts)
    try:
        await status.edit_text(
            f"📦 Копия готова: {rs.human_size(total_size)}"
            + (f", частей: {n}" if n > 1 else "") + ". Отправляю…")
    except Exception:
        pass

    for i, path in enumerate(parts, 1):
        size = os.path.getsize(path)
        if n == 1:
            cap = ("✅ <b>Полная резервная копия платформы</b>\n\n"
                   "Внутри всё сразу: предметы, разделы, уроки, конспекты, ДЗ, "
                   "тесты и вопросы, пользователи, доступы, подписки, платежи, "
                   "настройки и статистика — и сайта, и бота.\n\n"
                   "Восстановить: «♻️ Восстановить бэкап».")
        else:
            cap = (f"✅ <b>Резервная копия — часть {i} из {n}</b> "
                   f"({rs.human_size(size)})\n\n"
                   f"⚠️ Сохраните ВСЕ {n} частей — для восстановления нужны все.")
        try:
            await bot.send_document(call.message.chat.id, FSInputFile(path),
                                    caption=cap, parse_mode="HTML")
        except Exception as e:
            await call.message.answer(f"⚠️ Не смог отправить часть {i}: {e}")

    try:
        await status.edit_text(
            f"✅ <b>Готово.</b> Прислал {'файл' if n == 1 else f'{n} частей'} — "
            f"это полная копия платформы целиком, отдельно с сайта качать нечего.",
            parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data == "bkp:restore", IsAdmin())
async def cb_restore_ask(call: CallbackQuery, state: FSMContext):
    await state.set_state(BackupStates.waiting_file)
    await call.message.answer(
        "⬆️ <b>Восстановление</b>\n\n"
        "Пришли мне файл резервной копии (backup_*.zip).\n\n"
        "/cancel — отмена.", parse_mode="HTML")
    await call.answer()


@router.message(BackupStates.waiting_file, F.document, IsAdmin())
async def msg_restore_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not doc.file_name.endswith(".zip"):
        await message.answer("Нужен ZIP-файл бэкапа. Пришли backup_*.zip")
        return
    os.makedirs(backup_service.BACKUP_DIR, exist_ok=True)
    # Накапливаем части (можно прислать несколько файлов подряд)
    sd = await state.get_data()
    parts = sd.get('restore_parts', [])
    idx = len(parts) + 1
    local = os.path.join(backup_service.BACKUP_DIR,
                          f"restore_{message.from_user.id}_p{idx}.zip")
    try:
        tg_file = await bot.get_file(doc.file_id)
        await bot.download_file(tg_file.file_path, destination=local)
    except Exception as e:
        # Файл больше 20 МБ? Подскажем
        if "too big" in str(e).lower():
            await message.answer(
                "⚠️ Этот файл больше 20 МБ — Telegram не даёт его скачать.\n"
                "Используй НОВЫЙ бэкап (он разбит на части ≤19 МБ). "
                "Старые большие бэкапы восстановить нельзя.")
        else:
            await message.answer(f"⚠️ Не смог скачать файл: {e}")
        return

    parts.append(local)
    await state.update_data(restore_parts=parts)

    # Проверяем — есть ли среди частей backup.json
    import zipfile, json
    has_main = False
    n_cat = n_test = n_q = n_media = 0
    for pp in parts:
        try:
            zf = zipfile.ZipFile(pp)
            if "backup.json" in zf.namelist():
                has_main = True
                data = json.loads(zf.read("backup.json").decode("utf-8"))
                tbl = data.get("tables", {})
                n_cat = len(tbl.get("test_categories", []))
                n_test = len(tbl.get("tests", []))
                n_q = len(tbl.get("questions", []))
                n_media = len(data.get("media_map", {}))
            zf.close()
        except Exception:
            pass

    if not has_main:
        await message.answer(
            f"📥 Получена часть {len(parts)}. Это часть без основных данных.\n"
            f"Пришли часть с данными (обычно part1), или все части по очереди.")
        return

    # Сколько картинок собрано из присланных частей
    collected_media = 0
    for pp in parts:
        try:
            zf = zipfile.ZipFile(pp)
            collected_media += sum(1 for n in zf.namelist() if n.startswith("media/"))
            zf.close()
        except Exception:
            pass

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Заменить всё", callback_data="bkp:mode:replace")
    kb.button(text="➕ Добавить к существующим", callback_data="bkp:mode:append")
    kb.button(text="📎 Прислать ещё часть", callback_data="bkp:morepart")
    kb.button(text="❌ Отмена", callback_data="bkp:cancel")
    kb.adjust(1)
    media_note = ""
    if n_media > collected_media:
        media_note = (f"\n\n⚠️ Картинок в бэкапе: {n_media}, прислано: "
                      f"{collected_media}. Пришли остальные части чтобы "
                      f"картинки восстановились полностью, либо восстанавливай "
                      f"как есть.")
    await message.answer(
        f"⚠️ <b>Восстановление</b> (частей: {len(parts)})\n\n"
        f"В бэкапе:\n"
        f"• Разделов: <b>{n_cat}</b>\n"
        f"• Тестов: <b>{n_test}</b>\n"
        f"• Вопросов: <b>{n_q}</b>\n"
        f"• Картинок: <b>{n_media}</b> (собрано: {collected_media})\n"
        f"{media_note}\n\n"
        f"Как восстановить?",
        reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "bkp:morepart", IsAdmin())
async def cb_more_part(call: CallbackQuery, state: FSMContext):
    await state.set_state(BackupStates.waiting_file)
    await call.message.answer("📎 Пришли следующую часть бэкапа (.zip)")
    await call.answer()


@router.callback_query(F.data == "bkp:cancel", IsAdmin())
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Восстановление отменено.")
    await call.answer()


@router.callback_query(F.data.startswith("bkp:mode:"), IsAdmin())
async def cb_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[2]
    await state.update_data(restore_mode=mode)
    label = "🔄 ЗАМЕНИТЬ ВСЁ (текущее удалится)" if mode == "replace" \
            else "➕ Добавить к существующим"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, восстановить", callback_data="bkp:confirm")
    kb.button(text="❌ Отмена", callback_data="bkp:cancel")
    kb.adjust(1)
    await call.message.edit_text(
        f"⚠️ <b>Подтверждение</b>\n\n"
        f"Режим: <b>{label}</b>\n\n"
        f"{'Текущие тесты будут УДАЛЕНЫ и заменены данными из файла.' if mode=='replace' else 'Данные из файла добавятся к текущим.'}\n\n"
        f"Точно продолжить?",
        reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "bkp:confirm", IsAdmin())
async def cb_confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    # Поддержка частей (новый формат) и одиночного файла (старый)
    parts = data.get("restore_parts")
    path = parts if parts else data.get("restore_path")
    mode = data.get("restore_mode", "replace")
    await state.clear()
    if not path:
        await call.answer("Файл не найден, начни заново.", show_alert=True)
        return
    await call.answer()
    prog = await call.message.edit_text("♻️ Восстанавливаю… подожди.")
    # Передаём chat для заливки медиа + сообщение для прогресса
    backup_service.restore_backup._admin_chat = call.message.chat.id
    try:
        backup_service.restore_backup._progress_msg_id = prog.message_id
    except Exception:
        backup_service.restore_backup._progress_msg_id = call.message.message_id
    try:
        report = await backup_service.restore_backup(bot, path, mode=mode)
    except Exception as e:
        log.exception("restore: %s", e)
        await call.message.answer(f"⚠️ Ошибка восстановления: {e}")
        return

    lines = [
        "✅ <b>Восстановление завершено!</b>\n",
        "📊 <b>Отчёт:</b>",
        f"• Разделов: {report['categories']} ✅",
        f"• Тестов: {report['tests']} ✅",
        f"• Вопросов: {report['questions']} ✅",
        f"• Вариантов ответов: {report['options']} ✅",
        f"• Медиафайлов: {report['media']}" +
            (f" (не удалось: {report['media_failed']}) ⚠️" if report['media_failed'] else " ✅"),
        (f"   📷 В бэкапе заявлено: {report.get('media_in_map', '?')}, "
         f"файлов в архиве: {report.get('media_files_in_zip', '?')}, "
         f"залито: {report['media']}"),
        f"• Доступов: {report['access']} ✅",
        f"• Премиум-доступов: {report.get('premium', 0)} ✅",
        f"• Пользователей: {report.get('users', 0)} ✅",
        f"• Результатов тестов: {report.get('test_attempts', 0)} ✅",
    ]
    if report['errors']:
        lines.append(f"\n⚠️ <b>Проблемы ({len(report['errors'])}):</b>")
        for err in report['errors'][:10]:
            lines.append(f"• {err}")
        if len(report['errors']) > 10:
            lines.append(f"…и ещё {len(report['errors'])-10}")
    else:
        lines.append("\n🎉 Без ошибок!")
    await call.message.answer("\n".join(lines), parse_mode="HTML")


# ===================== ПОИСК ВОПРОСА =====================

@router.callback_query(F.data == "bkp:findq", IsAdmin())
async def cb_findq_ask(call: CallbackQuery, state: FSMContext):
    await state.set_state(BackupStates.waiting_findq)
    await call.message.answer(
        "🔍 <b>Поиск вопроса</b>\n\n"
        "Введи серийный номер: <code>Q-1247</code> или просто <code>1247</code>.\n\n"
        "/cancel — отмена.", parse_mode="HTML")
    await call.answer()


@router.message(BackupStates.waiting_findq, IsAdmin())
async def msg_findq(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/cancel'):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    await state.clear()
    from services import appeal_service
    q = appeal_service.find_question_by_serial(message.text.strip())
    if not q:
        await message.answer(
            f"⚠️ Вопрос <code>{utils.escape_html(message.text.strip())}</code> не найден.",
            parse_mode="HTML")
        return
    # Используем карточку из appeals
    from handlers.appeals import _show_question_card
    await _show_question_card(message, q)


# ===================== ГЕНЕРАТОР КАРТИНОК ФОРМУЛ =====================

_LATEX_HELP = (
    "🖼 <b>Генератор картинок формул</b>\n\n"
    "Пришли <b>.txt файл</b>, где каждая строка — одна формула в LaTeX.\n\n"
    "<b>Шпаргалка LaTeX:</b>\n"
    "• Дробь: <code>\\frac{1}{2}</code>\n"
    "• Корень: <code>\\sqrt{16}</code>\n"
    "• Степень: <code>x^2</code>\n"
    "• Индекс: <code>x_1</code>\n"
    "• Умножить: <code>\\times</code>\n"
    "• Деление: <code>\\div</code>\n"
    "• ±: <code>\\pm</code>\n"
    "• ≤ ≥: <code>\\leq</code> <code>\\geq</code>\n"
    "• Сумма: <code>\\sum</code>, Интеграл: <code>\\int</code>\n\n"
    "<b>Пример файла:</b>\n"
    "<code>\\frac{3}{4} \\times \\sqrt{16}\n"
    "x^2 + 5x - 6 = 0\n"
    "\\frac{x+1}{x-1} = 5</code>\n\n"
    "Можно добавить подпись через <code>|</code>:\n"
    "<code>\\frac{1}{2} | задача 1</code>\n\n"
    "/cancel — отмена."
)


@router.callback_query(F.data == "adm:formulas", IsOwner())
async def cb_formulas(call: CallbackQuery, state: FSMContext):
    await state.set_state(BackupStates.waiting_formula_txt)
    await call.message.answer(_LATEX_HELP, parse_mode="HTML")
    await call.answer()


@router.message(BackupStates.waiting_formula_txt, F.document, IsAdmin())
async def msg_formula_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or '').endswith(('.txt', '.tex')):
        await message.answer("Нужен .txt файл с формулами (по одной на строку).")
        return
    # Скачиваем
    import io as _io
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = _io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        text = buf.getvalue().decode('utf-8', errors='ignore')
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать файл: {e}")
        return

    from services import formula_service
    formulas = formula_service.parse_formulas_txt(text)
    if not formulas:
        await message.answer("В файле нет формул. Каждая строка — одна формула.")
        return

    await state.clear()
    status = await message.answer(
        f"🖼 Генерирую картинки… ({len(formulas)} формул)")
    try:
        zip_path, ok, failed, paths = formula_service.generate_zip(formulas)
    except Exception as e:
        await status.edit_text(f"⚠️ Ошибка генерации: {e}")
        return
    try:
        await status.delete()
    except Exception:
        pass

    # Отправляем ZIP
    try:
        await bot.send_document(
            message.chat.id, FSInputFile(zip_path),
            caption=(
                f"✅ Готово! Картинок: {ok}" +
                (f", ошибок: {len(failed)}" if failed else "") +
                "\n\nПрикрепи нужную картинку к вопросу через "
                "редактор → «🖼 Добавить фото»."),
            parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Не смог отправить ZIP: {e}")

    if failed:
        errs = "\n".join(f"• строка {i}: {tex[:40]}" for i, tex in failed[:10])
        await message.answer(
            f"⚠️ Не отрисовались (проверь синтаксис LaTeX):\n{errs}")
