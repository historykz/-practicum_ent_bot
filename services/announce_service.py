"""Анонсы автотестов в канале и реклама после групповых тестов (v95).

Работает внутри существующего автозапуска (services/auto_schedule_service.py):
  • за N минут до слота — предварительный анонс в канал с темами, числом
    вопросов, временем и кнопкой в чат. Тесты слота выбираются в этот момент
    и запоминаются (auto_schedule_plans), запуск берёт именно их;
  • после успешного старта лобби — сообщение «МЫ НАЧАЛИ» с той же кнопкой;
  • после окончания группового теста — реклама в тот же чат.

Ссылка на чат берётся из настроек: у расписания (chat_link) или, если там
пусто, у этого чата в «Авто-публикации». Сама ссылка не создаётся.

Каждая отправка сначала записывается в журнал с уникальным ключом, поэтому
перезапуск сервера или второй проход цикла её не повторит. Неудачную можно
отправить повторно — уже отправленные при этом не дублируются.
"""
import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Optional

import config
import database as db
import utils

log = logging.getLogger(__name__)

KINDS = {"pre": "Предварительный анонс", "start": "Анонс старта"}
DEFAULT_TEXTS = {
    "pre": ("🧠 <b>SMART ЕНТ | ТЕСТИРОВАНИЕ</b>\n\n"
            "🔥 Друзья, давайте вместе решим тесты в нашем чатике!\n\n"
            "📚 Предмет: <b>{предмет}</b>\n"
            "📖 Сегодняшние темы:\n{темы}\n\n"
            "📝 Количество вопросов: <b>{вопросов}</b>\n"
            "⏰ Начало: <b>{время}</b>\n\n"
            "🚀 Проверим свои знания и подготовимся к ЕНТ вместе!\n\n"
            "👇 НАЧАТЬ ТЕСТИРОВАНИЕ"),
    "start": ("🚀 <b>МЫ НАЧАЛИ ТЕСТИРОВАНИЕ!</b>\n\n"
              "🧠 Тесты по предмету «{предмет}» уже идут в нашем чате!\n"
              "🔥 Заходи прямо сейчас и проверь свои знания!\n\n"
              "👇👇👇\n"
              "<b>НАЧАТЬ ТЕСТИРОВАНИЕ</b>\n\n"
              "🔗 Ссылка на чат: {ссылка}"),
}
PLACEHOLDERS = {"предмет": "предмет или раздел", "темы": "список тем (названия тестов)",
                "вопросов": "сколько вопросов всего", "время": "время начала",
                "дата": "дата", "ссылка": "ссылка на чат", "тестов": "сколько тестов"}
BUTTON_TEXT = "🚀 НАЧАТЬ ТЕСТИРОВАНИЕ"
TZ_CHOICES = {"Asia/Oral": "Орал, Актобе, Атырау (UTC+5)", "Asia/Almaty": "Астана, Алматы (UTC+5)",
              "Asia/Aqtau": "Актау (UTC+5)", "Asia/Tashkent": "Ташкент (UTC+5)",
              "Europe/Moscow": "Москва (UTC+3)", "Asia/Bishkek": "Бишкек (UTC+6)"}
_TZ_FALLBACK = {"Asia/Oral": 5, "Asia/Almaty": 5, "Asia/Aqtau": 5, "Asia/Tashkent": 5,
                "Europe/Moscow": 3, "Asia/Bishkek": 6}


# ───────────────────────── настройки ─────────────────────────

def _setting(key: str, default: str = "") -> str:
    r = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return (r["value"] if r and r["value"] is not None else default)


def _set(key: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))


def tz_name() -> str:
    name = _setting("auto_tz", "Asia/Oral")
    return name if name in TZ_CHOICES else "Asia/Oral"


def tzinfo():
    """Часовой пояс расписаний. Без базы часовых поясов на сервере — тот же сдвиг."""
    name = tz_name()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=_TZ_FALLBACK.get(name, 5)))


def set_tz(name: str) -> bool:
    if name not in TZ_CHOICES:
        return False
    _set("auto_tz", name)
    apply_tz()
    return True


