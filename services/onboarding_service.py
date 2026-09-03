"""
Инструкция после выдачи Премиума.

Одна дверь для всех способов выдачи: покупка за Stars, ручная выдача админом,
промокод, подарок, награда за друзей, продление. Везде вызывается
premium_activated() — и человек получает одинаковую инструкцию, независимо
от того, как ему открыли доступ.

Саму инструкцию админ собирает из блоков: приветствие, видео, текст, кнопки.
Блоки хранятся в базе и меняются без правки кода.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from services import study_settings as ss

log = logging.getLogger(__name__)

KINDS = ("text", "photo", "video", "animation", "video_note", "voice", "document")

KIND_TITLES = {
    "text": "Текст", "photo": "Фото", "video": "Видео", "animation": "GIF",
    "video_note": "Кружок", "voice": "Голосовое", "document": "Файл",
}

_ready = False


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS onboarding_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'text', file_id TEXT DEFAULT '',
            text TEXT DEFAULT '', buttons TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        _ready = True
    except Exception:
        pass


# ---------- Блоки инструкции ----------

def add_block(kind: str, file_id: str = "", text: str = "",
              buttons: list = None) -> int:
    ensure_schema()
    kind = kind if kind in KINDS else "text"
    nxt = (db.fetchone("SELECT MAX(sort_order) AS m FROM onboarding_blocks")["m"] or 0) + 1
    db.execute(
        "INSERT INTO onboarding_blocks (kind, file_id, text, buttons, sort_order) "
        "VALUES (?,?,?,?,?)",
        (kind, (file_id or "")[:300], (text or "")[:3000],
         json.dumps(buttons or [], ensure_ascii=False), nxt))
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def blocks(only_enabled: bool = False) -> list:
    ensure_schema()
    sql = "SELECT * FROM onboarding_blocks"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_order, id"
    try:
        return [dict(r) for r in db.fetchall(sql)]
    except Exception:
        return []


def get_block(block_id: int) -> Optional[dict]:
    ensure_schema()
    row = db.fetchone("SELECT * FROM onboarding_blocks WHERE id=?", (block_id,))
    return dict(row) if row else None


def delete_block(block_id: int) -> None:
    ensure_schema()
    db.execute("DELETE FROM onboarding_blocks WHERE id=?", (block_id,))


def toggle_block(block_id: int) -> None:
    ensure_schema()
    db.execute("UPDATE onboarding_blocks SET enabled=1-enabled WHERE id=?", (block_id,))


def edit_block(block_id: int, text: str = None, buttons: list = None) -> None:
    ensure_schema()
    if text is not None:
        db.execute("UPDATE onboarding_blocks SET text=? WHERE id=?",
                   (text[:3000], block_id))
    if buttons is not None:
        db.execute("UPDATE onboarding_blocks SET buttons=? WHERE id=?",
                   (json.dumps(buttons, ensure_ascii=False), block_id))


def move_block(block_id: int, direction: str) -> None:
    items = blocks()
    ids = [b["id"] for b in items]
    if block_id not in ids:
        return
    i = ids.index(block_id)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(ids):
        return
    ids[i], ids[j] = ids[j], ids[i]
    for pos, bid in enumerate(ids, start=1):
        db.execute("UPDATE onboarding_blocks SET sort_order=? WHERE id=?", (pos, bid))


# ---------- Кнопки под инструкцией ----------

def _final_keyboard() -> Optional[InlineKeyboardMarkup]:
    """Кнопки, которые видит человек после инструкции."""
    import config
    short = (getattr(config, "WEB_APP_SHORT_NAME", "") or "").strip()
    base = f"https://t.me/{config.WEB_BOT_USERNAME}"
    if short:
        base += f"/{short}"

    rows = [
        [InlineKeyboardButton(text="📚 Открыть конспекты", url=f"{base}?startapp=learn")],
        [InlineKeyboardButton(text="✅ Перейти к ДЗ", url=f"{base}?startapp=tests")],
    ]
    row = [InlineKeyboardButton(text="🎥 Посмотреть инструкцию ещё раз",
                                callback_data="onb:again")]
    rows.append(row)
    manager = (ss.get("support_username") or "").strip().lstrip("@")
    if manager:
        rows.append([InlineKeyboardButton(text="❓ Поддержка",
                                          url=f"https://t.me/{manager}")])
    else:
        rows.append([InlineKeyboardButton(text="❓ Поддержка",
                                          callback_data="support:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _block_keyboard(block: dict) -> Optional[InlineKeyboardMarkup]:
    """Кнопки самого блока — их задаёт админ (подпись + ссылка)."""
    try:
        raw = json.loads(block.get("buttons") or "[]")
    except (ValueError, TypeError):
        return None
    rows = []
    for item in raw:
        if isinstance(item, dict) and item.get("text") and item.get("url"):
            rows.append([InlineKeyboardButton(text=item["text"][:64],
                                              url=item["url"])])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ---------- Отправка инструкции ----------

DEFAULT_INTRO = ("🎉 <b>Премиум активирован!</b>\n\n"
                 "Теперь тебе открыты все конспекты и платные тесты.")

DEFAULT_HOWTO = ("<b>Как пользоваться обучением:</b>\n\n"
                 "1. Открываешь нужный урок.\n"
                 "2. Изучаешь конспект.\n"
                 "3. После конспекта переходишь в ДЗ.\n"
                 "4. Выполняешь задания.\n"
                 "5. После выполнения тема отмечается как завершённая.\n\n"
                 "Занимайся понемногу каждый день — так материал усваивается "
                 "лучше всего.")


async def send_instruction(bot, tg_id: int) -> bool:
    """Показать инструкцию. Тем же вызовом работает кнопка «посмотреть ещё раз»."""
    ensure_schema()
    items = blocks(only_enabled=True)
    sent = False

    if not items:
        # Админ ещё не собрал свою инструкцию — показываем базовую,
        # чтобы человек в любом случае знал, что делать.
        try:
            await bot.send_message(tg_id, DEFAULT_INTRO, parse_mode="HTML")
            await asyncio.sleep(0.4)
            await bot.send_message(tg_id, DEFAULT_HOWTO, parse_mode="HTML",
                                   reply_markup=_final_keyboard())
            return True
        except Exception as e:
            log.warning("инструкция %s: %s", tg_id, e)
            return False

    for i, block in enumerate(items):
        last = (i == len(items) - 1)
        kb = _block_keyboard(block) or (_final_keyboard() if last else None)
        text = block.get("text") or ""
        file_id = block.get("file_id") or ""
        kind = block.get("kind") or "text"
        try:
            if kind == "text" or not file_id:
                if not text.strip():
                    continue
                await bot.send_message(tg_id, text, parse_mode="HTML",
                                       reply_markup=kb)
            elif kind == "photo":
                await bot.send_photo(tg_id, file_id, caption=text[:1000] or None,
                                     parse_mode="HTML", reply_markup=kb)
            elif kind == "video":
                await bot.send_video(tg_id, file_id, caption=text[:1000] or None,
                                     parse_mode="HTML", reply_markup=kb)
            elif kind == "animation":
                await bot.send_animation(tg_id, file_id, caption=text[:1000] or None,
                                         parse_mode="HTML", reply_markup=kb)
            elif kind == "video_note":
                await bot.send_video_note(tg_id, file_id)
                if text.strip():
                    await bot.send_message(tg_id, text, parse_mode="HTML",
                                           reply_markup=kb)
            elif kind == "voice":
                await bot.send_voice(tg_id, file_id, caption=text[:1000] or None,
                                     parse_mode="HTML", reply_markup=kb)
            elif kind == "document":
                await bot.send_document(tg_id, file_id, caption=text[:1000] or None,
                                        parse_mode="HTML", reply_markup=kb)
            sent = True
            await asyncio.sleep(0.5)     # чтобы Telegram не считал это флудом
        except Exception as e:
            log.warning("блок инструкции %s -> %s: %s", block.get("id"), tg_id, e)
    return sent


# ---------- Единая точка активации Премиума ----------

def _already_onboarded(tg_id: int) -> bool:
    row = db.fetchone(
        "SELECT id FROM auth_events WHERE tg_id=? AND event='premium_onboarding' "
        "LIMIT 1", (tg_id,))
    return row is not None


def mark_onboarded(tg_id: int, source: str = "") -> None:
    try:
        db.execute(
            "INSERT INTO auth_events (tg_id, event, details) VALUES (?,?,?)",
            (tg_id, "premium_onboarding", (source or "")[:200]))
    except Exception:
        pass


async def premium_activated(bot, tg_id: int, days: int = 0,
                            source: str = "", force: bool = False) -> bool:
    """Премиум выдан — что бы ни было причиной.

    Инструкцию показываем один раз: при продлении человека не заваливаем
    теми же сообщениями. Если он раньше её не видел (например, доступ выдали
    ещё до появления этой системы) — покажем при продлении.
    """
    if not tg_id:
        return False
    if not ss.get_bool("onboarding_enabled"):
        return False
    if not force and _already_onboarded(tg_id):
        return False
    ok = await send_instruction(bot, tg_id)
    if ok:
        mark_onboarded(tg_id, source)
    return ok


def schedule_premium_activated(bot, tg_id: int, days: int = 0, source: str = "") -> None:
    """Позвать инструкцию из обычного (не асинхронного) кода.

    Ручная выдача премиума случается в разных местах, часть из них — синхронные.
    Здесь мы просто ставим задачу в текущий цикл событий и не задерживаем
    того, кто выдал доступ.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(premium_activated(bot, tg_id, days, source))
