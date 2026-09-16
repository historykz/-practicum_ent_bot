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


async def send_instruction(bot, tg_id: int, source: str = "") -> bool:
    """Показать инструкцию. Тем же вызовом работает кнопка «посмотреть ещё раз».

    Блоки идут строго по порядку админа. Ошибка одного блока (протухший
    file_id, слишком длинная подпись) пишется в лог, а остальные блоки всё
    равно отправляются — человек не должен остаться без инструкции целиком
    из-за одной картинки.
    """
    ensure_schema()
    items = blocks(only_enabled=True)
    sent = False
    tag = f"tg_id={tg_id} source={source or '-'}"

    if not items:
        # Админ ещё не собрал свою инструкцию — показываем базовую,
        # чтобы человек в любом случае знал, что делать.
        try:
            await bot.send_message(tg_id, DEFAULT_INTRO, parse_mode="HTML")
            await asyncio.sleep(0.4)
            await bot.send_message(tg_id, DEFAULT_HOWTO, parse_mode="HTML",
                                   reply_markup=_final_keyboard())
            log.info("premium onboarding [%s]: блоков нет, отправлена базовая инструкция", tag)
            return True
        except Exception as e:
            log.warning("premium onboarding [%s]: базовая инструкция не ушла: %s", tag, e)
            return False

    ok_n = 0
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
            ok_n += 1
            log.info("premium onboarding [%s]: блок %d/%d (%s, id=%s) отправлен",
                     tag, i + 1, len(items), kind, block.get("id"))
            await asyncio.sleep(0.5)     # чтобы Telegram не считал это флудом
        except Exception as e:
            log.warning("premium onboarding [%s]: блок %d/%d (%s, id=%s) НЕ отправлен: %s",
                        tag, i + 1, len(items), kind, block.get("id"), e)
    log.info("premium onboarding [%s]: отправлено блоков %d из %d", tag, ok_n, len(items))
    return sent


# ---------- Единая точка активации Премиума ----------

# После каких способов выдачи инструкцию НЕ шлём. Награда за друзей — бонус
# тому, кто платформой уже пользуется; ему хватает сообщения о награде.
NO_INSTRUCTION_SOURCES = {"referral"}

# Одно и то же событие может дойти до нас дважды (двойной клик админа,
# повторное подтверждение платежа от Telegram). Без ключа события повтор
# в этом окне считаем дублем.
DUPLICATE_WINDOW_SECONDS = 90


def _event_details(source: str, event_key) -> str:
    base = (source or "")[:100]
    return f"{base}#{str(event_key)[:150]}" if event_key else base


def _seen_event(tg_id: int, source: str, event_key) -> bool:
    if not event_key:
        return False
    row = db.fetchone(
        "SELECT id FROM auth_events WHERE tg_id=? AND event='premium_onboarding' "
        "AND details=? LIMIT 1", (tg_id, _event_details(source, event_key)))
    return row is not None