def apply_tz() -> None:
    """Расписания считают время в выбранном поясе: модуль берёт его из ALMATY."""
    try:
        from services import auto_schedule_service as ass
        ass.ALMATY = tzinfo()
    except Exception as e:
        log.warning("часовой пояс расписаний: %s", e)


def ads_settings() -> dict:
    return {"enabled": _setting("ads_enabled", "1") == "1",
            "mode": _setting("ads_mode", "sequence") if _setting("ads_mode", "sequence") in ("sequence", "random") else "sequence",
            "per_test": max(1, min(int(_setting("ads_per_test", "1") or 1), 5)),
            "every_n": max(1, min(int(_setting("ads_every_n", "1") or 1), 50))}


def save_ads_settings(enabled: bool, mode: str, per_test: int, every_n: int) -> None:
    _set("ads_enabled", "1" if enabled else "0")
    _set("ads_mode", mode if mode in ("sequence", "random") else "sequence")
    _set("ads_per_test", max(1, min(int(per_test or 1), 5)))
    _set("ads_every_n", max(1, min(int(every_n or 1), 50)))


# ───────────────────────── шаблоны ─────────────────────────

def templates(kind: str = "") -> list:
    sql, args = "SELECT * FROM announce_templates", []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    return [dict(r) for r in db.fetchall(sql + " ORDER BY kind, id", args)]


def get_template(tid) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM announce_templates WHERE id=?", (int(tid or 0),))
    return dict(r) if r else None


def save_template(tid, title: str, kind: str, body: str, category_id=None, sticker: str = "") -> int:
    if kind not in KINDS:
        raise ValueError("kind")
    body = (body or "").strip()[:3500] or DEFAULT_TEXTS[kind]
    cat = int(category_id) if str(category_id or "").lstrip("-").isdigit() else None
    if tid:
        db.execute("UPDATE announce_templates SET title=?, kind=?, body=?, category_id=?, sticker_file_id=? WHERE id=?",
                   ((title or KINDS[kind])[:120], kind, body, cat, (sticker or "")[:300] or None, int(tid)))
        return int(tid)
    return db.execute("INSERT INTO announce_templates (title, kind, body, category_id, sticker_file_id, created_at) "
                      "VALUES (?,?,?,?,?,?)", ((title or KINDS[kind])[:120], kind, body, cat,
                                               (sticker or "")[:300] or None, utils.now_iso())).lastrowid


def delete_template(tid) -> None:
    db.execute("DELETE FROM announce_templates WHERE id=?", (int(tid),))
    db.execute("UPDATE auto_schedule SET pre_template_id=NULL WHERE pre_template_id=?", (int(tid),))
    db.execute("UPDATE auto_schedule SET start_template_id=NULL WHERE start_template_id=?", (int(tid),))


def pick_template(kind: str, sched: dict, category_id=None) -> dict:
    """Шаблон расписания → шаблон раздела (предмета) → общий → встроенный текст."""
    tid = sched.get(f"{kind}_template_id") if sched else None
    t = get_template(tid) if tid else None
    if not t and category_id:
        r = db.fetchone("SELECT * FROM announce_templates WHERE kind=? AND category_id=? ORDER BY id DESC LIMIT 1",
                        (kind, int(category_id)))
        t = dict(r) if r else None
    if not t:
        r = db.fetchone("SELECT * FROM announce_templates WHERE kind=? AND category_id IS NULL ORDER BY id DESC LIMIT 1",
                        (kind,))
        t = dict(r) if r else None
    return t or {"id": None, "title": "Встроенный", "kind": kind, "body": DEFAULT_TEXTS[kind], "sticker_file_id": None}


_ALLOWED_TAGS = re.compile(r"</?(b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|tg-emoji|blockquote)\b[^>]*>", re.I)


