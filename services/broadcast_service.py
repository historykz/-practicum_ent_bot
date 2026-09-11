"""
Глобальная рассылка: любое сообщение всем пользователям бота.

Как устроено
- Админ присылает боту сообщение — текст, фото, видео, файл, голосовое,
  кружок, GIF, опрос (можно переслать пост из канала). Бот рассылает его
  копией (copy_message): форматирование, медиа и подпись сохраняются,
  пометки «переслано от» нет.
- Под сообщением — свои кнопки-ссылки (сайт, аккаунт, канал, мини-
  приложение) в несколько рядов.
- Получатели фиксируются в момент запуска (broadcast_recipients). По этой
  таблице видно, кому дошло, кто заблокировал бота, у кого удалён аккаунт,
  и по ней же сообщение удаляется у всех.
- Отправка идёт в фоне ~25 сообщений в секунду (лимит Telegram — около
  30). Перезапуск бота её не обрывает: продолжаем с того же места, и
  никто не получает сообщение дважды.

Чего Telegram не позволяет — это не обходится
- Узнать, прочитал ли человек сообщение: ботам это не сообщается. Поэтому
  считаем нажатия на кнопки-ссылки на сайты — они проходят через наш сайт
  (/r/...). Переходы в Telegram (t.me, @username, мини-приложение)
  Telegram открывает сам, мимо нас, их посчитать нельзя.
- Удалить сообщение позже 48 часов после отправки.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter,
                                TelegramServerError)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import utils

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

RATE_PER_SEC = 25            # лимит Telegram ~30 сообщений в секунду на бота
DELETE_WINDOW_HOURS = 48     # позже Telegram удалить не даёт
MAX_BUTTONS = 20
MAX_PER_ROW = 8
MAX_BUTTON_TEXT = 64
PROGRESS_EVERY = 3.0         # секунд между обновлениями прогресса у админа
BATCH = 200

# Статусы рассылки
DRAFT, SENDING, DONE, STOPPED, FAILED, DELETING, DELETED = (
    "draft", "sending", "done", "stopped", "failed", "deleting", "deleted")
BC_TITLES = {
    DRAFT: "📝 черновик", SENDING: "📤 отправляется", DONE: "✅ завершена",
    STOPPED: "⏹ остановлена", FAILED: "⚠️ прервана", DELETING: "🗑 удаляется",
    DELETED: "🗑 удалена у получателей",
}

# Статусы получателя
R_PENDING, R_SENT = "pending", "sent"
R_BLOCKED = "blocked"            # заблокировал бота
R_NOT_STARTED = "not_started"    # ни разу не нажимал «Старт» (заходил только в приложение)
R_NOT_FOUND = "not_found"        # чат не найден
R_DEACTIVATED = "deactivated"    # аккаунт удалён
R_FAILED = "failed"              # прочая ошибка
R_DELETED, R_DELETE_FAILED = "deleted", "delete_failed"
DELIVERED = (R_SENT, R_DELETED, R_DELETE_FAILED)   # до получателя дошло

KIND_TITLES = {
    "text": "текст", "photo": "фото", "video": "видео", "animation": "GIF",
    "document": "файл", "audio": "аудио", "voice": "голосовое",
    "video_note": "кружок", "sticker": "стикер", "poll": "опрос",
    "location": "геопозиция", "venue": "место", "contact": "контакт",
    "dice": "кубик",
}
_KINDS = ("photo", "video", "animation", "document", "audio", "voice",
          "video_note", "sticker", "poll", "venue", "location", "contact", "dice")


class BusyError(Exception):
    """Уже идёт другая рассылка — две сразу упёрлись бы в лимит Telegram."""
    def __init__(self, bid: int):
        super().__init__(bid)
        self.bid = bid


class _SourceGone(Exception):
    """Исходное сообщение больше нельзя скопировать — дальше слать нечего."""


async def _sleep(seconds: float) -> None:       # тесты подменяют, чтобы не ждать
    await asyncio.sleep(seconds)


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _local(iso) -> str:
    dt = utils.parse_utc_naive(iso)
    if not dt:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone(ALMATY).strftime("%d.%m.%Y %H:%M")


def _n(x) -> str:
    return f"{int(x or 0):,}".replace(",", " ")


# ───────────────────────── схема ─────────────────────────

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_tg_id INTEGER NOT NULL,
        from_chat_id INTEGER NOT NULL,
        from_message_id INTEGER NOT NULL,
        kind TEXT DEFAULT 'text',
        preview TEXT DEFAULT '',
        buttons_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        total INTEGER DEFAULT 0,
        error TEXT DEFAULT '',
        progress_chat_id INTEGER,
        progress_message_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT,
        finished_at TEXT,
        deleted_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS broadcast_recipients (
        broadcast_id INTEGER NOT NULL,
        tg_id INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',
        message_id INTEGER,
        error TEXT DEFAULT '',
        sent_at TEXT,
        PRIMARY KEY (broadcast_id, tg_id))""",
    "CREATE INDEX IF NOT EXISTS idx_bcr_status ON broadcast_recipients(broadcast_id, status)",
    """CREATE TABLE IF NOT EXISTS broadcast_clicks (
        broadcast_id INTEGER NOT NULL,
        button_idx INTEGER NOT NULL,
        tg_id INTEGER NOT NULL,
        clicks INTEGER DEFAULT 1,
        first_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (broadcast_id, button_idx, tg_id))""",
)
_ready = False


