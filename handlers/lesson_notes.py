"""
Выдача конспектов уроков в личку Telegram (вместо показа на сайте).

Ученик на сайте жмёт «ОТКРЫТЬ КОНСПЕКТ» → deep-link ?start=note_<lesson_id> →
бот проверяет доступ → шлёт конспект (текст + фото) с ПЕРСОНАЛЬНЫМ водяным
знаком (ID платформы, Telegram ID, @username) и защитой от пересылки →
под конспектом кнопка «ВЫПОЛНИТЬ ДЗ» (тест этого же урока на сайте/Mini App).

Каждая выдача пишется в журнал lesson_note_log.
"""
import io
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import utils
from webapp import shortcuts as sc

log = logging.getLogger(__name__)


def _site_url() -> str:
    url = (getattr(config, "SITE_URL", "") or "").strip().rstrip("/")
    if url:
        return url
    return "https://practicumentbot-production.up.railway.app"


def _lesson_access_sync(lesson_id: int, tg_id: int):
    """Проверка доступа к уроку: активный предмет + доступ к нему.
    Копия-ярлык раскрывается в оригинал, доступ считается на оригинале.
    Возвращает (lesson, subject) или (None, причина)."""
    raw = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not raw:
        return None, "not_found"
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (raw["section_id"],))
    if not section:
        return None, "not_found"
    subject = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'",
                          (section["subject_id"],))
    if not subject:
        return None, "not_found"
    subject = dict(subject)
    lesson = sc.resolve_lesson(raw)
    if utils.is_admin(tg_id):
        return lesson, subject
    if raw["status"] != "open":
        return None, "closed"
    mode = sc.subject_mode(subject)
    # Открытый предмет и витрина-премиум пускают внутрь всех: платность
    # решается на уровне самого урока.
    if sc.needs_subject_access(subject) and not _subject_access_ok(subject["id"], tg_id):
        return None, "no_access"
    if raw["is_paid"] and not _paid_ok(lesson, raw, tg_id):
        return None, "need_premium"
    return lesson, subject


def _subject_access_ok(subject_id: int, tg_id: int) -> bool:
    """Доступ к предмету считается на ОРИГИНАЛЕ — копия делит его с ним."""
    real = sc.orig_subject_id(subject_id)
    row = db.fetchone(
        "SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
        (real, tg_id))
    if not row:
        return False
    if not row["expires_at"]:
        return True
    from datetime import datetime
    try:
        return datetime.fromisoformat(row["expires_at"]) > datetime.utcnow()
    except ValueError:
        return False


def _paid_ok(lesson: dict, raw, tg_id: int) -> bool:
    """Платный урок: Премиум, личный доступ к уроку или доступ к предмету."""
    user = utils.get_user_by_tg(tg_id)
    if user and utils.is_premium(user["id"]):
        return True
    real_lesson = sc.orig_lesson_id(raw["id"])
    if db.fetchone("SELECT id FROM lesson_access WHERE lesson_id=? AND user_tg_id=?",
                   (real_lesson, tg_id)):
        return True
    for lid in {raw["id"], real_lesson}:
        row = db.fetchone(
            "SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
            "WHERE l.id=?", (lid,))
        if row and _subject_access_ok(row["subject_id"], tg_id):
            return True
    return False


def _watermark_text(user: dict, tg_id: int) -> str:
    """Метка на страницах — только номер пользователя, без пояснений.

    Ученику это ни о чём не говорит и не отвлекает, а по любому пересланному
    снимку сразу видно, чья копия утекла.
    """
    return str(tg_id)