def clean_html(body: str) -> str:
    """Только теги, которые понимает Telegram. Остальное экранируется."""
    out, pos = [], 0
    for m in _ALLOWED_TAGS.finditer(body or ""):
        out.append(_esc_text(body[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_esc_text(body[pos:]))
    return "".join(out)


def _esc_text(s: str) -> str:
    # Готовые сущности (&amp; &lt;) не портим, одиночные < > & экранируем
    s = re.sub(r"&(?!(amp|lt|gt|quot|#\d+);)", "&amp;", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


def render(body: str, ctx: dict) -> str:
    text = clean_html(body)
    for key in PLACEHOLDERS:
        text = text.replace("{" + key + "}", ctx.get(key, ""))
    return text


def strip_custom_emoji(html: str) -> str:
    """Запасной вариант: если Telegram не принял Premium-эмодзи, остаются обычные."""
    return re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", html or "", flags=re.S)


# ───────────────────────── ссылка на чат ─────────────────────────

_LINK_RE = re.compile(r"^(https://t\.me/|https://telegram\.me/|tg://)[\w+\-/?=&.%]+$")


def valid_link(link: str) -> bool:
    return bool(_LINK_RE.match((link or "").strip()))


def chat_link(sched: dict) -> str:
    link = (sched or {}).get("chat_link") or ""
    if valid_link(link):
        return link.strip()
    try:
        from services import autopub_service as aps
        for c in aps.get_chats():
            if str(c.get("id")) == str((sched or {}).get("chat_id")) and valid_link(c.get("invite") or ""):
                return c["invite"].strip()
    except Exception:
        pass
    return ""


def button(link: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    if not valid_link(link):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BUTTON_TEXT, url=link)]])


# ───────────────────────── контекст анонса ─────────────────────────

def context(sched: dict, tests: list, hm: str, day: str) -> dict:
    """Настоящие данные запланированного тестирования."""
    cats = []
    for t in tests:
        if t.get("category_id"):
            r = db.fetchone("SELECT name FROM test_categories WHERE id=?", (t["category_id"],))
            if r and r["name"] not in cats:
                cats.append(r["name"])
    subjects = [s for s in dict.fromkeys((t.get("subject") or "").strip() for t in tests) if s]
    subject = ", ".join(subjects or cats) or "Общий"
    topics = [(t.get("topic") or "").strip() or (t.get("title") or "").strip() for t in tests]
    qn = 0
    for t in tests:
        qn += db.fetchone("SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (t["id"],))["c"]
    link = chat_link(sched)
    try:
        day_h = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        day_h = day
    return {"предмет": escape(subject), "темы": "\n".join("• " + escape(x) for x in dict.fromkeys(topics) if x) or "• —",
            "вопросов": str(qn), "время": escape(hm), "дата": escape(day_h),
            "ссылка": escape(link) if link else "уточните у администратора", "тестов": str(len(tests)),
            "_link": link, "_category_id": (tests[0].get("category_id") if tests else None)}


def planned_tests(sched_id: int, slot_key: str) -> Optional[list]:
    r = db.fetchone("SELECT test_ids FROM auto_schedule_plans WHERE schedule_id=? AND slot_key=?",
                    (int(sched_id), slot_key))
    if not r:
        return None
    ids = json.loads(r["test_ids"] or "[]")
    out = []
    for i in ids:
        t = db.fetchone("SELECT * FROM tests WHERE id=?", (int(i),))
        if t:
            out.append(dict(t))
    return out


def save_plan(sched_id: int, slot_key: str, tests: list) -> None:
    db.execute("INSERT OR IGNORE INTO auto_schedule_plans (schedule_id, slot_key, test_ids, created_at) VALUES (?,?,?,?)",
               (int(sched_id), slot_key, json.dumps([t["id"] for t in tests]), utils.now_iso()))


# ───────────────────────── журнал и отправка ─────────────────────────

def _claim(key: str, kind: str, target, sched_id=None, slot_key=None, title: str = "") -> Optional[int]:
    """Запись в журнале до отправки. None — уже отправлено или отправляется."""
    row = db.fetchone("SELECT id, status FROM announce_log WHERE dedup_key=?", (key,))
    if row:
        if row["status"] in ("sent", "sending"):
            return None
        cur = db.execute("UPDATE announce_log SET status='sending', error='' WHERE id=? AND status=?",
                         (row["id"], row["status"]))
        return row["id"] if cur.rowcount else None
    try:
        return db.execute("INSERT INTO announce_log (dedup_key, kind, target, schedule_id, slot_key, title, status, created_at) "
                          "VALUES (?,?,?,?,?,?, 'sending', ?)",
                          (key, kind, str(target), sched_id, slot_key, (title or "")[:200], utils.now_iso())).lastrowid
    except Exception:
        return None


def _done(log_id: int, ok: bool, error: str = "", message_id=None) -> None:
    db.execute("UPDATE announce_log SET status=?, error=?, message_id=?, sent_at=? WHERE id=?",
               ("sent" if ok else "error", (error or "")[:500], message_id, utils.now_iso(), log_id))


async def _call(fn, *a, **k):
    """Вызов Bot API с повтором при «подождите N секунд» и сетевых сбоях."""
    from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError
    for attempt in range(3):
        try:
            return await fn(*a, **k)
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(int(e.retry_after) + 1, 60))
        except TelegramNetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    return await fn(*a, **k)