def ensure_schema() -> None:
    """Таблицы создаёт database.init_db; подстраховка для старой базы."""
    global _ready
    if _ready:
        return
    for sql in SCHEMA:
        try:
            db.execute(sql)
        except Exception as e:
            log.warning("схема рассылки: %s", e)
    _ready = True


# ───────────────────────── кнопки ─────────────────────────

_USERNAME_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{3,31})$")
_SEP_RE = re.compile(r"\s+[-–—]\s+")


def site_url() -> str:
    try:
        from webapp.learning import _site_url_sync
        return _site_url_sync()
    except Exception:
        url = (getattr(config, "SITE_URL", "") or "").strip().rstrip("/")
        return url or "https://practicumentbot-production.up.railway.app"


def app_url() -> str:
    try:
        from handlers.lesson_notes import _miniapp_url
        return _miniapp_url("")
    except Exception:
        short = (getattr(config, "WEB_APP_SHORT_NAME", "") or "").strip()
        base = f"https://t.me/{config.WEB_BOT_USERNAME}"
        return f"{base}/{short}" if short else base


def normalize_link(raw: str) -> Optional[str]:
    """Ссылка кнопки в том виде, который примет Telegram. None — не понял."""
    s = (raw or "").strip()
    low = s.lower()
    if not s or " " in s:
        return None
    if low in ("app", "приложение", "мини-приложение", "miniapp"):
        return app_url()
    if low in ("site", "сайт"):
        return site_url()
    if low.startswith(("http://", "https://")):
        host = s.split("://", 1)[1].split("/", 1)[0]
        return s if "." in host else None
    if low.startswith("tg://"):
        return s
    if low.startswith(("t.me/", "telegram.me/")):
        return "https://" + s
    m = _USERNAME_RE.match(s)
    if m:
        return f"https://t.me/{m.group(1)}"
    if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", low):   # example.com/...
        return "https://" + s
    return None


def parse_buttons(text: str) -> tuple:
    """Кнопки из текста админа → (ряды, ошибки).

    Строка — ряд. В одном ряду кнопки через « | ». Кнопка: «Текст - ссылка».
    Ссылка: https://…, t.me/…, @username, «app» (мини-приложение), «сайт».
    """
    rows, errors, count = [], [], 0
    for n, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        row = []
        for part in [p.strip() for p in line.split("|") if p.strip()]:
            bits = _SEP_RE.split(part)
            if len(bits) < 2:
                errors.append(f"строка {n}: «{part[:40]}» — нужно «Текст - ссылка»")
                continue
            label = " - ".join(b.strip() for b in bits[:-1]).strip()
            link = normalize_link(bits[-1])
            if not label:
                errors.append(f"строка {n}: у кнопки нет текста")
            elif len(label) > MAX_BUTTON_TEXT:
                errors.append(f"строка {n}: текст «{label[:24]}…» длиннее {MAX_BUTTON_TEXT} символов")
            elif not link:
                errors.append(f"строка {n}: не понял ссылку «{bits[-1].strip()[:40]}»")
            else:
                row.append({"text": label, "url": link})
        if len(row) > MAX_PER_ROW:
            errors.append(f"строка {n}: больше {MAX_PER_ROW} кнопок в одном ряду")
            continue
        if row:
            rows.append(row)
            count += len(row)
    if count > MAX_BUTTONS:
        errors.append(f"кнопок {count}, а можно не больше {MAX_BUTTONS}")
    return rows, errors