def _sent_seconds_ago(tg_id: int):
    row = db.fetchone(
        "SELECT created_at FROM auth_events WHERE tg_id=? AND event='premium_onboarding' "
        "ORDER BY id DESC LIMIT 1", (tg_id,))
    if not row or not row.get("created_at"):
        return None
    try:
        dt = datetime.fromisoformat(str(row["created_at"]).replace(" ", "T"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def mark_onboarded(tg_id: int, source: str = "", event_key=None) -> None:
    try:
        db.execute(
            "INSERT INTO auth_events (tg_id, event, details) VALUES (?,?,?)",
            (tg_id, "premium_onboarding", _event_details(source, event_key)))
    except Exception:
        pass


async def premium_activated(bot, tg_id: int, days: int = 0,
                            source: str = "", force: bool = False,
                            event_key=None) -> bool:
    """Премиум выдан — прислать инструкцию (после поздравления, не вместо).

    Инструкция приходит при КАЖДОЙ выдаче: покупка за звёзды, ручная выдача в
    боте или на сайте, отложенный доступ, продление. Не приходит только за
    приглашённых друзей (NO_INSTRUCTION_SOURCES).

    event_key — что именно это за выдача (чек Stars, id заявки). С ним одно и
    то же событие не пришлёт инструкцию дважды, а новая покупка позже — пришлёт.
    Без ключа дублем считается повтор в пределах DUPLICATE_WINDOW_SECONDS.
    """
    if not tg_id:
        return False
    src = (source or "").strip() or "unknown"
    enabled = ss.get_bool("onboarding_enabled")
    n_blocks = len(blocks(only_enabled=True))
    log.info("premium onboarding: tg_id=%s source=%s days=%s event=%s enabled=%s блоков=%s",
             tg_id, src, days, event_key or "-", enabled, n_blocks)
    if src in NO_INSTRUCTION_SOURCES and not force:
        log.info("premium onboarding: tg_id=%s source=%s — инструкция не предусмотрена", tg_id, src)
        return False
    if not enabled:
        log.info("premium onboarding: tg_id=%s — отправка выключена админом", tg_id)
        return False
    if not force:
        if _seen_event(tg_id, src, event_key):
            log.info("premium onboarding: tg_id=%s событие %s уже отработано — повтор не шлём",
                     tg_id, _event_details(src, event_key))
            return False
        if not event_key:
            ago = _sent_seconds_ago(tg_id)
            if ago is not None and ago < DUPLICATE_WINDOW_SECONDS:
                log.info("premium onboarding: tg_id=%s инструкция ушла %.0f с назад — "
                         "считаем дублем", tg_id, ago)
                return False
    ok = await send_instruction(bot, tg_id, source=src)
    if ok:
        mark_onboarded(tg_id, src, event_key)
    return ok


# ---------- Ручная выдача: поздравление, затем инструкция ----------

PREMIUM_CONGRATS = (
    "🎉 <b>Поздравляем! Вы получили Premium-доступ!</b>\n\n"
    "💎 Срок действия: {duration}\n\n"
    "<b>Ваши новые привилегии:</b>\n"
    "✅ Доступ ко всем платным тестам\n"
    "✅ Полные разделы и материалы для подготовки\n"
    "✅ Quiz-формат с таймером (как на ЕНТ)\n"
    "✅ Новые тесты сразу после добавления\n"
    "✅ Расширенная статистика результатов\n"
    "✅ Приоритетная поддержка\n\n"
)


async def send_premium_congrats(bot, tg_id: int, days: int) -> bool:
    """Стандартное поздравление «🎉 Поздравляем! Вы получили Premium-доступ!»
    со списком открывшихся платных тестов. Одно на все ручные выдачи."""
    import utils as _u
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    user = db.fetchone("SELECT id FROM users WHERE tg_id=?", (tg_id,))
    until_str = "—"
    if user:
        info = _u.get_premium_info(user["id"]) or {}
        exp = info.get("expires_at")
        if exp:
            until_str = str(exp)[:10]
    duration = ("♾ <b>Бессрочно</b>" if not days
                else f"⏱ <b>{days} дн.</b> (до <b>{until_str}</b>)")
    text = PREMIUM_CONGRATS.format(duration=duration)
    paid_tests = db.fetchall(
        "SELECT id, title FROM tests WHERE is_paid=1 AND status='active' "
        "AND COALESCE(is_private,0)=0 ORDER BY id DESC LIMIT 30")
    kb = InlineKeyboardBuilder()
    if paid_tests:
        text += f"🔓 <b>Платных тестов открыто: {len(paid_tests)}</b>\nВыберите тест, чтобы начать:"
        for tst in paid_tests[:15]:
            kb.button(text=f"💎 {(tst['title'] or '—')[:45]}",
                      callback_data=f"opentest:{tst['id']}")
        kb.button(text="📚 Все тесты", callback_data="m:tests")
    else:
        text += "📚 Откройте каталог тестов в главном меню."
        kb.button(text="📚 Главное меню", callback_data="m:menu")
    kb.adjust(1)
    try:
        await bot.send_message(tg_id, text, reply_markup=kb.as_markup(), parse_mode="HTML")
        log.info("premium congrats: tg_id=%s days=%s отправлено", tg_id, days)
        return True
    except Exception as e:
        log.warning("premium congrats: tg_id=%s НЕ отправлено: %s", tg_id, e)
        return False


async def notify_manual_grant(bot, tg_id: int, days: int, source: str = "admin",
                              event_key=None) -> dict:
    """Ручная выдача: сначала поздравление, и только после него — инструкция.

    Если поздравление не дошло (человек не открывал бота или заблокировал
    его), инструкцию не пробуем: она упадёт так же, а порядок «поздравление →
    инструкция» нарушать нельзя. Премиум при этом остаётся выданным.
    """
    congrats = await send_premium_congrats(bot, tg_id, days)
    instruction = False
    if congrats:
        try:
            instruction = await premium_activated(bot, tg_id, days, source=source,
                                                  event_key=event_key)
        except Exception as e:
            log.warning("premium onboarding: tg_id=%s source=%s сбой: %s", tg_id, source, e)
    else:
        log.warning("premium onboarding: tg_id=%s source=%s — поздравление не дошло, "
                    "инструкция пропущена", tg_id, source)
    return {"congrats": congrats, "instruction": instruction}


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