async def send_html(bot, chat_id, html: str, markup=None, sticker: str = None) -> int:
    """Стикер (если есть) и текст. Если Premium-эмодзи недоступны боту — без них."""
    from aiogram.exceptions import TelegramBadRequest
    target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
    if sticker:
        try:
            await _call(bot.send_sticker, target, sticker)
        except TelegramBadRequest as e:
            log.warning("стикер анонса не отправлен: %s", e)
    try:
        m = await _call(bot.send_message, target, html, parse_mode="HTML", reply_markup=markup,
                        disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "tg-emoji" not in html and "emoji" not in str(e).lower() and "entit" not in str(e).lower():
            raise
        m = await _call(bot.send_message, target, strip_custom_emoji(html), parse_mode="HTML",
                        reply_markup=markup, disable_web_page_preview=True)
    return m.message_id


async def announce(bot, kind: str, sched: dict, tests: list, hm: str, day: str, *,
                   key: str = "", target=None, manual: bool = False) -> dict:
    """Отправить анонс по шаблону. key — защита от повтора; target — куда (по умолчанию канал расписания)."""
    target = target or sched.get("channel_id")
    if not target:
        return {"ok": False, "error": "у расписания не выбран канал"}
    ctx = context(sched, tests, hm, day)
    tpl = pick_template(kind, sched, ctx["_category_id"])
    html = render(tpl["body"], ctx)
    key = key or f"manual:{kind}:{sched.get('id')}:{utils.now_iso()}"
    log_id = _claim(key, f"{kind}{'_manual' if manual else ''}", target, sched.get("id"), f"{day} {hm}",
                    re.sub(r"<[^>]+>", "", html)[:200])
    if not log_id:
        return {"ok": True, "already": True}
    if not ctx["_link"]:
        db.execute("UPDATE announce_log SET error=? WHERE id=?",
                   ("ссылка на чат не задана: анонс ушёл без кнопки", log_id))
    try:
        mid = await send_html(bot, target, html, button(ctx["_link"]), tpl.get("sticker_file_id"))
        _done(log_id, True, "" if ctx["_link"] else "ссылка на чат не задана: анонс без кнопки", mid)
        return {"ok": True, "message_id": mid}
    except Exception as e:
        _done(log_id, False, _human_error(e))
        log.warning("анонс %s в %s не отправлен: %s", kind, target, e)
        return {"ok": False, "error": _human_error(e)}


def _human_error(e) -> str:
    s = str(e)
    low = s.lower()
    if "chat not found" in low:
        return "чат или канал не найден: проверьте ID и что бот добавлен туда"
    if "not enough rights" in low or "need administrator" in low or "chat_admin_required" in low:
        return "у бота нет прав публиковать: сделайте его администратором канала"
    if "bot is not a member" in low or "forbidden" in low:
        return "бот не состоит в канале или чате"
    return s[:300]


async def preview(bot, admin_tg: int, kind: str, sched: dict, tests: list, hm: str, day: str) -> dict:
    """Тот же анонс — только админу в личку, без записи о публикации."""
    ctx = context(sched, tests, hm, day)
    tpl = pick_template(kind, sched, ctx["_category_id"])
    try:
        mid = await send_html(bot, admin_tg, render(tpl["body"], ctx), button(ctx["_link"]), tpl.get("sticker_file_id"))
        return {"ok": True, "message_id": mid, "link": ctx["_link"]}
    except Exception as e:
        return {"ok": False, "error": _human_error(e)}


async def retry(bot, log_id: int) -> dict:
    """Повторить неудачную отправку. Отправленные не трогаются."""
    row = db.fetchone("SELECT * FROM announce_log WHERE id=?", (int(log_id),))
    if not row or row["status"] != "error":
        return {"ok": False, "error": "повторять нечего"}
    row = dict(row)
    if row["kind"].startswith("ad"):
        return await _retry_ad(bot, row)
    from services import auto_schedule_service as ass
    sched = ass.get_schedule(row["schedule_id"] or 0)
    if not sched:
        return {"ok": False, "error": "расписание удалено"}
    day, hm = (row.get("slot_key") or " ").split(" ", 1) if " " in (row.get("slot_key") or "") else ("", "")
    tests = planned_tests(sched["id"], row.get("slot_key") or "") or ass.pick_for_slot(sched)
    return await announce(bot, row["kind"].replace("_manual", ""), sched, tests, hm, day, key=row["dedup_key"],
                          target=row["target"])


# ───────────────────────── цикл предварительных анонсов ─────────────────────────

def due_pre(now: datetime) -> list:
    """Слоты, для которых пора отправить предварительный анонс: [(sched, day, hm, slot_dt)]."""
    from services import auto_schedule_service as ass
    out = []
    for sched in ass.get_active_schedules():
        if not sched.get("pre_announce") or not sched.get("channel_id"):
            continue
        lead = max(1, int(sched.get("pre_minutes") or 60))
        for offset in (0, 1):
            d = now.date() + timedelta(days=offset)
            if ass._valid_date(sched.get("start_date")) and d.isoformat() < sched["start_date"]:
                continue
            if sched.get("end_mode") == "date" and ass._valid_date(sched.get("end_date")) and d.isoformat() > sched["end_date"]:
                continue
            for hm in ass.slots_for_day(sched, d):
                slot = ass._slot_dt(d, hm)
                key = f"{d.isoformat()} {hm}"
                if key <= (sched.get("last_run_slot") or ""):
                    continue
                if slot - timedelta(minutes=lead) <= now < slot:
                    out.append((sched, d.isoformat(), hm, slot))
    return out


async def pre_tick(bot, now: datetime = None) -> int:
    from services import auto_schedule_service as ass
    now = now or ass._now_almaty()
    sent = 0
    for sched, day, hm, _slot in due_pre(now):
        slot_key = f"{day} {hm}"
        key = f"pre:{sched['id']}:{slot_key}"
        if db.fetchone("SELECT 1 FROM announce_log WHERE dedup_key=? AND status IN ('sent','sending')", (key,)):
            continue
        tests = planned_tests(sched["id"], slot_key)
        if tests is None:
            tests = ass.pick_for_slot(sched)
            if not tests:
                continue
            save_plan(sched["id"], slot_key, tests)
            tests = planned_tests(sched["id"], slot_key) or tests
        res = await announce(bot, "pre", sched, tests, hm, day, key=key)
        if res.get("ok") and not res.get("already"):
            sent += 1
        elif not res.get("ok"):
            await ass._notify_admin(bot, sched, f"⚠️ <b>Анонс в канал не отправлен</b>\n\nРасписание #{sched['id']}, "
                                                f"запуск {escape(slot_key)}\nПричина: {escape(res.get('error') or '')}\n\n"
                                                "Повторить можно в разделе «📢 Анонсы и автотесты» → «Журнал».",
                                    dedup=f"{sched['id']}:pre:{res.get('error', '')[:60]}")
    return sent


async def on_launched(bot, sched: dict, run: dict, test: dict) -> None:
    """Лобби запущено: в канал — «МЫ НАЧАЛИ», один раз на запуск (слот)."""
    if not sched.get("start_announce") or not sched.get("channel_id"):
        return
    slot_key = run.get("slot_key") or ""
    day, hm = slot_key.split(" ", 1) if " " in slot_key else ("", "")
    tests = planned_tests(sched["id"], slot_key) or [test]
    res = await announce(bot, "start", sched, tests, hm, day, key=f"start:{sched['id']}:{slot_key}")
    if not res.get("ok"):
        from services import auto_schedule_service as ass
        await ass._notify_admin(bot, sched, f"⚠️ <b>Сообщение «Мы начали» не отправлено</b>\n\nРасписание #{sched['id']}\n"
                                            f"Причина: {escape(res.get('error') or '')}",
                                dedup=f"{sched['id']}:start:{res.get('error', '')[:60]}")


# ───────────────────────── реклама после тестов ─────────────────────────

AD_KINDS = {"text": "Текст", "photo": "Картинка", "video": "Видео", "animation": "Анимация (GIF)"}


def ads(active_only: bool = False) -> list:
    rows = [dict(r) for r in db.fetchall("SELECT * FROM ads ORDER BY sort_order, id")]
    if active_only:
        now = utils.now_iso()
        rows = [r for r in rows if r["enabled"] and (not r.get("expires_at") or r["expires_at"] > now)
                and (not r.get("starts_at") or r["starts_at"] <= now)]
    return rows


def get_ad(ad_id) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM ads WHERE id=?", (int(ad_id or 0),))
    return dict(r) if r else None


def ads_dir() -> Path:
    d = Path(config.DB_PATH).resolve().parent / "uploads" / "ads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_ad(ad_id, *, title: str, kind: str, caption: str = "", button_text: str = "", button_url: str = "",
            file_id: str = None, file_name: str = None, expires_at: str = "", enabled: bool = True,
            actor=None) -> int:
    if kind not in AD_KINDS:
        raise ValueError("kind")
    if button_url and not re.match(r"^https?://", button_url):
        button_url = ""
    exp = (expires_at or "").strip()[:10]
    if exp and not re.match(r"^\d{4}-\d{2}-\d{2}$", exp):
        exp = ""
    fields = {"title": (title or AD_KINDS[kind])[:120], "kind": kind, "caption": (caption or "")[:1000],
              "button_text": (button_text or "")[:60], "button_url": (button_url or "")[:500],
              "expires_at": (exp + "T23:59:59") if exp else None, "enabled": 1 if enabled else 0}
    if file_id is not None:
        fields["file_id"] = file_id or None
    if file_name is not None:
        fields["file_name"] = file_name or None
        fields["file_id"] = file_id or None          # новый файл — старый file_id больше не подходит
    if ad_id:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE ads SET {sets} WHERE id=?", list(fields.values()) + [int(ad_id)])
        return int(ad_id)
    fields.update(created_at=utils.now_iso(), created_by=actor,
                  sort_order=(db.fetchone("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM ads")["n"]))
    return db.execute(f"INSERT INTO ads ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                      list(fields.values())).lastrowid


def toggle_ad(ad_id) -> None:
    db.execute("UPDATE ads SET enabled=1-COALESCE(enabled,0) WHERE id=?", (int(ad_id),))


def _next_ads(n: int, mode: str) -> list:
    pool = ads(active_only=True)
    if not pool:
        return []
    if mode == "random":
        return random.sample(pool, min(n, len(pool)))
    cursor = int(_setting("ads_cursor", "0") or 0)
    picked = [pool[(cursor + i) % len(pool)] for i in range(min(n, len(pool)))]
    _set("ads_cursor", (cursor + len(picked)) % max(len(pool), 1))
    return picked


def _ad_markup(ad: dict):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    if ad.get("button_url"):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=ad.get("button_text") or "Подробнее", url=ad["button_url"])]])
    return None