def _watermark_caption(user: dict, tg_id: int) -> str:
    """Понятная подпись под шапкой конспекта — уже с датой и ником."""
    uname = user.get("username") if user else None
    who = f"@{uname}" if uname else ((user.get("first_name") if user else None) or "ученик")
    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(timezone(timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")
    return f"{who} · {tg_id} · {stamp}"


def _watermark_image_bytes(raw: bytes, label: str) -> bytes:
    """Один крупный номер по диагонали через всю страницу.

    Мелкая сетка на телефоне смазывалась и на фото не читалась — а смысл метки
    в том, чтобы по снимку было видно, чья это копия. Поэтому: одна надпись во
    всю ширину, повёрнутая по диагонали, очень прозрачная. Читать не мешает,
    но на любом кадре различима, и вырезать её из картинки нельзя.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        W, H = img.size

        # Размер подбираем так, чтобы надпись заняла ~85% диагонали
        import math
        diag = math.hypot(W, H)
        target = diag * 0.85
        size = max(24, int(target / max(1, len(label)) * 1.75))

        def _load(px):
            for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                         "/System/Library/Fonts/Supplemental/Arial.ttf",
                         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
                try:
                    return ImageFont.truetype(path, px)
                except Exception:
                    continue
            return ImageFont.load_default()

        font = _load(size)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        def _measure(f):
            try:
                b = probe.textbbox((0, 0), label, font=f)
                return b[2] - b[0], b[3] - b[1]
            except Exception:
                return len(label) * size // 2, size

        tw, th = _measure(font)
        # Подгоняем кегль, пока надпись не впишется в диагональ
        for _ in range(12):
            if tw <= target or size <= 20:
                break
            size = max(20, int(size * target / max(tw, 1)))
            font = _load(size)
            tw, th = _measure(font)

        # Рисуем на прозрачном слое и поворачиваем его целиком
        pad = int(size * 0.6)
        layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((pad, pad), label, font=font,
                                   fill=(118, 126, 145, 58))
        angle = math.degrees(math.atan2(H, W))       # ровно по диагонали листа
        layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)

        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        canvas.paste(layer, ((W - layer.width) // 2, (H - layer.height) // 2), layer)

        out_img = Image.alpha_composite(img, canvas).convert("RGB")
        buf = io.BytesIO()
        out_img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        log.warning("watermark image: %s", e)
        return raw


def _lesson_images_sync(lesson_id: int):
    """Страницы конспекта по порядку. Часть может лежать на диске, часть —
    в Telegram (file_id): отдаём обе, отличая по полю storage."""
    rows = db.fetchall(
        "SELECT image_path, file_id, storage FROM lesson_images "
        "WHERE lesson_id=? ORDER BY sort_order, id", (lesson_id,))
    return [dict(r) for r in rows]


def _uploads_root() -> Path:
    return Path(config.DB_PATH).resolve().parent


def _remember_msg_sync(chat_id: int, message_id: int, lesson_id: int):
    """Запомнить сообщение конспекта — через сутки удалим его молча."""
    try:
        db.execute(
            "INSERT INTO note_messages (chat_id, message_id, lesson_id) VALUES (?,?,?)",
            (chat_id, message_id, lesson_id))
    except Exception as e:
        log.warning("remember note msg: %s", e)


async def cleanup_notes_loop(bot: Bot):
    """Тихо удаляет отправленные конспекты через 24 часа.
    Пользователю ничего не сообщаем — сообщения просто исчезают."""
    import asyncio
    from datetime import datetime, timedelta
    await asyncio.sleep(120)
    while True:
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
            rows = await asyncio.to_thread(
                db.fetchall,
                "SELECT id, chat_id, message_id FROM note_messages "
                "WHERE deleted=0 AND sent_at <= ? LIMIT 300", (cutoff,))
            for r in rows:
                try:
                    await bot.delete_message(r["chat_id"], r["message_id"])
                except Exception:
                    pass  # уже удалено/недоступно — всё равно помечаем
                await asyncio.to_thread(
                    db.execute, "UPDATE note_messages SET deleted=1 WHERE id=?", (r["id"],))
                await asyncio.sleep(0.05)  # не упираемся в лимиты Telegram
            if rows:
                log.info("notes cleanup: удалено %d сообщений", len(rows))
        except Exception as e:
            log.warning("notes cleanup: %s", e)
        await asyncio.sleep(300)  # каждые 5 минут (быстрый отзыв доступа)


def _access_kind_sync(tg_id: int, lesson: dict, subject: dict):
    """На каком основании открыт конспект и с какого числа это основание есть.
    Возвращает (тип, дата получения) — для журнала просмотров."""
    if utils.is_admin(tg_id):
        return "администратор", None
    real_subject = sc.orig_subject_id(subject["id"])
    row = db.fetchone(
        "SELECT granted_at, expires_at FROM subject_access "
        "WHERE subject_id=? AND user_tg_id=?", (real_subject, tg_id))
    if row:
        until = f" (до {row['expires_at'][:10]})" if row["expires_at"] else " (бессрочно)"
        return f"доступ к предмету{until}", row["granted_at"]
    real_lesson = sc.orig_lesson_id(lesson["id"])
    row = db.fetchone(
        "SELECT granted_at FROM lesson_access WHERE lesson_id=? AND user_tg_id=?",
        (real_lesson, tg_id))
    if row:
        return "доступ к уроку", row["granted_at"]
    user = utils.get_user_by_tg(tg_id)
    if user and utils.is_premium(user["id"]):
        prow = db.fetchone(
            "SELECT granted_at, expires_at FROM premium_users WHERE user_id=?",
            (user["id"],))
        until = ""
        if prow and prow["expires_at"]:
            until = f" (до {prow['expires_at'][:10]})"
        elif prow:
            until = " (бессрочно)"
        return f"Премиум{until}", prow["granted_at"] if prow else None
    return "бесплатный урок", None


def _log_delivery_sync(tg_id: int, lesson_id: int, test_id, n_images: int,
                       lesson: dict = None, subject: dict = None,
                       source: str = "bot"):
    """Запись в журнал просмотров: снимок пользователя, урока и основания
    доступа. Снимок — чтобы история не поехала при переименованиях."""
    try:
        from datetime import datetime, timedelta, timezone
        user = utils.get_user_by_tg(tg_id) or {}
        subject_title = section_title = lesson_title = ""
        subject_id = None
        access_type = access_since = None
        if lesson:
            lesson_title = lesson.get("title") or ""
            sec = db.fetchone("SELECT title, subject_id FROM sections WHERE id=?",
                              (lesson.get("section_id"),))
            if sec:
                section_title = sec["title"]
                subject_id = sec["subject_id"]
        if subject:
            subject_title = subject.get("title") or ""
            subject_id = subject_id or subject.get("id")
            access_type, access_since = _access_kind_sync(tg_id, lesson or {}, subject)
        local = datetime.now(timezone(timedelta(hours=5))).strftime("%d.%m.%Y %H:%M:%S")
        db.execute(
            "INSERT INTO lesson_note_log (tg_id, lesson_id, test_id, images_sent, "
            "subject_id, subject_title, section_title, lesson_title, username, "
            "first_name, last_name, phone, access_type, access_since, "
            "opened_at_local, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tg_id, lesson_id, test_id, n_images, subject_id, subject_title,
             section_title, lesson_title, user.get("username"),
             user.get("first_name"), user.get("last_name"), user.get("phone"),
             access_type, access_since, local, source))
    except Exception as e:
        log.warning("note log: %s", e)


def _quizlet_url_sync(lesson: dict) -> str:
    """Ссылка на карточки Quizlet: сначала у урока, иначе общая у предмета."""
    u = (lesson.get("quizlet_url") or "").strip()
    if u:
        return u
    row = db.fetchone(
        "SELECT s.quizlet_url FROM lessons l JOIN sections sec ON sec.id=l.section_id "
        "JOIN subjects s ON s.id=sec.subject_id WHERE l.id=?", (lesson["id"],))
    return (row["quizlet_url"] or "").strip() if row else ""


def _hw_keyboard(lesson: dict) -> InlineKeyboardMarkup:
    """Кнопка «ВЫПОЛНИТЬ ДЗ» — ведёт на тест ИМЕННО этого урока.
    Для копии-ярлыка ссылка ведёт на ту карточку, откуда ученик пришёл."""
    rows = []
    url_id = lesson.get("url_id") or lesson.get("copy_id") or lesson["id"]
    if lesson.get("test_id"):
        url = f"{_site_url()}/learn/lesson/{url_id}/test"
        rows.append([InlineKeyboardButton(text="📝 ВЫПОЛНИТЬ ДЗ", url=url)])
    qz = _quizlet_url_sync(lesson)
    if qz:
        rows.append([InlineKeyboardButton(text="🃏 Карточки Quizlet", url=qz)])
    rows.append([InlineKeyboardButton(
        text="📚 Открыть урок на сайте", url=f"{_site_url()}/learn/lesson/{url_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_lesson_note(bot: Bot, chat_id: int, tg_id: int, lesson_id: int) -> bool:
    """Отправить персональный конспект урока в личку. True если отправлен."""
    import asyncio
    lesson, subject = await asyncio.to_thread(_lesson_access_sync, lesson_id, tg_id)
    if not lesson:
        reason = subject
        msgs = {
            "not_found": "⚠️ Урок не найден.",
            "no_access": "🔒 У вас нет доступа к этому предмету.\n\nОбратитесь к куратору за доступом.",
            "expired": "⏳ Срок доступа к предмету истёк.",
            "closed": "🔒 Этот урок пока закрыт преподавателем.",
            "need_premium": "💎 Этот конспект платный.\n\nОформите Премиум в боте "
                            "или попросите куратора выдать доступ к предмету.",
        }
        await bot.send_message(chat_id, msgs.get(reason, "⚠️ Конспект недоступен."))
        return False

    lesson["url_id"] = lesson_id     # ссылки ведут туда, откуда пришёл ученик
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    label = _watermark_text(user or {}, tg_id)          # на страницах — только номер
    caption = _watermark_caption(user or {}, tg_id)      # в шапке — с ником и датой

    header = (f"📖 <b>{utils.escape_html(lesson['title'])}</b>\n"
              f"<i>{utils.escape_html(subject['title'])}</i>\n\n"
              f"👤 Персональная копия: {utils.escape_html(caption)}\n"
              f"⚠️ Пересылка запрещена — копия помечена вашим ID.")
    _m = await bot.send_message(chat_id, header, parse_mode="HTML", protect_content=True)
    await asyncio.to_thread(_remember_msg_sync, chat_id, _m.message_id, lesson_id)

    # 1) Текст конспекта
    text = (lesson.get("content_html") or "").strip()
    if text:
        chunk = 3500
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)] or [text]
        for i, part in enumerate(parts):
            body = utils.escape_html(part)
            suffix = f"\n\n<i>— {utils.escape_html(caption)}</i>"
            _m = await bot.send_message(chat_id, body + suffix,
                                        parse_mode="HTML", protect_content=True)
            await asyncio.to_thread(_remember_msg_sync, chat_id, _m.message_id, lesson_id)

    # 2) Фото конспекта — с персональным водяным знаком
    paths = await asyncio.to_thread(_lesson_images_sync, lesson["id"])
    sent_images = 0
    root = _uploads_root()
    for page in paths:
        try:
            raw = None
            if (page.get("storage") or "disk") == "telegram" and page.get("file_id"):
                # Страница лежит в Telegram: качаем в память, метим и шлём.
                # На сервере не остаётся ни байта.
                buf = await bot.download(page["file_id"])
                raw = buf.read()
            else:
                fp = root / (page.get("image_path") or "").lstrip("/")
                if not fp.exists():
                    continue
                raw = fp.read_bytes()
            if not raw:
                continue
            marked = await asyncio.to_thread(_watermark_image_bytes, raw, label)
            _m = await bot.send_photo(
                chat_id, BufferedInputFile(marked, filename=f"note_{lesson['id']}_{sent_images}.jpg"),
                protect_content=True)
            await asyncio.to_thread(_remember_msg_sync, chat_id, _m.message_id, lesson_id)
            sent_images += 1
        except Exception as e:
            log.warning("send note image: %s", e)

    if not text and not sent_images:
        await bot.send_message(chat_id, "ℹ️ У этого урока пока нет конспекта.")

    # 3) Кнопка «ВЫПОЛНИТЬ ДЗ»
    await bot.send_message(
        chat_id,
        "✅ Конспект отправлен.\n\nКогда изучите — выполните домашнее задание 👇",
        reply_markup=_hw_keyboard(lesson), protect_content=True)

    await asyncio.to_thread(_log_delivery_sync, tg_id, lesson_id,
                            lesson.get("test_id"), sent_images, lesson, subject, "bot")
    return True
