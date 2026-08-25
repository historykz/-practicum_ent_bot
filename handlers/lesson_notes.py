"""
Выдача конспектов уроков в личку Telegram (вместо показа на сайте).

Ученик на сайте жмёт «ОТКРЫТЬ КОНСПЕКТ» → deep-link ?start=note_<lesson_id> →
бот проверяет доступ → шлёт конспект (текст + фото) с ПЕРСОНАЛЬНЫМ водяным
знаком (ID платформы, Telegram ID, @username) и защитой от пересылки →
под конспектом кнопка «ВЫПОЛНИТЬ ДЗ» (тест этого же урока на сайте/Mini App).

Каждая выдача пишется в журнал lesson_note_log.
"""
import asyncio
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
        # Предмет продаётся отдельно — советовать Премиум бессмысленно
        return None, "need_own_access" if _premium_ignored(raw) else "need_premium"
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


def _lesson_subject_ids(raw) -> list:
    """Предметы урока: свой и оригинала (копия-ярлык делит доступ с ним)."""
    ids = []
    for lid in {raw["id"], sc.orig_lesson_id(raw["id"])}:
        row = db.fetchone(
            "SELECT s.subject_id FROM lessons l JOIN sections s ON s.id=l.section_id "
            "WHERE l.id=?", (lid,))
        if row and row["subject_id"] not in ids:
            ids.append(row["subject_id"])
    return ids


def _premium_ignored(raw) -> bool:
    """Предмет продаётся отдельно — общий Премиум его уроки не открывает.
    Проверяем и предмет копии, и предмет оригинала: иначе запрет обходился бы
    через витрину."""
    for sid in _lesson_subject_ids(raw):
        for real in {sid, sc.orig_subject_id(sid)}:
            subj = db.fetchone("SELECT * FROM subjects WHERE id=?", (real,))
            if subj and sc.premium_ignored(dict(subj)):
                return True
    return False


def _paid_ok(lesson: dict, raw, tg_id: int) -> bool:
    """Платный урок: Премиум, личный доступ к уроку или доступ к предмету.
    Для предметов, которые продаются отдельно, Премиум не в счёт."""
    user = utils.get_user_by_tg(tg_id)
    if user and utils.is_premium(user["id"]) and not _premium_ignored(raw):
        return True
    real_lesson = sc.orig_lesson_id(raw["id"])
    if db.fetchone("SELECT id FROM lesson_access WHERE lesson_id=? AND user_tg_id=?",
                   (real_lesson, tg_id)):
        return True
    for sid in _lesson_subject_ids(raw):
        if _subject_access_ok(sid, tg_id):
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


def _font_candidates():
    """Пути, где обычно лежат шрифты. На хостинге их может не быть вовсе —
    тогда работает запасной путь через увеличение картинки (см. ниже)."""
    return (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    )


# Сегменты цифр (как на электронном табло): a b c d e f g
_SEG = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


