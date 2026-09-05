"""
Фоновая музыка на время прохождения тестов и Live-тестов.

Треки загружает админ. Файлы лежат рядом с базой (uploads/music), в таблице —
имя файла, название, источник и место в плейлисте. Плеер на странице теста
получает плейлист одним запросом и играет треки подряд, по кругу.

Про авторские права: платформа не умеет проверять лицензию за админа, поэтому
при загрузке он подтверждает, что трек можно использовать (royalty-free или
собственный), и указывает источник — эта пометка хранится рядом с треком.
"""
import uuid
from pathlib import Path

import config
import database as db

# Что принимаем. Браузеры уверенно играют mp3/m4a/ogg/wav.
ALLOWED_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".webm"}
MAX_TRACK_BYTES = 20 * 1024 * 1024      # 20 МБ на трек
MAX_TRACKS = 200

_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".webm": "audio/webm",
}


# Таблицу создаёт database.py при запуске. Но если сайт обновили, а
# database.py заменить забыли, страница музыки падала с 500 — поэтому
# сервис умеет создать таблицу сам. Проверка одноразовая и дешёвая.
_table_ready = False


def ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS music_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                source TEXT DEFAULT '',
                order_num INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                size_bytes INTEGER DEFAULT 0,
                uploaded_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_music_order "
                   "ON music_tracks(order_num, id)")
        _table_ready = True
    except Exception:
        pass        # база недоступна — пусть решает вызывающий код


def upload_dir() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "music"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mime_for(filename: str) -> str:
    return _MIME.get(Path(filename).suffix.lower(), "application/octet-stream")


# ---------- Общий выключатель ----------

def is_enabled() -> bool:
    """Включена ли музыка на платформе вообще. По умолчанию — выключена:
    пока админ не загрузил треки, у учеников ничего лишнего не появляется."""
    row = db.fetchone("SELECT value FROM settings WHERE key='music_enabled'")
    return bool(row and str(row["value"]) == "1")


def set_enabled(on: bool) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('music_enabled', ?)",
               ("1" if on else "0",))


# ---------- Треки ----------

def all_tracks() -> list:
    """Все треки в порядке плейлиста — для админки."""
    ensure_table()
    try:
        return [dict(r) for r in db.fetchall(
            "SELECT * FROM music_tracks ORDER BY order_num, id")]
    except Exception:
        return []


def playlist() -> list:
    """То, что реально играет у ученика: только включённые треки, по порядку."""
    if not is_enabled():
        return []
    ensure_table()
    try:
        rows = db.fetchall(
            "SELECT id, title, filename FROM music_tracks WHERE enabled=1 "
            "ORDER BY order_num, id")
    except Exception:
        return []
    return [{"id": r["id"], "title": r["title"],
             "url": f"/uploads/music/{r['filename']}"} for r in rows]


def add_track(data: bytes, original_filename: str, title: str = "",
              source: str = "", admin_id: int = None) -> tuple:
    """Сохраняет файл и запись о нём. Возвращает (ok, сообщение)."""
    ext = Path(original_filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return False, ("Формат не поддерживается. Подойдут MP3, M4A, OGG или WAV.")
    if not data:
        return False, "Файл пустой."
    if len(data) > MAX_TRACK_BYTES:
        mb = round(len(data) / 1024 / 1024, 1)
        return False, (f"Файл {mb} МБ — слишком тяжёлый. "
                       f"Максимум {MAX_TRACK_BYTES // 1024 // 1024} МБ на трек.")
    ensure_table()
    count = db.fetchone("SELECT COUNT(*) c FROM music_tracks")["c"]
    if count >= MAX_TRACKS:
        return False, (f"Достигнут предел в {MAX_TRACKS} треков — "
                       f"удалите ненужные, чтобы добавить новые.")

    new_name = f"{uuid.uuid4().hex}{ext}"
    (upload_dir() / new_name).write_bytes(data)
    nice = (title or "").strip() or Path(original_filename).stem or "Без названия"
    nxt = (db.fetchone("SELECT MAX(order_num) m FROM music_tracks")["m"] or 0) + 1
    db.execute(
        "INSERT INTO music_tracks (title, filename, source, order_num, enabled, "
        "size_bytes, uploaded_by) VALUES (?,?,?,?,1,?,?)",
        (nice[:120], new_name, (source or "").strip()[:200], nxt, len(data), admin_id))
    return True, f"Трек «{nice[:120]}» добавлен в плейлист."


def delete_track(track_id: int) -> None:
    row = db.fetchone("SELECT filename FROM music_tracks WHERE id=?", (track_id,))
    db.execute("DELETE FROM music_tracks WHERE id=?", (track_id,))
    if row:
        try:
            (upload_dir() / row["filename"]).unlink(missing_ok=True)
        except Exception:
            pass        # файла нет — запись всё равно убрали


def toggle_track(track_id: int) -> None:
    db.execute("UPDATE music_tracks SET enabled=1-enabled WHERE id=?", (track_id,))


def rename_track(track_id: int, title: str, source: str = None) -> None:
    title = (title or "").strip()[:120]
    if title:
        db.execute("UPDATE music_tracks SET title=? WHERE id=?", (title, track_id))
    if source is not None:
        db.execute("UPDATE music_tracks SET source=? WHERE id=?",
                   ((source or "").strip()[:200], track_id))


def move_track(track_id: int, direction: str) -> None:
    """Меняет трек местами с соседом. Порядок в плейлисте — это order_num."""
    rows = all_tracks()
    ids = [r["id"] for r in rows]
    if track_id not in ids:
        return
    i = ids.index(track_id)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(ids):
        return          # уже с краю
    # Переписываем порядок целиком — так он остаётся ровным 1..N даже если
    # в базе успели появиться дырки после удалений.
    ids[i], ids[j] = ids[j], ids[i]
    for pos, tid in enumerate(ids, start=1):
        db.execute("UPDATE music_tracks SET order_num=? WHERE id=?", (pos, tid))
