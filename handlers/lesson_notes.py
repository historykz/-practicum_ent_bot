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

log = logging.getLogger(__name__)


def _site_url() -> str:
    url = (getattr(config, "SITE_URL", "") or "").strip().rstrip("/")
    if url:
        return url
    return "https://practicumentbot-production.up.railway.app"


def _lesson_access_sync(lesson_id: int, tg_id: int):
    """Проверка доступа к уроку: активный предмет + доступ к нему.
    Возвращает (lesson, subject) или (None, причина)."""
    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not lesson:
        return None, "not_found"
    section = db.fetchone("SELECT * FROM sections WHERE id=?", (lesson["section_id"],))
    if not section:
        return None, "not_found"
    subject = db.fetchone("SELECT * FROM subjects WHERE id=? AND status='active'",
                          (section["subject_id"],))
    if not subject:
        return None, "not_found"
    if utils.is_admin(tg_id):
        return dict(lesson), dict(subject)
    # доступ к предмету
    if not subject["is_open"]:
        row = db.fetchone(
            "SELECT expires_at FROM subject_access WHERE subject_id=? AND user_tg_id=?",
            (subject["id"], tg_id))
        if not row:
            return None, "no_access"
        if row["expires_at"]:
            from datetime import datetime
            try:
                if datetime.fromisoformat(row["expires_at"]) <= datetime.utcnow():
                    return None, "expired"
            except ValueError:
                pass
    if lesson["status"] != "open":
        return None, "closed"
    return dict(lesson), dict(subject)


def _watermark_text(user: dict, tg_id: int) -> str:
    uname = user.get("username") if user else None
    if uname:
        who = f"@{uname}"
    else:
        who = (user.get("first_name") if user else None) or "ученик"
    return f"{who} · TG:{tg_id} · ID:{(user or {}).get('id', '-')}"


def _watermark_image_bytes(raw: bytes, label: str) -> bytes:
    """Диагональный полупрозрачный знак по всей странице."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        size = max(14, img.size[0] // 34)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", size)
        except Exception:
            font = ImageFont.load_default()
        step_x = max(220, img.size[0] // 2)
        step_y = max(140, img.size[1] // 6)
        for y in range(0, img.size[1] + step_y, step_y):
            for x in range(-step_x, img.size[0] + step_x, step_x):
                tmp = Image.new("RGBA", (step_x, 60), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((0, 0), label, font=font, fill=(255, 255, 255, 90))
                rot = tmp.rotate(30, expand=True)
                layer.paste(rot, (x, y), rot)
        out_img = Image.alpha_composite(img, layer).convert("RGB")
        buf = io.BytesIO()
        out_img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        log.warning("watermark image: %s", e)
        return raw


def _lesson_images_sync(lesson_id: int):
    rows = db.fetchall(
        "SELECT image_path FROM lesson_images WHERE lesson_id=? ORDER BY sort_order, id",
        (lesson_id,))
    return [r["image_path"] for r in rows]


def _uploads_root() -> Path:
    return Path(config.DB_PATH).resolve().parent


def _log_delivery_sync(tg_id: int, lesson_id: int, test_id, n_images: int):
    try:
        db.execute(
            "INSERT INTO lesson_note_log (tg_id, lesson_id, test_id, images_sent) VALUES (?,?,?,?)",
            (tg_id, lesson_id, test_id, n_images))
    except Exception as e:
        log.warning("note log: %s", e)


def _hw_keyboard(lesson: dict) -> InlineKeyboardMarkup:
    """Кнопка «ВЫПОЛНИТЬ ДЗ» — ведёт на тест ИМЕННО этого урока."""
    rows = []
    if lesson.get("test_id"):
        url = f"{_site_url()}/learn/lesson/{lesson['id']}/test"
        rows.append([InlineKeyboardButton(text="📝 ВЫПОЛНИТЬ ДЗ", url=url)])
    rows.append([InlineKeyboardButton(
        text="📚 Открыть урок на сайте", url=f"{_site_url()}/learn/lesson/{lesson['id']}")])
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
        }
        await bot.send_message(chat_id, msgs.get(reason, "⚠️ Конспект недоступен."))
        return False

    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    label = _watermark_text(user or {}, tg_id)

    header = (f"📖 <b>{utils.escape_html(lesson['title'])}</b>\n"
              f"<i>{utils.escape_html(subject['title'])}</i>\n\n"
              f"👤 Персональная копия: {utils.escape_html(label)}\n"
              f"⚠️ Пересылка запрещена — копия помечена вашим ID.")
    await bot.send_message(chat_id, header, parse_mode="HTML", protect_content=True)

    # 1) Текст конспекта
    text = (lesson.get("content_html") or "").strip()
    if text:
        chunk = 3500
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)] or [text]
        for i, part in enumerate(parts):
            body = utils.escape_html(part)
            suffix = f"\n\n<i>— {utils.escape_html(label)}</i>"
            await bot.send_message(chat_id, body + suffix,
                                   parse_mode="HTML", protect_content=True)

    # 2) Фото конспекта — с персональным водяным знаком
    paths = await asyncio.to_thread(_lesson_images_sync, lesson_id)
    sent_images = 0
    root = _uploads_root()
    for rel in paths:
        try:
            fp = root / rel.lstrip("/")
            if not fp.exists():
                continue
            raw = fp.read_bytes()
            marked = await asyncio.to_thread(_watermark_image_bytes, raw, label)
            await bot.send_photo(
                chat_id, BufferedInputFile(marked, filename=f"note_{lesson_id}_{sent_images}.jpg"),
                protect_content=True)
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

    await asyncio.to_thread(_log_delivery_sync, tg_id, lesson_id, lesson.get("test_id"), sent_images)
    return True