def _secret() -> bytes:
    try:
        from webapp.server import _session_secret_bytes
        base = _session_secret_bytes()
    except Exception:
        base = hashlib.sha256(("smartent-session::" + config.BOT_TOKEN).encode()).digest()
    return hashlib.sha256(b"broadcast-click::" + base).digest()


def click_sig(bid: int, idx: int, tg_id: int) -> str:
    return hmac.new(_secret(), f"{int(bid)}:{int(idx)}:{int(tg_id)}".encode(),
                    hashlib.sha256).hexdigest()[:16]


def trackable(url: str) -> bool:
    """Ссылку на сайт можно провести через счётчик. Telegram-ссылки — нет:
    их Telegram открывает сам, лишний переход через браузер только мешает."""
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    host = low.split("://", 1)[1].split("/", 1)[0]
    return host not in ("t.me", "www.t.me", "telegram.me") and not host.endswith(".t.me")


def markup_for(bid: int, rows: list, tg_id: int = None,
               track: bool = True) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура для конкретного получателя (у каждого своя ссылка-счётчик)."""
    if not rows:
        return None
    base = site_url() if (track and tg_id) else ""
    kb, idx = [], 0
    for row in rows:
        out = []
        for b in row:
            url = b["url"]
            if base and trackable(url):
                url = f"{base}/r/{int(bid)}/{idx}/{int(tg_id)}/{click_sig(bid, idx, tg_id)}"
            out.append(InlineKeyboardButton(text=b["text"], url=url))
            idx += 1
        kb.append(out)
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ───────────────────────── черновик ─────────────────────────

def message_kind(message) -> Optional[str]:
    """Что за сообщение. None — Telegram его не скопирует."""
    for k in _KINDS:
        if getattr(message, k, None):
            return k
    if (getattr(message, "text", None) or "").strip():
        return "text"
    return None


def create(admin_tg_id: int, from_chat_id: int, from_message_id: int,
           kind: str, preview: str) -> int:
    ensure_schema()
    db.execute(
        "INSERT INTO broadcasts (admin_tg_id, from_chat_id, from_message_id, kind, preview) "
        "VALUES (?,?,?,?,?)",
        (admin_tg_id, from_chat_id, from_message_id, kind or "text", (preview or "")[:300]))
    return db.fetchone("SELECT last_insert_rowid() AS id")["id"]


def get(bid: int) -> Optional[dict]:
    ensure_schema()
    row = db.fetchone("SELECT * FROM broadcasts WHERE id=?", (bid,))
    return dict(row) if row else None


def rows_of(b: dict) -> list:
    try:
        return json.loads((b or {}).get("buttons_json") or "[]")
    except (ValueError, TypeError):
        return []


def set_buttons(bid: int, rows: list) -> None:
    db.execute("UPDATE broadcasts SET buttons_json=? WHERE id=? AND status=?",
               (json.dumps(rows or [], ensure_ascii=False), bid, DRAFT))


def set_progress_message(bid: int, chat_id: int, message_id: int) -> None:
    db.execute("UPDATE broadcasts SET progress_chat_id=?, progress_message_id=? WHERE id=?",
               (chat_id, message_id, bid))


def delete_draft(bid: int) -> bool:
    cur = db.execute("DELETE FROM broadcasts WHERE id=? AND status=?", (bid, DRAFT))
    return cur.rowcount == 1


def button_at(bid: int, idx: int) -> Optional[dict]:
    flat = [b for row in rows_of(get(bid)) for b in row]
    return flat[idx] if 0 <= idx < len(flat) else None


def record_click(bid: int, idx: int, tg_id: int) -> None:
    db.execute(
        "INSERT INTO broadcast_clicks (broadcast_id, button_idx, tg_id) VALUES (?,?,?) "
        "ON CONFLICT(broadcast_id, button_idx, tg_id) DO UPDATE SET "
        "clicks=clicks+1, last_at=CURRENT_TIMESTAMP", (bid, idx, tg_id))


def audience() -> dict:
    """Кто получит рассылку прямо сейчас."""
    ensure_schema()
    c = lambda sql: (db.fetchone(sql) or {"c": 0})["c"]
    total = c("SELECT COUNT(*) AS c FROM users WHERE tg_id IS NOT NULL AND tg_id > 0")
    banned = c("SELECT COUNT(*) AS c FROM users WHERE tg_id > 0 AND COALESCE(is_blocked,0)=1")
    blocked = c("SELECT COUNT(*) AS c FROM users WHERE tg_id > 0 AND COALESCE(is_blocked,0)=0 "
                "AND COALESCE(bot_blocked,0)=1")
    return {"all": total, "banned": banned, "known_blocked": blocked, "targets": total - banned}


def active() -> Optional[dict]:
    ensure_schema()
    row = db.fetchone("SELECT * FROM broadcasts WHERE status IN (?,?) ORDER BY id DESC LIMIT 1",
                      (SENDING, DELETING))
    return dict(row) if row else None


def recent(limit: int = 15) -> list:
    ensure_schema()
    rows = [dict(r) for r in db.fetchall(
        "SELECT * FROM broadcasts WHERE status<>? ORDER BY id DESC LIMIT ?", (DRAFT, limit))]
    if rows:
        ph = ",".join("?" * len(rows))
        got = {r["broadcast_id"]: r["c"] for r in db.fetchall(
            f"SELECT broadcast_id, COUNT(*) AS c FROM broadcast_recipients "
            f"WHERE broadcast_id IN ({ph}) AND status IN (?,?,?) GROUP BY broadcast_id",
            tuple(r["id"] for r in rows) + DELIVERED)}
        for r in rows:
            r["delivered"] = got.get(r["id"], 0)
    return rows


# ───────────────────────── запуск и остановка ─────────────────────────

_tasks: dict = {}          # id рассылки -> asyncio.Task (отправка или удаление)
_stop: set = set()


def start(bid: int) -> int:
    """Зафиксировать получателей и включить отправку. Число получателей;
    0 — рассылка уже запущена (повторное нажатие) или некому слать."""
    ensure_schema()
    other = db.fetchone("SELECT id FROM broadcasts WHERE status IN (?,?) AND id<>?",
                        (SENDING, DELETING, bid))
    if other:
        raise BusyError(other["id"])
    cur = db.execute("UPDATE broadcasts SET status=?, started_at=? WHERE id=? AND status=?",
                     (SENDING, _now_iso(), bid, DRAFT))
    if cur.rowcount != 1:
        return 0
    # Все, кто есть в базе: нажимали «Старт» или заходили в приложение.
    # Забаненным админом не шлём — бот их и так не обслуживает.
    db.execute(
        "INSERT OR IGNORE INTO broadcast_recipients (broadcast_id, tg_id) "
        "SELECT ?, tg_id FROM users WHERE tg_id IS NOT NULL AND tg_id > 0 "
        "AND COALESCE(is_blocked,0)=0", (bid,))
    total = db.fetchone("SELECT COUNT(*) AS c FROM broadcast_recipients WHERE broadcast_id=?",
                        (bid,))["c"]
    db.execute("UPDATE broadcasts SET total=? WHERE id=?", (total, bid))
    if not total:
        db.execute("UPDATE broadcasts SET status=?, finished_at=? WHERE id=?",
                   (DONE, _now_iso(), bid))
    return total


def is_running(bid: int) -> bool:
    t = _tasks.get(bid)
    return bool(t and not t.done())


def launch(bot, bid: int, what: str = "send") -> bool:
    """Запустить фоновую отправку или удаление. Второй раз не запускается."""
    if is_running(bid):
        return False
    coro = run_send(bot, bid) if what == "send" else run_delete(bot, bid)
    _tasks[bid] = asyncio.create_task(_guarded(coro, bid))
    return True


async def _guarded(coro, bid: int) -> None:
    try:
        await coro
    except Exception as e:
        # Статус остаётся «отправляется»: после перезапуска или по кнопке
        # «Продолжить» рассылка пойдёт дальше с того же места.
        log.exception("рассылка №%s прервалась: %s", bid, e)


def request_stop(bid: int) -> bool:
    _stop.add(bid)
    cur = db.execute("UPDATE broadcasts SET status=?, finished_at=? WHERE id=? AND status=?",
                     (STOPPED, _now_iso(), bid, SENDING))
    return cur.rowcount == 1


# ───────────────────────── отправка ─────────────────────────

async def _send_one(bot, b: dict, rows: list, tg_id: int) -> tuple:
    """(статус, id сообщения, текст ошибки) для одного получателя."""
    err = ""
    for attempt in range(5):
        try:
            m = await bot.copy_message(
                chat_id=tg_id, from_chat_id=b["from_chat_id"],
                message_id=b["from_message_id"],
                reply_markup=markup_for(b["id"], rows, tg_id))
            return R_SENT, m.message_id, ""
        except TelegramRetryAfter as e:
            # Telegram просит подождать — ждём и повторяем тому же человеку
            await _sleep(float(getattr(e, "retry_after", 1) or 1) + 0.5)
            err = "Telegram просил подождать"
        except TelegramForbiddenError as e:
            msg = str(e).lower()
            if "deactivated" in msg:
                return R_DEACTIVATED, None, msg[:300]
            if "initiate" in msg:
                return R_NOT_STARTED, None, msg[:300]
            return R_BLOCKED, None, msg[:300]
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if ("message to copy not found" in msg or "message_id_invalid" in msg
                    or "button_url_invalid" in msg):
                raise _SourceGone(msg)
            if "chat not found" in msg or "user not found" in msg or "peer_id_invalid" in msg:
                return R_NOT_FOUND, None, msg[:300]
            return R_FAILED, None, msg[:300]
        except (TelegramNetworkError, TelegramServerError) as e:
            err = str(e)
            await _sleep(2.0 * (attempt + 1))
        except Exception as e:
            return R_FAILED, None, repr(e)[:300]
    return R_FAILED, None, (err or "не удалось после повторов")[:300]


def _mark(bid: int, tg_id: int, status: str, message_id, error: str) -> None:
    db.execute(
        "UPDATE broadcast_recipients SET status=?, message_id=?, error=?, sent_at=? "
        "WHERE broadcast_id=? AND tg_id=?",
        (status, message_id, (error or "")[:300], _now_iso(), bid, tg_id))
    # Тот же флаг, что ведут автонапоминания (reminder_service.mark_blocked):
    # дошло — значит бот доступен; «заблокировал» / «удалён» — недоступен.
    # «Не нажимал Старт» флаг не трогает: иначе после /start человек так и
    # остался бы для напоминаний «заблокировавшим».
    if status == R_SENT:
        db.execute("UPDATE users SET bot_blocked=0 WHERE tg_id=? AND COALESCE(bot_blocked,0)=1",
                   (tg_id,))
    elif status in (R_BLOCKED, R_DEACTIVATED):
        db.execute("UPDATE users SET bot_blocked=1 WHERE tg_id=?", (tg_id,))


def _pending(bid: int, status: str = R_PENDING) -> list:
    return db.fetchall(
        "SELECT tg_id, message_id, sent_at FROM broadcast_recipients "
        "WHERE broadcast_id=? AND status=? ORDER BY rowid LIMIT ?", (bid, status, BATCH))


async def run_send(bot, bid: int) -> None:
    ensure_schema()
    b = get(bid)
    if not b or b["status"] != SENDING:
        return
    rows = rows_of(b)
    interval = 1.0 / max(1, RATE_PER_SEC)
    last_progress = 0.0
    handled = 0
    stopped = False
    _stop.discard(bid)
    try:
        while not stopped:
            batch = await asyncio.to_thread(_pending, bid)
            if not batch:
                break
            for r in batch:
                if bid in _stop:
                    stopped = True
                    break
                handled += 1
                if handled % 50 == 0:
                    st = await asyncio.to_thread(get, bid)
                    if not st or st["status"] != SENDING:
                        stopped = True
                        break
                status, mid, err = await _send_one(bot, b, rows, r["tg_id"])
                await asyncio.to_thread(_mark, bid, r["tg_id"], status, mid, err)
                if time.monotonic() - last_progress >= PROGRESS_EVERY:
                    last_progress = time.monotonic()
                    await _update_progress(bot, bid)
                await _sleep(interval)
    except _SourceGone as e:
        log.warning("рассылка №%s: исходное сообщение не копируется: %s", bid, e)
        db.execute("UPDATE broadcasts SET status=?, error=?, finished_at=? WHERE id=?",
                   (FAILED, "Исходное сообщение удалено из чата с ботом или кнопка "
                    "с неверной ссылкой — Telegram не даёт его копировать. "
                    "Остальным не отправлено.", _now_iso(), bid))
        await _update_progress(bot, bid)
        await _notify_admin(bot, bid)
        return
    finally:
        _stop.discard(bid)
    final = STOPPED if stopped else DONE
    db.execute("UPDATE broadcasts SET status=?, finished_at=? WHERE id=? AND status IN (?,?)",
               (final, _now_iso(), bid, SENDING, STOPPED))
    await _update_progress(bot, bid)
    await _notify_admin(bot, bid)


# ───────────────────────── удаление у всех ─────────────────────────

async def prepare_delete(bid: int) -> str:
    """Перевести рассылку в «удаляется». Идущую отправку сначала останавливаем.
    'ok' | 'busy' | 'bad_status' | 'not_found'."""
    b = get(bid)
    if not b:
        return "not_found"
    if b["status"] == SENDING:
        request_stop(bid)
        t = _tasks.get(bid)
        if t and not t.done():
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=60)
            except Exception:
                pass
    other = db.fetchone("SELECT id FROM broadcasts WHERE status IN (?,?) AND id<>?",
                        (SENDING, DELETING, bid))
    if other:
        return "busy"
    cur = db.execute("UPDATE broadcasts SET status=? WHERE id=? AND status IN (?,?,?)",
                     (DELETING, bid, DONE, STOPPED, FAILED))
    return "ok" if cur.rowcount == 1 else "bad_status"


async def _delete_one(bot, tg_id: int, message_id: int) -> tuple:
    for attempt in range(5):
        try:
            await bot.delete_message(chat_id=tg_id, message_id=message_id)
            return R_DELETED, ""
        except TelegramRetryAfter as e:
            await _sleep(float(getattr(e, "retry_after", 1) or 1) + 0.5)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "not found" in msg:
                return R_DELETED, "уже удалено у получателя"
            return R_DELETE_FAILED, msg[:300]
        except TelegramForbiddenError as e:
            return R_DELETE_FAILED, str(e)[:300]
        except (TelegramNetworkError, TelegramServerError):
            await _sleep(2.0 * (attempt + 1))
        except Exception as e:
            return R_DELETE_FAILED, repr(e)[:300]
    return R_DELETE_FAILED, "не удалось после повторов"


def _mark_deleted(bid: int, tg_id: int, status: str, error: str) -> None:
    db.execute("UPDATE broadcast_recipients SET status=?, error=? WHERE broadcast_id=? AND tg_id=?",
               (status, (error or "")[:300], bid, tg_id))


async def run_delete(bot, bid: int) -> None:
    ensure_schema()
    b = get(bid)
    if not b or b["status"] != DELETING:
        return
    interval = 1.0 / max(1, RATE_PER_SEC)
    cutoff = datetime.utcnow() - timedelta(hours=DELETE_WINDOW_HOURS)
    last_progress = 0.0
    while True:
        batch = await asyncio.to_thread(_pending, bid, R_SENT)
        if not batch:
            break
        for r in batch:
            sent_at = utils.parse_utc_naive(r["sent_at"])
            if not r["message_id"]:
                status, err = R_DELETE_FAILED, "нет номера сообщения"
            elif sent_at and sent_at < cutoff:
                status, err = R_DELETE_FAILED, "старше 48 часов — Telegram не даёт удалить"
            else:
                status, err = await _delete_one(bot, r["tg_id"], r["message_id"])
                await _sleep(interval)
            await asyncio.to_thread(_mark_deleted, bid, r["tg_id"], status, err)
            if time.monotonic() - last_progress >= PROGRESS_EVERY:
                last_progress = time.monotonic()
                await _update_progress(bot, bid)
    db.execute("UPDATE broadcasts SET status=?, deleted_at=? WHERE id=?",
               (DELETED, _now_iso(), bid))
    await _update_progress(bot, bid)
    await _notify_admin(bot, bid)


async def resume_all(bot, delay: float = 20) -> None:
    """После перезапуска: продолжить прерванные отправку и удаление."""
    await _sleep(delay)
    try:
        ensure_schema()
        for r in db.fetchall("SELECT id, status FROM broadcasts WHERE status IN (?,?)",
                             (SENDING, DELETING)):
            if launch(bot, r["id"], "send" if r["status"] == SENDING else "delete"):
                log.info("рассылка №%s продолжена после перезапуска (%s)", r["id"], r["status"])
    except Exception as e:
        log.warning("продолжение рассылок: %s", e)


# ───────────────────────── статистика ─────────────────────────

def stats(bid: int) -> dict:
    b = get(bid)
    if not b:
        return {}
    by = {r["status"]: r["c"] for r in db.fetchall(
        "SELECT status, COUNT(*) AS c FROM broadcast_recipients WHERE broadcast_id=? "
        "GROUP BY status", (bid,))}
    first = db.fetchone(
        "SELECT MIN(sent_at) AS m FROM broadcast_recipients WHERE broadcast_id=? "
        "AND status IN (?,?,?)", (bid,) + DELIVERED)
    flat = [x for row in rows_of(b) for x in row]
    clicks = {r["button_idx"]: (r["people"], r["total"]) for r in db.fetchall(
        "SELECT button_idx, COUNT(*) AS people, SUM(clicks) AS total FROM broadcast_clicks "
        "WHERE broadcast_id=? GROUP BY button_idx", (bid,))}
    buttons = []
    for i, btn in enumerate(flat):
        people, total = clicks.get(i, (0, 0))
        buttons.append({"text": btn["text"], "url": btn["url"], "tracked": trackable(btn["url"]),
                        "people": people or 0, "clicks": total or 0})
    delivered = sum(by.get(s, 0) for s in DELIVERED)
    total = b.get("total") or sum(by.values())
    first_dt = utils.parse_utc_naive(first["m"]) if first and first["m"] else None
    delete_until = first_dt + timedelta(hours=DELETE_WINDOW_HOURS) if first_dt else None
    return {
        "b": b, "total": total, "by": by, "delivered": delivered,
        "pending": by.get(R_PENDING, 0), "blocked": by.get(R_BLOCKED, 0),
        "not_started": by.get(R_NOT_STARTED, 0) + by.get(R_NOT_FOUND, 0),
        "deactivated": by.get(R_DEACTIVATED, 0), "failed": by.get(R_FAILED, 0),
        "deleted": by.get(R_DELETED, 0), "delete_failed": by.get(R_DELETE_FAILED, 0),
        "buttons": buttons, "delete_until": delete_until,
        "can_delete": bool(delivered - by.get(R_DELETED, 0) - by.get(R_DELETE_FAILED, 0) > 0
                           and delete_until and delete_until > datetime.utcnow()
                           and b["status"] in (DONE, STOPPED, FAILED, SENDING)),
    }


def _bar(pct: float) -> str:
    full = int(round(pct / 10))
    return "█" * full + "░" * (10 - full)


def progress_text(bid: int) -> str:
    s = stats(bid)
    if not s:
        return "Рассылка не найдена."
    b, total = s["b"], s["total"] or 0
    lines = [f"📣 <b>Рассылка №{bid}</b> — {BC_TITLES.get(b['status'], b['status'])}", ""]
    if b["status"] in (DELETING, DELETED):
        base = s["delivered"] or 0
        done = s["deleted"] + s["delete_failed"]
        pct = round(done * 100 / base) if base else 100
        lines.append(f"{_bar(pct)} {pct}%")
        lines.append(f"🗑 Удалено у получателей: <b>{_n(s['deleted'])}</b> из {_n(base)}")
        if s["delete_failed"]:
            lines.append(f"⚠️ Не удалось удалить: {_n(s['delete_failed'])}")
    else:
        done = total - s["pending"]
        pct = round(done * 100 / total) if total else 100
        lines.append(f"{_bar(pct)} {pct}%")
        lines.append(f"Обработано: <b>{_n(done)}</b> из {_n(total)}")
        lines.append(f"✅ Доставлено: <b>{_n(s['delivered'])}</b>")
        if s["blocked"]:
            lines.append(f"⛔️ Заблокировали бота: {_n(s['blocked'])}")
        if s["not_started"]:
            lines.append(f"🚫 Не запускали бота: {_n(s['not_started'])}")
        if s["deactivated"]:
            lines.append(f"🗑 Аккаунт удалён: {_n(s['deactivated'])}")
        if s["failed"]:
            lines.append(f"⚠️ Ошибки: {_n(s['failed'])}")
    if b.get("error"):
        lines += ["", f"⚠️ {utils.escape_html(b['error'])}"]
    return "\n".join(lines)


def progress_kb(bid: int) -> InlineKeyboardMarkup:
    b = get(bid) or {}
    row = []
    if b.get("status") == SENDING:
        row.append(InlineKeyboardButton(text="⏹ Остановить", callback_data=f"bc:stop:{bid}"))
    row.append(InlineKeyboardButton(text="📊 Статистика", callback_data=f"bc:st:{bid}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def stats_text(bid: int) -> str:
    s = stats(bid)
    if not s:
        return "Рассылка не найдена."
    b, total = s["b"], s["total"] or 0
    pct = lambda x: f" ({round(x * 100 / total)}%)" if total else ""
    lines = [
        f"📊 <b>Рассылка №{bid}</b> · {_local(b.get('started_at') or b.get('created_at'))} (Астана)",
        f"Статус: {BC_TITLES.get(b['status'], b['status'])} · {KIND_TITLES.get(b['kind'], b['kind'])}",
    ]
    if b.get("preview"):
        lines.append(f"<i>«{utils.escape_html(b['preview'][:120])}"
                     f"{'…' if len(b['preview']) > 120 else ''}»</i>")
    lines += [
        "",
        f"👥 Получателей: <b>{_n(total)}</b>",
        f"✅ Доставлено: <b>{_n(s['delivered'])}</b>{pct(s['delivered'])}",
        f"⛔️ Заблокировали бота: <b>{_n(s['blocked'])}</b>{pct(s['blocked'])}",
        f"🚫 Не запускали бота / чат не найден: <b>{_n(s['not_started'])}</b>",
        f"🗑 Аккаунт удалён: <b>{_n(s['deactivated'])}</b>",
        f"⚠️ Другие ошибки: <b>{_n(s['failed'])}</b>",
    ]
    if s["pending"]:
        lines.append(f"⏳ Ещё в очереди: <b>{_n(s['pending'])}</b>")
    if s["buttons"]:
        lines += ["", "🔘 <b>Нажатия на кнопки</b> (уникальных людей):"]
        for btn in s["buttons"]:
            name = utils.escape_html(btn["text"])
            if btn["tracked"]:
                share = (f" — {round(btn['people'] * 100 / s['delivered'])}% доставленных"
                         if s["delivered"] else "")
                lines.append(f"• {name}: <b>{_n(btn['people'])}</b>{share}"
                             + (f" (всего нажатий {_n(btn['clicks'])})"
                                if btn["clicks"] > btn["people"] else ""))
            else:
                lines.append(f"• {name}: не считается — ссылка в Telegram")
    lines += ["", "👀 <i>Прочитали ли — Telegram ботам не сообщает. Нажатия на "
                  "кнопки выше — точная цифра интереса.</i>"]
    if s["deleted"] or s["delete_failed"] or b["status"] in (DELETING, DELETED):
        lines.append(f"🗑 Удалено у получателей: <b>{_n(s['deleted'])}</b>"
                     + (f" · не удалось: {_n(s['delete_failed'])}" if s["delete_failed"] else ""))
    elif s["delete_until"]:
        left = s["delete_until"] - datetime.utcnow()
        if left.total_seconds() > 0:
            hours = int(left.total_seconds() // 3600)
            lines.append(f"🗑 Удалить у всех можно ещё {hours} ч — до {_local(s['delete_until'])}")
        else:
            lines.append("🗑 Удалить уже нельзя: прошло больше 48 часов")
    if b.get("error"):
        lines += ["", f"⚠️ {utils.escape_html(b['error'])}"]
    return "\n".join(lines)


async def _update_progress(bot, bid: int) -> None:
    b = await asyncio.to_thread(get, bid)
    if not b or not b.get("progress_chat_id") or not b.get("progress_message_id"):
        return
    try:
        text = await asyncio.to_thread(progress_text, bid)
        kb = await asyncio.to_thread(progress_kb, bid)
        await bot.edit_message_text(text, chat_id=b["progress_chat_id"],
                                    message_id=b["progress_message_id"],
                                    reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("прогресс рассылки №%s: %s", bid, e)
    except Exception as e:
        log.debug("прогресс рассылки №%s: %s", bid, e)


async def _notify_admin(bot, bid: int) -> None:
    """Отдельное сообщение по окончании: правка прогресса уведомление не шлёт."""
    s = await asyncio.to_thread(stats, bid)
    if not s:
        return
    b = s["b"]
    if b["status"] == DELETED:
        text = (f"🗑 Рассылка №{bid} удалена у получателей: {_n(s['deleted'])}"
                + (f", не удалось — {_n(s['delete_failed'])}" if s["delete_failed"] else ""))
    elif b["status"] == FAILED:
        text = f"⚠️ Рассылка №{bid} прервана. Доставлено: {_n(s['delivered'])} из {_n(s['total'])}."
    else:
        text = (f"{'⏹' if b['status'] == STOPPED else '✅'} Рассылка №{bid} "
                f"{'остановлена' if b['status'] == STOPPED else 'завершена'}. "
                f"Доставлено: {_n(s['delivered'])} из {_n(s['total'])}, "
                f"заблокировали бота: {_n(s['blocked'])}.")
    try:
        await bot.send_message(b["admin_tg_id"], text, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📊 Статистика",
                                                   callback_data=f"bc:st:{bid}")]]))
    except Exception as e:
        log.debug("уведомление о рассылке №%s: %s", bid, e)
