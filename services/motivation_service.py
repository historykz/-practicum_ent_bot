"""
Мотивационные материалы: текст, фото, видео, гиф, кружок, голосовое, файл.

Админ загружает их пачкой, бот подмешивает к напоминаниям. Главное правило —
не показывать человеку одно и то же слишком часто: сначала идут материалы,
которых он ещё не видел, а уже потом по кругу, начиная с самых давних.
"""
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import database as db
from services import study_settings as ss

log = logging.getLogger(__name__)

KINDS = ("text", "photo", "video", "animation", "video_note", "voice", "document")

KIND_TITLES = {
    "text": "Текст",
    "photo": "Фото",
    "video": "Видео",
    "animation": "GIF",
    "video_note": "Кружок",
    "voice": "Голосовое",
    "document": "Файл",
}

_ready = False


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS motivations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'text', file_id TEXT DEFAULT '',
            text TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0, created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""CREATE TABLE IF NOT EXISTS motivation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL,
            motivation_id INTEGER NOT NULL,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_motivation_log "
                   "ON motivation_log(tg_id, sent_at DESC)")
        _ready = True
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Управление материалами ----------

def add(kind: str, file_id: str = "", text: str = "", admin_id: int = None) -> int:
    ensure_schema()
    kind = kind if kind in KINDS else "text"
    nxt = (db.fetchone("SELECT MAX(sort_order) AS m FROM motivations")["m"] or 0) + 1
    db.execute(
        "INSERT INTO motivations (kind, file_id, text, sort_order, created_by) "
        "VALUES (?,?,?,?,?)",
        (kind, (file_id or "")[:300], (text or "")[:1000], nxt, admin_id))
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def all_items(only_enabled: bool = False) -> list:
    ensure_schema()
    sql = "SELECT * FROM motivations"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_order, id"
    try:
        return [dict(r) for r in db.fetchall(sql)]
    except Exception:
        return []


def get(item_id: int) -> Optional[dict]:
    ensure_schema()
    row = db.fetchone("SELECT * FROM motivations WHERE id=?", (item_id,))
    return dict(row) if row else None


def delete(item_id: int) -> None:
    ensure_schema()
    db.execute("DELETE FROM motivations WHERE id=?", (item_id,))
    db.execute("DELETE FROM motivation_log WHERE motivation_id=?", (item_id,))


def toggle(item_id: int) -> None:
    ensure_schema()
    db.execute("UPDATE motivations SET enabled=1-enabled WHERE id=?", (item_id,))


def clear_all() -> int:
    ensure_schema()
    n = len(all_items())
    db.execute("DELETE FROM motivations")
    db.execute("DELETE FROM motivation_log")
    return n


# ---------- Выбор материала для конкретного ученика ----------

