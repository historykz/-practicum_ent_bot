"""
Конспекты и ДЗ прямо в боте: предмет → раздел → урок.

Админ выбирает предмет (те же предметы, что на сайте), внутри — раздел,
внутри — урок. У урока можно добавить/заменить/удалить конспект, прислав
фото пачкой (сжатыми или файлами), и загрузить домашнее задание тестом
из ZIP, TXT или обычным текстом.

Страницы конспекта хранятся в Telegram по file_id — на сервере они места
не занимают. Всё, что делается здесь, сразу видно на сайте: база одна.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
import utils
from filters import IsAdmin
from services import notes_storage as ns

router = Router(name="notes_admin")
log = logging.getLogger(__name__)

PER_PAGE = 8


class NotesStates(StatesGroup):
    waiting_pages = State()       # приём фото конспекта
    waiting_lesson_title = State()
    waiting_section_title = State()
    waiting_hw_text = State()
    waiting_hw_file = State()
    waiting_rename = State()


# ===================== вспомогательное =====================

def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _subjects():
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM subjects "
        "ORDER BY COALESCE(is_pinned,0) DESC, sort_order, id")]


def _sections(subject_id):
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM sections WHERE subject_id=? ORDER BY sort_order, id",
        (subject_id,))]


def _lessons(section_id):
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM lessons WHERE section_id=? ORDER BY sort_order, id",
        (section_id,))]


def _lesson(lesson_id):
    row = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    return dict(row) if row else None


def _mode_icon(subject: dict) -> str:
    from webapp import shortcuts as sc
    return {sc.OPEN: "🌍", sc.PREMIUM: "💎", sc.PRIVATE: "🔒"}.get(
        sc.subject_mode(subject), "🔑")


async def _show(call_or_msg, text: str, kb: InlineKeyboardMarkup):
    """Перерисовать экран: правим сообщение, если не выходит — шлём новое."""
    if isinstance(call_or_msg, CallbackQuery):
        try:
            await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            await call_or_msg.message.answer(text, reply_markup=kb, parse_mode="HTML")
            return
    await call_or_msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ===================== экран 1: предметы =====================

def _subjects_screen():
    subs = _subjects()
    rows = []
    for s in subs:
        cnt = db.fetchone(
            "SELECT COUNT(*) AS c FROM lessons l JOIN sections sec ON sec.id=l.section_id "
            "WHERE sec.subject_id=?", (s["id"],))["c"]
        rows.append([_btn(f"{_mode_icon(s)} {s['title']} · {cnt} ур.", f"na:sub:{s['id']}")])
    rows.append([_btn("🔄 Обновить", "na:home")])
    text = ("📚 <b>Конспекты и ДЗ</b>\n\n"
            "Выберите предмет — откроются его разделы и уроки.\n"
            "Всё, что измените здесь, сразу появится на сайте.")
    if not subs:
        text += "\n\n<i>Предметов пока нет — создайте первый на сайте.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.in_({"/conspects", "/notes", "/konspekt"}), IsAdmin())
async def cmd_notes(message: Message, state: FSMContext):
    await state.clear()
    text, kb = _subjects_screen()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "na:home", IsAdmin())
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = _subjects_screen()
    await _show(call, text, kb)
    await call.answer()


# ===================== экран 2: разделы =====================

def _sections_screen(subject_id):
    subj = db.fetchone("SELECT * FROM subjects WHERE id=?", (subject_id,))
    if not subj:
        return "Предмет не найден", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("⬅️ К предметам", "na:home")]])
    subj = dict(subj)
    rows = []
    for sec in _sections(subject_id):
        n = db.fetchone("SELECT COUNT(*) AS c FROM lessons WHERE section_id=?",
                        (sec["id"],))["c"]
        rows.append([_btn(f"📂 {sec['title']} · {n} ур.", f"na:sec:{sec['id']}")])
    rows.append([_btn("➕ Новый раздел", f"na:newsec:{subject_id}")])
    rows.append([_btn("⬅️ К предметам", "na:home")])
    text = (f"{_mode_icon(subj)} <b>{utils.escape_html(subj['title'])}</b>\n\n"
            "Выберите раздел:")
    if not _sections(subject_id):
        text += "\n\n<i>Разделов пока нет — создайте первый кнопкой ниже.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("na:sub:"), IsAdmin())
async def cb_subject(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(call.data.split(":")[2])
    text, kb = _sections_screen(sid)
    await _show(call, text, kb)
    await call.answer()


# ===================== экран 3: уроки =====================

def _lessons_screen(section_id):
    sec = db.fetchone("SELECT * FROM sections WHERE id=?", (section_id,))
    if not sec:
        return "Раздел не найден", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("⬅️ К предметам", "na:home")]])
    rows = []
    for l in _lessons(section_id):
        pages = ns.page_count(l["id"])
        has_text = bool((l["content_html"] or "").strip())
        mark = "📖" if (pages or has_text) else "⬜️"
        money = "💎" if l["is_paid"] else "🆓"
        lock = "" if l["status"] == "open" else " 🔒"
        rows.append([_btn(f"{mark}{money} {l['title'][:34]}{lock}", f"na:les:{l['id']}")])
    rows.append([_btn("➕ Новый урок", f"na:newles:{section_id}")])
    rows.append([_btn("⬅️ К разделам", f"na:sub:{sec['subject_id']}")])
    text = (f"📂 <b>{utils.escape_html(sec['title'])}</b>\n\n"
            "📖 — конспект есть · ⬜️ — пусто\n"
            "💎 — платный · 🆓 — бесплатный · 🔒 — закрыт ученикам\n\n"
            "Выберите урок:")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("na:sec:"), IsAdmin())
async def cb_section(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sec_id = int(call.data.split(":")[2])
    text, kb = _lessons_screen(sec_id)
    await _show(call, text, kb)
    await call.answer()


# ===================== экран 4: урок =====================

def _lesson_screen(lesson_id):
    l = _lesson(lesson_id)
    if not l:
        return "Урок не найден", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("⬅️ К предметам", "na:home")]])
    st = ns.storage_summary(lesson_id)
    has_text = bool((l["content_html"] or "").strip())
    qcount = 0
    if l["test_id"]:
        qcount = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                             (l["test_id"],))["c"]

    lines = [f"📘 <b>{utils.escape_html(l['title'])}</b>", ""]
    if st["total"]:
        where = []
        if st["telegram"]:
            where.append(f"{st['telegram']} в Telegram")
        if st["disk"]:
            where.append(f"{st['disk']} на сервере")
        lines.append(f"📄 Страниц конспекта: <b>{st['total']}</b> ({', '.join(where)})")
    else:
        lines.append("📄 Конспект: <i>пусто</i>")
    if has_text:
        lines.append("📝 Есть текстовая часть конспекта")
    lines.append(f"📝 ДЗ (тест): <b>{qcount}</b> вопрос(ов)" if qcount
                 else "📝 ДЗ (тест): <i>нет</i>")
    lines.append(f"{'💎 Платный' if l['is_paid'] else '🆓 Бесплатный'} · "
                 f"{'👁 Открыт ученикам' if l['status'] == 'open' else '🔒 Закрыт'}")

    rows = [
        [_btn("📷 Добавить страницы конспекта", f"na:add:{lesson_id}")],
        [_btn("🔁 Заменить конспект", f"na:replace:{lesson_id}"),
         _btn("🗑 Удалить конспект", f"na:clear:{lesson_id}")],
        [_btn("👁 Посмотреть конспект", f"na:view:{lesson_id}")],
        [_btn("📝 Загрузить ДЗ", f"na:hw:{lesson_id}")],
        [_btn("💎 Сделать платным" if not l["is_paid"] else "🆓 Сделать бесплатным",
              f"na:paid:{lesson_id}"),
         _btn("🔒 Закрыть" if l["status"] == "open" else "👁 Открыть",
              f"na:open:{lesson_id}")],
        [_btn("✏️ Переименовать", f"na:rename:{lesson_id}")],
        [_btn("⬅️ К урокам", f"na:sec:{l['section_id']}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("na:les:"), IsAdmin())
async def cb_lesson(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lid = int(call.data.split(":")[2])
    text, kb = _lesson_screen(lid)
    await _show(call, text, kb)
    await call.answer()


# ===================== приём страниц конспекта =====================

def _receiving_kb(lesson_id, added):
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"✅ Готово ({added} стр.)", f"na:done:{lesson_id}")],
        [_btn("❌ Отменить приём", f"na:les:{lesson_id}")],
    ])


async def _start_receiving(call: CallbackQuery, state: FSMContext,
                           lesson_id: int, replace: bool):
    l = _lesson(lesson_id)
    if not l:
        await call.answer("Урок не найден", show_alert=True)
        return
    removed = 0
    if replace:
        removed = await _thread(ns.clear, lesson_id)
    await state.set_state(NotesStates.waiting_pages)
    await state.update_data(lesson_id=lesson_id, added=0)
    head = f"🔁 Старые страницы удалены ({removed}).\n\n" if replace else ""
    await _show(call,
                f"{head}📷 <b>Присылайте страницы конспекта</b>\n\n"
                f"Урок: {utils.escape_html(l['title'])}\n\n"
                "• Можно пачкой — хоть 30 фото за раз\n"
                "• Сжатыми фото или файлами (документом) — как удобно\n"
                "• Порядок сохранится ровно такой, в каком отправите\n"
                "• Файлы лягут в Telegram — место на сервере не тратится\n\n"
                "Когда закончите — нажмите «Готово».",
                _receiving_kb(lesson_id, 0))
    await call.answer()


@router.callback_query(F.data.startswith("na:add:"), IsAdmin())
async def cb_add_pages(call: CallbackQuery, state: FSMContext):
    await _start_receiving(call, state, int(call.data.split(":")[2]), replace=False)


@router.callback_query(F.data.startswith("na:replace:"), IsAdmin())
async def cb_replace_pages(call: CallbackQuery, state: FSMContext):
    await _start_receiving(call, state, int(call.data.split(":")[2]), replace=True)


async def _thread(fn, *a):
    import asyncio
    return await asyncio.to_thread(fn, *a)


@router.message(NotesStates.waiting_pages, F.photo, IsAdmin())
async def msg_page_photo(message: Message, state: FSMContext):
    """Сжатое фото. Берём самый большой размер — качество максимальное."""
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        return
    ph = message.photo[-1]
    await _thread(ns.add_telegram_page, lesson_id, ph.file_id, ph.file_unique_id,
                  message.message_id, False, "", message.from_user.id)
    await _bump(message, state, lesson_id)


@router.message(NotesStates.waiting_pages, F.document, IsAdmin())
async def msg_page_doc(message: Message, state: FSMContext):
    """Фото файлом (без сжатия) — качество оригинала."""
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        return
    doc = message.document
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    is_img = mime.startswith("image/") or name.endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"))
    if not is_img:
        await message.answer(
            "⚠️ Это не изображение. Для конспекта пришлите фото или картинку файлом.\n"
            "Домашнее задание загружается отдельной кнопкой «📝 Загрузить ДЗ».")
        return
    await _thread(ns.add_telegram_page, lesson_id, doc.file_id, doc.file_unique_id,
                  message.message_id, True, doc.file_name or "", message.from_user.id)
    await _bump(message, state, lesson_id)


async def _bump(message: Message, state: FSMContext, lesson_id: int):
    """Счётчик принятых страниц. Обновляем не чаще, чем раз в несколько
    файлов — иначе Telegram упрётся в лимит на правки сообщений."""
    data = await state.get_data()
    added = (data.get("added") or 0) + 1
    await state.update_data(added=added)
    if added <= 3 or added % 5 == 0:
        try:
            await message.answer(f"✅ Принято страниц: <b>{added}</b>",
                                 reply_markup=_receiving_kb(lesson_id, added),
                                 parse_mode="HTML")
        except Exception:
            pass


@router.message(NotesStates.waiting_pages, IsAdmin())
async def msg_page_other(message: Message):
    await message.answer(
        "Жду фото или картинку файлом. Когда закончите — нажмите «Готово» "
        "в сообщении выше.")


@router.callback_query(F.data.startswith("na:done:"), IsAdmin())
async def cb_done_pages(call: CallbackQuery, state: FSMContext):
    lesson_id = int(call.data.split(":")[2])
    await state.clear()
    total = await _thread(ns.renumber, lesson_id)
    text, kb = _lesson_screen(lesson_id)
    await _show(call, f"✅ Конспект сохранён: {total} стр.\n\n" + text, kb)
    await call.answer("Сохранено")


# ===================== просмотр и очистка =====================

@router.callback_query(F.data.startswith("na:view:"), IsAdmin())
async def cb_view(call: CallbackQuery, bot: Bot):
    lesson_id = int(call.data.split(":")[2])
    rows = await _thread(ns.pages, lesson_id)
    if not rows:
        await call.answer("Конспект пуст", show_alert=True)
        return
    await call.answer(f"Отправляю {len(rows)} стр.")
    from pathlib import Path
    import config
    from aiogram.types import FSInputFile
    root = Path(config.DB_PATH).resolve().parent
    sent = 0
    for r in rows:
        try:
            if (r.get("storage") or "disk") == ns.TG:
                await bot.send_photo(call.message.chat.id, r["file_id"],
                                     caption=f"стр. {r['sort_order']}")
            else:
                fp = root / (r.get("image_path") or "").lstrip("/")
                if fp.exists():
                    await bot.send_photo(call.message.chat.id, FSInputFile(str(fp)),
                                         caption=f"стр. {r['sort_order']}")
            sent += 1
        except Exception as e:
            log.warning("view page %s: %s", r["id"], e)
    text, kb = _lesson_screen(lesson_id)
    await call.message.answer(f"👁 Показано страниц: {sent}\n\n" + text,
                              reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("na:clear:"), IsAdmin())
async def cb_clear_ask(call: CallbackQuery):
    lesson_id = int(call.data.split(":")[2])
    n = ns.page_count(lesson_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"🗑 Да, удалить {n} стр.", f"na:clearyes:{lesson_id}")],
        [_btn("⬅️ Отмена", f"na:les:{lesson_id}")],
    ])
    await _show(call, f"🗑 Удалить конспект целиком ({n} стр.)?\n\n"
                      "Тест урока и настройки останутся.", kb)
    await call.answer()


@router.callback_query(F.data.startswith("na:clearyes:"), IsAdmin())
async def cb_clear_yes(call: CallbackQuery):
    lesson_id = int(call.data.split(":")[2])
    removed = await _thread(ns.clear, lesson_id)
    await _thread(db.execute, "UPDATE lessons SET content_html='' WHERE id=?", (lesson_id,))
    text, kb = _lesson_screen(lesson_id)
    await _show(call, f"🗑 Удалено страниц: {removed}\n\n" + text, kb)
    await call.answer("Конспект удалён")


# ===================== платность, видимость, переименование =====================

@router.callback_query(F.data.startswith("na:paid:"), IsAdmin())
async def cb_toggle_paid(call: CallbackQuery):
    lesson_id = int(call.data.split(":")[2])
    l = _lesson(lesson_id)
    if not l:
        await call.answer("Урок не найден", show_alert=True)
        return
    now_paid = bool(l["is_paid"])
    await _thread(db.execute,
                  "UPDATE lessons SET is_paid=?, free_override=? WHERE id=?",
                  (0 if now_paid else 1, 1 if now_paid else 0, lesson_id))
    text, kb = _lesson_screen(lesson_id)
    await _show(call, text, kb)
    await call.answer("Теперь бесплатный 🆓" if now_paid else "Теперь платный 💎")


@router.callback_query(F.data.startswith("na:open:"), IsAdmin())
async def cb_toggle_open(call: CallbackQuery):
    lesson_id = int(call.data.split(":")[2])
    l = _lesson(lesson_id)
    if not l:
        await call.answer("Урок не найден", show_alert=True)
        return
    new_status = "closed" if l["status"] == "open" else "open"
    await _thread(db.execute, "UPDATE lessons SET status=? WHERE id=?",
                  (new_status, lesson_id))
    text, kb = _lesson_screen(lesson_id)
    await _show(call, text, kb)
    await call.answer("Открыт ученикам" if new_status == "open" else "Закрыт")


@router.callback_query(F.data.startswith("na:rename:"), IsAdmin())
async def cb_rename_ask(call: CallbackQuery, state: FSMContext):
    lesson_id = int(call.data.split(":")[2])
    await state.set_state(NotesStates.waiting_rename)
    await state.update_data(lesson_id=lesson_id)
    await _show(call, "✏️ Пришлите новое название урока.\n\n/cancel — отмена",
                InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", f"na:les:{lesson_id}")]]))
    await call.answer()


@router.message(NotesStates.waiting_rename, IsAdmin())
async def msg_rename(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or title.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    lesson_id = data["lesson_id"]
    await _thread(db.execute, "UPDATE lessons SET title=? WHERE id=?", (title[:200], lesson_id))
    await state.clear()
    text, kb = _lesson_screen(lesson_id)
    await message.answer("✏️ Переименовано.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ===================== создание раздела и урока =====================

@router.callback_query(F.data.startswith("na:newsec:"), IsAdmin())
async def cb_new_section(call: CallbackQuery, state: FSMContext):
    subject_id = int(call.data.split(":")[2])
    await state.set_state(NotesStates.waiting_section_title)
    await state.update_data(subject_id=subject_id)
    await _show(call, "➕ Пришлите название нового раздела.\n\n/cancel — отмена",
                InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", f"na:sub:{subject_id}")]]))
    await call.answer()


@router.message(NotesStates.waiting_section_title, IsAdmin())
async def msg_new_section(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or title.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    subject_id = data["subject_id"]

    def _create():
        db.execute(
            "INSERT INTO sections (subject_id, title, sort_order) VALUES (?,?,"
            "COALESCE((SELECT MAX(sort_order)+1 FROM sections WHERE subject_id=?),0))",
            (subject_id, title[:200], subject_id))
    await _thread(_create)
    await state.clear()
    text, kb = _sections_screen(subject_id)
    await message.answer("✅ Раздел создан.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("na:newles:"), IsAdmin())
async def cb_new_lesson(call: CallbackQuery, state: FSMContext):
    section_id = int(call.data.split(":")[2])
    await state.set_state(NotesStates.waiting_lesson_title)
    await state.update_data(section_id=section_id)
    await _show(call, "➕ Пришлите название нового урока.\n\n"
                      "Он создастся <b>закрытым</b> — ученики его не увидят, "
                      "пока вы сами не откроете.\n\n/cancel — отмена",
                InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", f"na:sec:{section_id}")]]))
    await call.answer()


@router.message(NotesStates.waiting_lesson_title, IsAdmin())
async def msg_new_lesson(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or title.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    section_id = data["section_id"]

    def _create():
        # Новый урок закрыт: ученики не увидят его до публикации админом.
        # В премиум-предмете он сразу платный.
        from webapp import shortcuts as sc
        subj = db.fetchone(
            "SELECT s.* FROM subjects s JOIN sections sec ON sec.subject_id=s.id "
            "WHERE sec.id=?", (section_id,))
        paid = 1 if (subj and sc.subject_mode(dict(subj)) == sc.PREMIUM) else 0
        db.execute(
            "INSERT INTO lessons (section_id, title, status, is_paid, sort_order) "
            "VALUES (?,?, 'closed', ?, "
            "COALESCE((SELECT MAX(sort_order)+1 FROM lessons WHERE section_id=?),0))",
            (section_id, title[:200], paid, section_id))
        return db.fetchone("SELECT last_insert_rowid() AS id")["id"]

    lesson_id = await _thread(_create)
    await state.clear()
    text, kb = _lesson_screen(lesson_id)
    await message.answer("✅ Урок создан (закрыт для учеников).\n\n" + text,
                         reply_markup=kb, parse_mode="HTML")


# ===================== домашнее задание =====================

@router.callback_query(F.data.startswith("na:hw:"), IsAdmin())
async def cb_hw_menu(call: CallbackQuery, state: FSMContext):
    lesson_id = int(call.data.split(":")[2])
    await state.set_state(NotesStates.waiting_hw_file)
    await state.update_data(lesson_id=lesson_id)
    l = _lesson(lesson_id)
    qc = 0
    if l and l["test_id"]:
        qc = db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?",
                         (l["test_id"],))["c"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("⌨️ Вставить текстом", f"na:hwtext:{lesson_id}")],
        [_btn("⬅️ Назад к уроку", f"na:les:{lesson_id}")],
    ])
    await _show(call,
                "📝 <b>Домашнее задание</b>\n\n"
                + (f"Сейчас в тесте: <b>{qc}</b> вопрос(ов). Новый заменит старый.\n\n"
                   if qc else "")
                + "Пришлите файл <b>.txt</b> или <b>.zip</b> (questions.txt + images/), "
                  "либо нажмите «Вставить текстом».\n\n"
                  "Формат:\n"
                  "<code>Текст вопроса\nA) вариант\nB) вариант *\nC) вариант</code>\n\n"
                  "Звёздочка <code>*</code> — правильный ответ.",
                kb)
    await call.answer()


@router.callback_query(F.data.startswith("na:hwtext:"), IsAdmin())
async def cb_hw_text(call: CallbackQuery, state: FSMContext):
    lesson_id = int(call.data.split(":")[2])
    await state.set_state(NotesStates.waiting_hw_text)
    await state.update_data(lesson_id=lesson_id)
    await _show(call, "⌨️ Пришлите текст с вопросами одним сообщением.\n\n/cancel — отмена",
                InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", f"na:les:{lesson_id}")]]))
    await call.answer()


async def _save_hw(message: Message, state: FSMContext, questions, errors):
    data = await state.get_data()
    lesson_id = data["lesson_id"]
    if not questions:
        await message.answer(
            "⚠️ Не удалось разобрать ни одного вопроса.\n\n"
            "Проверьте формат: текст вопроса, ниже варианты «A) …», "
            "у правильного — звёздочка в конце." +
            (f"\n\nОшибки: {len(errors)}" if errors else ""))
        return

    def _apply():
        from webapp import lesson_import as li
        l = _lesson(lesson_id)
        old_test = l["test_id"] if l else None
        title = (l["title"] if l else "ДЗ")[:200]
        test_id = li.finalize_test(title, message.from_user.id, questions, [])
        db.execute("UPDATE lessons SET test_id=? WHERE id=?", (test_id, lesson_id))
        if old_test and old_test != test_id:
            db.execute("DELETE FROM tests WHERE id=?", (old_test,))
        return test_id

    await _thread(_apply)
    await state.clear()
    text, kb = _lesson_screen(lesson_id)
    warn = f"\n⚠️ Строк с ошибками формата: {len(errors)}" if errors else ""
    await message.answer(f"✅ ДЗ сохранено: {len(questions)} вопрос(ов).{warn}\n\n" + text,
                         reply_markup=kb, parse_mode="HTML")


@router.message(NotesStates.waiting_hw_text, IsAdmin())
async def msg_hw_text(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    from webapp import lesson_import as li
    questions, errors = await _thread(li.parse_draft_from_text, raw)
    await _save_hw(message, state, questions, errors)


@router.message(NotesStates.waiting_hw_file, F.document, IsAdmin())
async def msg_hw_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith((".txt", ".zip")):
        await message.answer("Нужен файл .txt или .zip. Или нажмите «Вставить текстом».")
        return
    buf = await bot.download(doc)
    raw = buf.read()
    from webapp import lesson_import as li
    if name.endswith(".zip"):
        questions, errors = await _thread(li.parse_draft_from_zip, raw)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="replace")
        questions, errors = await _thread(li.parse_draft_from_text, text)
    await _save_hw(message, state, questions, errors)


@router.message(NotesStates.waiting_hw_file, F.text, IsAdmin())
async def msg_hw_file_as_text(message: Message, state: FSMContext):
    """Админ вставил вопросы текстом, не нажав кнопку — принимаем как есть."""
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return
    from webapp import lesson_import as li
    questions, errors = await _thread(li.parse_draft_from_text, raw)
    await _save_hw(message, state, questions, errors)
