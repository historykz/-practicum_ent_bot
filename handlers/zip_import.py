"""
Импорт вопросов из ZIP-архива с картинками.
Кнопка «🖼 Импорт ZIP» в карточке теста.
Бот берёт готовые картинки из архива (НЕ генерирует) и создаёт вопросы.
"""
import io
import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, BufferedInputFile

import database as db
import utils
from filters import IsAdmin
from services import zip_import_service as zis

router = Router(name="zip_import")
log = logging.getLogger(__name__)


class ZipImportStates(StatesGroup):
    waiting_zip = State()


_HELP = (
    "🖼 <b>Импорт из ZIP с картинками</b>\n\n"
    "Пришли <b>ZIP-архив</b>:\n"
    "• <code>questions.txt</code> — вопросы\n"
    "• папка <code>images/</code> — картинки\n\n"
    "<b>Формат questions.txt:</b>\n"
    "<code>[img:q1.png]\n"
    "Текст вопроса (необязательно)\n"
    "A) текст ответа\n"
    "B) [img:b.png] *\n"
    "C) 5\n"
    "D) 7</code>\n\n"
    "• <code>[img:файл]</code> в строке вопроса — картинка вопроса\n"
    "• <code>[img:файл]</code> в варианте — картинка варианта\n"
    "• <code>*</code> в конце — правильный ответ\n"
    "• Пустая строка разделяет вопросы\n\n"
    "Бот сам поймёт: если у вариантов картинки — склеит их в одно фото A/B/C/D.\n\n"
    "/cancel — отмена."
)


@router.callback_query(F.data.startswith("admimport_zip:"), IsAdmin())
async def cb_import_zip(call: CallbackQuery, state: FSMContext):
    try:
        tid = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    await state.set_state(ZipImportStates.waiting_zip)
    await state.update_data(zip_test_id=tid)
    await call.message.answer(_HELP, parse_mode="HTML")
    await call.answer()


@router.message(ZipImportStates.waiting_zip, F.document, IsAdmin())
async def msg_import_zip(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or '').lower().endswith('.zip'):
        await message.answer("Нужен ZIP-архив. Пришли .zip")
        return
    data = await state.get_data()
    test_id = data.get('zip_test_id')
    await state.clear()

    status = await message.answer("📦 Распаковываю архив…")
    # Скачиваем zip
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        zip_bytes = buf.getvalue()
    except Exception as e:
        await status.edit_text(f"⚠️ Не смог скачать архив: {e}")
        return

    questions, images, errors = zis.parse_zip(zip_bytes)
    if not questions:
        msg = "⚠️ Не нашёл вопросов в архиве.\n"
        if errors:
            msg += "\n".join(f"• {e}" for e in errors[:8])
        await status.edit_text(msg)
        return

    await status.edit_text(
        f"📦 Найдено {len(questions)} вопросов. Загружаю картинки и создаю…")

    created = 0
    img_uploaded = 0
    fail = []

    for qi, q in enumerate(questions, 1):
        try:
            # 1. Картинка вопроса
            q_file_id = None
            if q.get('q_image') and q['q_image'] in images:
                q_file_id = await _upload_image(
                    bot, message.chat.id, images[q['q_image']],
                    f"вопрос {qi}")
                if q_file_id:
                    img_uploaded += 1

            # 2. Варианты: есть ли картинки?
            opts_with_img = [o for o in q['options'] if o.get('image')]
            merged_file_id = None
            option_texts = []
            correct_idx = 0

            if opts_with_img:
                # Склеиваем варианты-картинки в одно фото A/B/C/D
                merged = zis.merge_option_images(q['options'], images)
                if merged:
                    merged_file_id = await _upload_image(
                        bot, message.chat.id, merged, f"варианты {qi}")
                    img_uploaded += 1
                # В Quiz Poll варианты = буквы
                letters = "ABCDE"
                for idx, o in enumerate(q['options']):
                    option_texts.append(letters[idx])
                    if o['correct']:
                        correct_idx = idx
            else:
                # Обычные текстовые варианты
                for idx, o in enumerate(q['options']):
                    option_texts.append(o['text'][:100] or f"вариант {idx+1}")
                    if o['correct']:
                        correct_idx = idx

            # 3. Сохраняем вопрос в БД
            # Если у вопроса своя картинка — она в photo_file_id.
            # Если варианты-картинки склеены — приоритет у склейки (показываем её).
            photo_id = merged_file_id or q_file_id
            q_text = q['text'] or "Выберите правильный вариант:"

            cur = db.execute(
                "INSERT INTO questions (test_id, text, photo_file_id, order_num) "
                "VALUES (?,?,?,?)",
                (test_id, q_text, photo_id,
                 db.fetchone("SELECT COALESCE(MAX(order_num),0)+1 AS n FROM questions WHERE test_id=?", (test_id,))['n']))
            qid = cur.lastrowid
            # Если есть и картинка вопроса И склейка вариантов — отправим обе:
            # сохраняем склейку как photo, а картинку вопроса добавим в текст? 
            # Проще: если обе — показываем их последовательно через вторую запись невозможно,
            # поэтому склейку вариантов кладём в photo, вопрос-картинку игнорируем если конфликт.

            for idx, otext in enumerate(option_texts):
                db.execute(
                    "INSERT INTO question_options (question_id, text, is_correct, order_num) "
                    "VALUES (?,?,?,?)",
                    (qid, otext, 1 if idx == correct_idx else 0, idx))
            created += 1
        except Exception as e:
            log.exception("zip import q%s: %s", qi, e)
            fail.append(f"вопрос {qi}: {e}")

    # Отчёт
    lines = [
        "✅ <b>Импорт завершён!</b>\n",
        f"• Вопросов создано: <b>{created}</b>",
        f"• Картинок загружено: <b>{img_uploaded}</b>",
    ]
    if fail:
        lines.append(f"\n⚠️ Проблемы ({len(fail)}):")
        for f in fail[:8]:
            lines.append(f"• {f}")
    if errors:
        lines.append(f"\n⚠️ При разборе: {len(errors)}")
        for e in errors[:5]:
            lines.append(f"• {e}")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _upload_image(bot, chat_id, img_bytes, label="") -> str:
    """Залить картинку и вернуть file_id."""
    try:
        photo = BufferedInputFile(img_bytes, filename="q.png")
        msg = await bot.send_photo(chat_id, photo,
                                    caption=f"📷 Загружено: {label}" if label else None)
        if msg.photo:
            return msg.photo[-1].file_id
    except Exception as e:
        log.warning("upload image: %s", e)
    return None