async def send_ad(bot, chat_id, ad: dict) -> int:
    """Отправить одну рекламу. Файл с сайта отправляется один раз, дальше — по file_id Telegram."""
    from aiogram.types import FSInputFile
    target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
    caption = clean_html(ad.get("caption") or "")
    markup = _ad_markup(ad)
    if ad["kind"] == "text":
        return await send_html(bot, target, caption or escape(ad.get("title") or ""), markup)
    media = ad.get("file_id")
    if not media and ad.get("file_name"):
        p = ads_dir() / ad["file_name"]
        if not p.exists():
            raise RuntimeError("файл рекламы не найден на сервере")
        media = FSInputFile(str(p))
    if not media:
        raise RuntimeError("у рекламы нет файла")
    fn = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation}[ad["kind"]]
    m = await _call(fn, target, media, caption=caption or None, parse_mode="HTML", reply_markup=markup)
    if not ad.get("file_id"):
        obj = m.photo[-1] if ad["kind"] == "photo" and m.photo else getattr(m, ad["kind"], None)
        if obj is not None and getattr(obj, "file_id", None):
            db.execute("UPDATE ads SET file_id=? WHERE id=?", (obj.file_id, ad["id"]))
    db.execute("UPDATE ads SET sent_count=COALESCE(sent_count,0)+1, last_sent_at=? WHERE id=?",
               (utils.now_iso(), ad["id"]))
    return m.message_id