def _draw_digits(label: str, cell_w: int):
    """Нарисовать число прямоугольниками, вообще без шрифтов.

    Это запасной путь для хостинга, где шрифтов в системе нет: раньше метка
    там молча не наносилась совсем, и конспекты уходили без защиты.
    """
    from PIL import Image, ImageDraw
    w = max(8, int(cell_w))
    h = int(w * 1.9)
    t = max(2, int(w * 0.20))          # толщина штриха
    gap = int(w * 0.42)                # расстояние между цифрами
    chars = [c for c in str(label) if c in _SEG] or ["0"]
    W = len(chars) * w + (len(chars) - 1) * gap
    img = Image.new("RGBA", (W + t, h + t), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ch in enumerate(chars):
        x = i * (w + gap)
        segs = _SEG[ch]
        mid = h // 2
        if "a" in segs: d.rectangle([x, 0, x + w, t], fill=(255, 255, 255, 255))
        if "g" in segs: d.rectangle([x, mid - t // 2, x + w, mid + t // 2], fill=(255, 255, 255, 255))
        if "d" in segs: d.rectangle([x, h - t, x + w, h], fill=(255, 255, 255, 255))
        if "f" in segs: d.rectangle([x, 0, x + t, mid], fill=(255, 255, 255, 255))
        if "b" in segs: d.rectangle([x + w - t, 0, x + w, mid], fill=(255, 255, 255, 255))
        if "e" in segs: d.rectangle([x, mid, x + t, h], fill=(255, 255, 255, 255))
        if "c" in segs: d.rectangle([x + w - t, mid, x + w, h], fill=(255, 255, 255, 255))
    return img


def _render_label(label: str, target_w: int):
    """Белая надпись шириной ~target_w. Сначала пробуем настоящий шрифт,
    если в системе его нет — рисуем цифры сами."""
    from PIL import Image, ImageDraw, ImageFont

    def measure(font):
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        b = probe.textbbox((0, 0), label, font=font)
        return max(1, b[2] - b[0]), max(1, b[3] - b[1])

    size = max(24, int(target_w / max(1, len(label)) * 1.75))
    for path in _font_candidates():
        try:
            font = ImageFont.truetype(path, size)
            tw, th = measure(font)
            for _ in range(12):
                if abs(tw - target_w) <= target_w * 0.06 or size <= 20:
                    break
                size = max(20, int(size * target_w / tw))
                font = ImageFont.truetype(path, size)
                tw, th = measure(font)
            pad = max(4, size // 3)
            layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
            ImageDraw.Draw(layer).text((pad, pad), label, font=font,
                                       fill=(255, 255, 255, 255))
            return layer
        except Exception:
            continue

    chars = max(1, len([c for c in str(label) if c in _SEG]))
    return _draw_digits(label, int(target_w / (chars * 1.42)))


def _watermark_image_bytes(raw: bytes, label: str) -> bytes:
    """Один крупный номер по диагонали через всю страницу.

    Мелкая сетка на телефоне смазывалась и на фото не читалась — а смысл метки
    в том, чтобы по снимку было видно, чья это копия. Поэтому: одна надпись во
    всю ширину, повёрнутая по диагонали, очень прозрачная. Читать не мешает,
    но на любом кадре различима, и вырезать её из картинки нельзя.
    """
    try:
        import math
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        W, H = img.size

        text_layer = _render_label(str(label), int(math.hypot(W, H) * 0.85))
        # Красим готовую надпись в нужный цвет и прозрачность.
        # 58 было почти незаметно на снимке телефона; 84 читается на любом
        # кадре и при этом не перекрывает текст конспекта.
        WATERMARK_ALPHA = 84
        alpha = text_layer.split()[-1].point(lambda a: int(a * WATERMARK_ALPHA / 255))
        tinted = Image.new("RGBA", text_layer.size, (116, 124, 144, 0))
        tinted.putalpha(alpha)

        angle = math.degrees(math.atan2(H, W))    # ровно по диагонали листа
        rotated = tinted.rotate(angle, expand=True, resample=Image.BICUBIC)

        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        canvas.paste(rotated, ((W - rotated.width) // 2, (H - rotated.height) // 2),
                     rotated)

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


def notes_ttl_hours() -> int:
    """Через сколько часов конспект сам исчезает из переписки.

    Настраивает админ. 0 — не удалять никогда.
    """
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='notes_ttl_hours'")
        if row and row.get("value") not in (None, ""):
            return max(0, int(row["value"]))
    except Exception:
        pass
    return 24


def set_notes_ttl_hours(hours: int) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('notes_ttl_hours', ?)",
               (str(max(0, int(hours))),))


async def wipe_user_notes(bot: Bot, tg_id: int, subject_id=None) -> int:
    """Немедленно стереть у человека ВСЕ выданные конспекты.

    Вызывается, когда доступ забрали или он закончился: оставлять на руках
    материалы, за которые больше не платят, нельзя. Если указан subject_id —
    убираем только конспекты этого предмета.
    """
    sql = ("SELECT id, chat_id, message_id FROM note_messages "
           "WHERE deleted=0 AND chat_id=?")
    args = [tg_id]
    if subject_id:
        sql += (" AND lesson_id IN (SELECT l.id FROM lessons l "
                "JOIN sections s ON s.id=l.section_id WHERE s.subject_id=?)")
        args.append(sc.orig_subject_id(subject_id))
    rows = await asyncio.to_thread(db.fetchall, sql, tuple(args))
    n = 0
    for r in rows:
        try:
            await bot.delete_message(r["chat_id"], r["message_id"])
            n += 1
        except Exception:
            pass    # уже удалено или недоступно — всё равно помечаем
        await asyncio.to_thread(
            db.execute, "UPDATE note_messages SET deleted=1 WHERE id=?", (r["id"],))
        await asyncio.sleep(0.03)
    if rows:
        log.info("Конспекты пользователя %s стёрты: %d сообщений", tg_id, len(rows))
    return n


async def cleanup_notes_loop(bot: Bot):
    """Удаляет выданные конспекты, когда вышел их срок.

    Срок задаёт админ («📖 Конспекты уроков» → «⏱ Срок хранения»). Отдельно
    подчищаем тех, у кого доступ забрали или он истёк, — у них конспекты
    исчезают сразу, не дожидаясь срока.
    """
    import asyncio as _aio
    from datetime import datetime, timedelta
    await _aio.sleep(120)
    while True:
        try:
            hours = await _aio.to_thread(notes_ttl_hours)
            # Отзыв доступа помечает конспекты датой 2000 года — такие сносим
            # всегда, даже если хранение вообще отключено (hours = 0).
            cutoff = "2001-01-01 00:00:00"
            if hours > 0:
                cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat(
                    timespec="seconds")
            if True:
                # ВАЖНО: сравниваем через datetime(), а не сырыми строками.
                # SQLite пишет sent_at с пробелом («2026-08-24 17:30»), а
                # python-cutoff шёл с буквой T («2026-08-24T02:31»). Пробел
                # в ASCII меньше «T», поэтому вечером (когда cutoff попадал
                # в ту же дату) ЛЮБОЙ свежий конспект строково оказывался
                # «старше» и удалялся первым же циклом — через минуты после
                # выдачи. datetime() приводит оба формата к одному виду.
                rows = await _aio.to_thread(
                    db.fetchall,
                    "SELECT id, chat_id, message_id FROM note_messages "
                    "WHERE deleted=0 AND datetime(sent_at) <= datetime(?) "
                    "LIMIT 300", (cutoff,))
                for r in rows:
                    try:
                        await bot.delete_message(r["chat_id"], r["message_id"])
                    except Exception:
                        pass
                    await _aio.to_thread(
                        db.execute, "UPDATE note_messages SET deleted=1 WHERE id=?",
                        (r["id"],))
                    await _aio.sleep(0.05)   # не упираемся в лимиты Telegram
                if rows:
                    log.info("notes cleanup: удалено %d сообщений (срок %d ч)",
                             len(rows), hours)

            # Доступ кончился или его забрали — стираем сразу, не дожидаясь срока
            gone = await _aio.to_thread(_expired_note_messages_sync)
            for r in gone[:300]:
                try:
                    await bot.delete_message(r["chat_id"], r["message_id"])
                except Exception:
                    pass
                await _aio.to_thread(
                    db.execute, "UPDATE note_messages SET deleted=1 WHERE id=?", (r["id"],))
                await _aio.sleep(0.03)
            if gone:
                log.info("notes cleanup: забрано %d конспектов у потерявших доступ",
                         len(gone))
        except Exception as e:
            log.warning("notes cleanup: %s", e)
        await _aio.sleep(300)   # каждые 5 минут


def _expired_note_messages_sync() -> list:
    """Конспекты на руках, доступ к которым уже кончился.

    Проверяем именно доступ к УРОКУ, а не к предмету: у премиум-предмета вход
    открыт всем, а платные уроки внутри — нет. Если бы смотрели на предмет,
    конспекты не забирались бы у тех, у кого просто истёк Премиум.
    """
    from webapp import learning as _lg
    rows = db.fetchall(
        "SELECT nm.id, nm.chat_id, nm.message_id, nm.lesson_id "
        "FROM note_messages nm WHERE nm.deleted=0 AND nm.lesson_id IS NOT NULL")
    out = []
    cache = {}
    for r in rows:
        tg, lid = r["chat_id"], r["lesson_id"]
        key = (tg, lid)
        if key not in cache:
            try:
                if utils.is_admin(tg):
                    cache[key] = True
                else:
                    lesson = db.fetchone("SELECT * FROM lessons WHERE id=?", (lid,))
                    cache[key] = bool(lesson) and _lg._has_lesson_paid_access_sync(
                        sc.resolve_lesson(lesson), tg)
            except Exception:
                cache[key] = True    # сомневаемся — не трогаем
        if not cache[key]:
            out.append(dict(r))
    return out


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


def _miniapp_url(param: str) -> str:
    """Ссылка, открывающая мини-приложение Telegram на нужном экране.

    Именно мини-приложение, а не сайт: оно разворачивается на весь экран
    внутри Telegram, вход происходит сам, и человека никуда не выкидывает.
    """
    short = (getattr(config, "WEB_APP_SHORT_NAME", "") or "").strip()
    base = f"https://t.me/{config.WEB_BOT_USERNAME}"
    if short:
        base += f"/{short}"
    return f"{base}?startapp={param}" if param else base


def _hw_keyboard(lesson: dict) -> InlineKeyboardMarkup:
    """Кнопки под конспектом. ДЗ открывается в мини-приложении на весь экран.
    Для копии-ярлыка ссылка ведёт на ту карточку, откуда ученик пришёл."""
    rows = []
    url_id = lesson.get("url_id") or lesson.get("copy_id") or lesson["id"]
    if lesson.get("test_id"):
        rows.append([InlineKeyboardButton(
            text="📝 ВЫПОЛНИТЬ ДЗ", url=_miniapp_url(f"test_{url_id}"))])
    if lesson.get("video_file_id"):
        # Видео открывается прямо в боте: так оно уходит с запретом на пересылку.
        rows.append([InlineKeyboardButton(
            text="🎬 СМОТРЕТЬ ВИДЕО",
            url=f"https://t.me/{config.WEB_BOT_USERNAME}?start=video_{url_id}")])
    qz = _quizlet_url_sync(lesson)
    if qz:
        rows.append([InlineKeyboardButton(text="🃏 Карточки Quizlet", url=qz)])
    # Кнопки «открыть на сайте» больше нет: всё внутри Telegram.
    return InlineKeyboardMarkup(inline_keyboard=rows)


NOTE_RESEND_SECONDS = 600      # пауза между повторными выдачами одного конспекта


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
            "need_own_access": "💎 Этот курс продаётся отдельно.\n\nОбщий Премиум его "
                               "не открывает — попросите куратора выдать доступ.",
        }
        # Платный урок — не просто отказ, а понятное предложение подписки
        # прямо здесь: человек пришёл с сайта именно за этим.
        if reason in ("need_premium", "need_own_access", "no_access", "expired"):
            try:
                from handlers.premium import build_premium_offer
                u = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
                text, markup = build_premium_offer(u or {"id": 0})
                await bot.send_message(
                    chat_id, msgs.get(reason, "") + "\n\n" + text,
                    reply_markup=markup, parse_mode="HTML")
                return False
            except Exception as e:
                log.warning("premium offer: %s", e)
        await bot.send_message(chat_id, msgs.get(reason, "⚠️ Конспект недоступен."))
        return False

    # Ограничение частоты живёт здесь, а не на сайте: так оно работает,
    # с какой бы стороны ни пришли — с сайта, из приложения или по ссылке.
    real_id = sc.orig_lesson_id(lesson_id)
    recent = await asyncio.to_thread(
        db.fetchone,
        "SELECT created_at FROM auth_events WHERE tg_id=? AND event='note_sent' "
        "AND details=? ORDER BY id DESC LIMIT 1", (tg_id, f"lesson={real_id}"))
    if recent and recent.get("created_at"):
        from datetime import datetime as _dt
        try:
            age = (_dt.utcnow() - _dt.fromisoformat(
                str(recent["created_at"]).replace(" ", "T"))).total_seconds()
        except ValueError:
            age = NOTE_RESEND_SECONDS
        if age < NOTE_RESEND_SECONDS:
            left = int((NOTE_RESEND_SECONDS - age) // 60) + 1
            await bot.send_message(
                chat_id,
                f"📖 Конспект этого урока уже отправлен — он выше в этой переписке.\n\n"
                f"Прислать заново можно через {left} мин.")
            return False

    lesson["url_id"] = lesson_id     # ссылки ведут туда, откуда пришёл ученик
    user = await asyncio.to_thread(utils.get_user_by_tg, tg_id)
    label = _watermark_text(user or {}, tg_id)          # на страницах — только номер

    # В шапке — только тема конспекта. Номер копии есть на самих страницах
    # водяным знаком, дублировать его текстом и пугать пользователя не нужно.
    header = (f"📖 <b>{utils.escape_html(lesson['title'])}</b>\n"
              f"<i>{utils.escape_html(subject['title'])}</i>")
    _m = await bot.send_message(chat_id, header, parse_mode="HTML", protect_content=True)
    await asyncio.to_thread(_remember_msg_sync, chat_id, _m.message_id, lesson_id)

    # 1) Текст конспекта
    text = (lesson.get("content_html") or "").strip()
    if text:
        chunk = 3500
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)] or [text]
        for i, part in enumerate(parts):
            body = utils.escape_html(part)
            _m = await bot.send_message(chat_id, body,
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
    # Отметка для паузы: она же не даёт зашвырять бота повторными нажатиями
    try:
        from services.account_service import log_event as _ev
        await asyncio.to_thread(_ev, (user or {}).get("id"), tg_id, "note_sent",
                                f"lesson={sc.orig_lesson_id(lesson_id)}")
    except Exception as e:
        log.debug("note_sent log: %s", e)
    return True


# ===================== видео урока ученику =====================

async def send_lesson_video(bot: Bot, chat_id: int, tg_id: int, lesson_id: int) -> bool:
    """Отправить видео урока в личные сообщения.

    Проверка доступа та же, что у конспекта. Видео уходит с запретом на
    пересылку и сохранение и попадает в тот же список на автоудаление —
    иначе оно оставалось бы у ученика навсегда после отзыва доступа.
    """
    lesson, subject = await asyncio.to_thread(_lesson_access_sync, lesson_id, tg_id)
    if lesson is None:
        reason = subject
        text = {
            "not_found": "Урок не найден или закрыт.",
            "no_access": "Этот урок пока закрыт — нужен доступ к предмету.",
            "need_payment": "Урок платный. Оформите Премиум или попросите доступ.",
        }.get(reason, "Видео недоступно.")
        await bot.send_message(chat_id, text)
        return False

    file_id = lesson.get("video_file_id")
    if not file_id:
        await bot.send_message(chat_id, "У этого урока пока нет видео.")
        return False

    caption = (f"🎬 <b>{utils.escape_html(lesson['title'])}</b>\n"
               f"<i>{utils.escape_html(subject['title'])}</i>")
    try:
        m = await bot.send_video(chat_id, file_id, caption=caption,
                                 parse_mode="HTML", protect_content=True,
                                 supports_streaming=True)
    except Exception:
        # Видео могло быть прислано документом — тогда шлём как документ
        m = await bot.send_document(chat_id, file_id, caption=caption,
                                    parse_mode="HTML", protect_content=True)
    await asyncio.to_thread(_remember_msg_sync, chat_id, m.message_id, lesson["id"])
    return True