def pick_for(tg_id: int) -> Optional[dict]:
    """Что отправить этому человеку.

    Сначала — то, чего он вообще не видел. Если всё видел, берём самое давнее,
    но только если прошло больше заданного числа дней. Так одна и та же
    картинка не прилетает второй раз через сутки.
    """
    ensure_schema()
    items = all_items(only_enabled=True)
    if not items:
        return None

    repeat_days = ss.get_int("study_motivation_repeat_days", 14)
    seen = {}
    try:
        rows = db.fetchall(
            "SELECT motivation_id, MAX(sent_at) AS last FROM motivation_log "
            "WHERE tg_id=? GROUP BY motivation_id", (tg_id,))
        seen = {r["motivation_id"]: r["last"] for r in rows}
    except Exception:
        seen = {}

    fresh = [i for i in items if i["id"] not in seen]
    if fresh:
        return random.choice(fresh)

    # Всё уже видел — берём то, что было давнее всего
    def _age(item):
        raw = seen.get(item["id"]) or ""
        try:
            dt = datetime.fromisoformat(str(raw).replace(" ", "T"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return 10 ** 6
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400

    items.sort(key=_age, reverse=True)
    oldest = items[0]
    if _age(oldest) < repeat_days:
        return None            # свежее нельзя — лучше вообще без мотивации
    return oldest


def mark_sent(tg_id: int, motivation_id: int) -> None:
    ensure_schema()
    try:
        db.execute("INSERT INTO motivation_log (tg_id, motivation_id, sent_at) "
                   "VALUES (?,?,?)", (tg_id, motivation_id, _now_iso()))
    except Exception:
        pass


async def send(bot, tg_id: int, item: dict) -> bool:
    """Отправить материал тем способом, каким он загружен."""
    kind = item.get("kind") or "text"
    text = item.get("text") or ""
    file_id = item.get("file_id") or ""
    try:
        if kind == "text" or not file_id:
            if not text.strip():
                return False
            await bot.send_message(tg_id, text)
        elif kind == "photo":
            await bot.send_photo(tg_id, file_id, caption=text[:1000] or None)
        elif kind == "video":
            await bot.send_video(tg_id, file_id, caption=text[:1000] or None)
        elif kind == "animation":
            await bot.send_animation(tg_id, file_id, caption=text[:1000] or None)
        elif kind == "video_note":
            await bot.send_video_note(tg_id, file_id)
            if text.strip():
                await bot.send_message(tg_id, text)
        elif kind == "voice":
            await bot.send_voice(tg_id, file_id, caption=text[:1000] or None)
        elif kind == "document":
            await bot.send_document(tg_id, file_id, caption=text[:1000] or None)
        else:
            return False
    except Exception as e:
        log.warning("мотивация %s -> %s: %s", item.get("id"), tg_id, e)
        return False
    mark_sent(tg_id, item["id"])
    return True


# ---------- Импорт из файла ----------

TEMPLATE_TEXT = """# Мотивашки для Smart ENT — шаблон импорта
#
# Как пользоваться:
# 1. Каждая мотивашка — с новой строки, строки с # бот пропускает.
# 2. Простой вариант: просто напишите текст мотивашки одной строкой.
# 3. Чтобы прислать несколько строк одной мотивашкой, замените переносы на \\n
#
# Пример простых текстовых мотивашек:
Каждый закрытый конспект — это плюс балл на ЕНТ. Продолжай.
Не нужно учить всё сразу. Нужно учить каждый день понемногу.
Сегодняшняя лень — это завтрашний недобранный балл.
Ты уже начал. Самое сложное позади.
Один разобранный вопрос сегодня лучше, чем сто «завтра».
#
# ФОТО, ВИДЕО, ГИФ, КРУЖОК, ГОЛОСОВОЕ
# Их проще загрузить прямо в бота: «🔥 Мотивация» → «Добавить» → пришлите файл.
# Бот сам поймёт тип и сохранит.
#
# ЕСЛИ ХОТИТЕ СГЕНЕРИРОВАТЬ ТЕКСТЫ НЕЙРОСЕТЬЮ — вот готовый запрос:
#
# «Напиши 20 коротких мотивационных сообщений для казахстанских школьников,
#  которые готовятся к ЕНТ. Требования: 1-2 предложения, без пафоса и без
#  обесценивания, дружелюбно, можно с лёгким юмором, обращение на "ты",
#  можно один эмодзи. Каждое сообщение — с новой строки, без нумерации.
#  Темы: не бросать подготовку, заниматься каждый день понемногу, вернуться
#  после пропуска, доделать домашнее задание, вера в себя.»
#
# Ответ нейросети вставьте сюда, сохраните файл и загрузите его в бота.
"""


def parse_import(raw: str) -> tuple:
    """Разбирает файл импорта. Возвращает (список текстов, сколько пропущено)."""
    if not raw:
        return [], 0
    lines, skipped = [], 0
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            skipped += 1
            continue
        lines.append(line.replace("\\n", "\n")[:1000])
    return lines, skipped


def import_texts(texts: list, admin_id: int = None) -> int:
    ensure_schema()
    n = 0
    for text in texts:
        if text.strip():
            add("text", "", text, admin_id)
            n += 1
    return n