async def after_quiz(bot, gq_id: int, chat_id, schedule_id: Optional[int] = None) -> int:
    """Реклама после завершённого группового теста. Повторно для того же теста не уходит."""
    cfg = ads_settings()
    if not cfg["enabled"]:
        return 0
    if schedule_id:
        s = db.fetchone("SELECT ads_enabled FROM auto_schedule WHERE id=?", (int(schedule_id),))
        if s and s["ads_enabled"] is not None and not s["ads_enabled"]:
            return 0
    if db.fetchone("SELECT 1 FROM announce_log WHERE dedup_key LIKE ? LIMIT 1", (f"ad:{int(gq_id)}:%",)):
        return 0                       # этот тест уже обработан (перезапуск, повторный вызов)
    count = int(_setting("ads_quiz_counter", "0") or 0) + 1
    _set("ads_quiz_counter", count)
    if count % cfg["every_n"] != 0:
        return 0
    sent = 0
    for i, ad in enumerate(_next_ads(cfg["per_test"], cfg["mode"])):
        key = f"ad:{gq_id}:{i}"
        log_id = _claim(key, "ad", chat_id, schedule_id, None, ad.get("title") or "")
        if not log_id:
            continue
        db.execute("UPDATE announce_log SET ad_id=? WHERE id=?", (ad["id"], log_id))
        try:
            mid = await send_ad(bot, chat_id, ad)
            _done(log_id, True, "", mid)
            sent += 1
        except Exception as e:
            _done(log_id, False, _human_error(e))
            log.warning("реклама %s в чат %s: %s", ad.get("id"), chat_id, e)
    return sent


async def _retry_ad(bot, row: dict) -> dict:
    ad = get_ad(row.get("ad_id"))
    if not ad:
        return {"ok": False, "error": "реклама удалена"}
    log_id = _claim(row["dedup_key"], row["kind"], row["target"])
    if not log_id:
        return {"ok": True, "already": True}
    try:
        mid = await send_ad(bot, row["target"], ad)
        _done(log_id, True, "", mid)
        return {"ok": True}
    except Exception as e:
        _done(log_id, False, _human_error(e))
        return {"ok": False, "error": _human_error(e)}


def journal(limit: int = 200) -> list:
    out = []
    for r in db.fetchall("SELECT * FROM announce_log ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["kind_title"] = {"pre": "📣 предварительный анонс", "start": "🚀 «Мы начали»", "pre_manual": "📣 анонс вручную",
                           "start_manual": "🚀 «Мы начали» вручную", "ad": "🎬 реклама"}.get(d["kind"], d["kind"])
        out.append(d)
    return out
