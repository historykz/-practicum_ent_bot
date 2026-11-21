"""Главное меню бота под управлением владельца (v100).

Кнопки меню лежат в таблице menu_buttons. Встроенные (Тесты, Конспекты ЕНТ, Премиум,
Дуэль, Профиль, Пригласить, Техподдержка, Помощь) можно переименовать, скрыть,
переставить, сделать на всю ширину или пополам и показывать только в заданные даты.
Свои кнопки: ссылка, раздел со своим текстом (и картинкой, и кнопками-ссылками),
страница мини-приложения или любой готовый раздел бота. Кнопка «Admin» всегда
последняя и видна только админам — её нельзя потерять.

Тексты: текст самого меню и «Помощь» правятся здесь же (поверх locales.py),
текст «Техподдержки» — в той же настройке, что и раньше (study_settings).
Если что-то пойдёт не так, меню собирается по-старому: оно не должно пропасть.
"""
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import database as db
import utils

log = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))

# Встроенные кнопки в прежнем порядке: (ключ, действие, надпись ru, надпись kz, на всю ширину).
# Казахские надписи — точно как в прежнем меню (верхние три и «Пригласить» были по-русски).
BUILTIN = [
    ("tests", "m:tests", "📚✨ ПРОЙТИ ТЕСТЫ ✨📚", "📚✨ ПРОЙТИ ТЕСТЫ ✨📚", 1),
    ("conspects", "m:conspects", "📖🔥 КОНСПЕКТЫ ЕНТ 🔥📖", "📖🔥 КОНСПЕКТЫ ЕНТ 🔥📖", 1),
    ("premium", "buy:premium", "💎🔥 ПРЕМИУМ-ПОДПИСКА 🔥💎", "💎🔥 ПРЕМИУМ-ПОДПИСКА 🔥💎", 1),
    ("duel", "m:duel", "⚔️ Дуэль", "⚔️ Дуэль", 0),
    ("profile", "m:profile", "👤 Профиль", "👤 Профиль", 0),
    ("referral", "m:referral", "🎁 Пригласить друзей", "🎁 Пригласить друзей", 0),
    ("support", "m:support", "🛠 Техподдержка", "🛠 Техқолдау", 0),
    ("help", "m:help", "ℹ️ Помощь", "ℹ️ Көмек", 1),
]
BUILTIN_TITLES = {k: ru for k, _, ru, _, _ in BUILTIN}
KINDS = {"builtin": "встроенная", "link": "ссылка", "text": "раздел со своим текстом",
         "webapp": "страница мини-приложения", "action": "готовый раздел бота"}
# Готовые разделы бота, которые можно повесить на свою кнопку
ACTIONS = {"m:tests": "Тесты", "m:conspects": "Конспекты ЕНТ", "buy:premium": "Премиум",
           "m:duel": "Дуэль", "m:profile": "Профиль", "m:referral": "Пригласить друзей",
           "m:support": "Техподдержка", "m:help": "Помощь", "m:rating": "Рейтинг",
           "m:daily": "Daily ENT", "m:menu": "Главное меню"}
# Тексты разделов, которые правятся здесь: ключ → (название, где хранится)
TEXTS = {"main_menu": "Текст главного меню", "help_text": "Раздел «Помощь»", "support_text": "Раздел «Техподдержка»"}

_lock = threading.Lock()
_cache = {"buttons": None, "texts": None}


def _now_local() -> str:
    return datetime.now(ALMATY).strftime("%Y-%m-%d %H:%M")


def invalidate() -> None:
    with _lock:
        _cache["buttons"] = None
        _cache["texts"] = None


# ───────────────────────── кнопки ─────────────────────────

def ensure_seeded() -> None:
    """Встроенные кнопки появляются в таблице один раз, в прежнем порядке."""
    have = {r["builtin_key"] for r in db.fetchall("SELECT builtin_key FROM menu_buttons WHERE builtin_key IS NOT NULL")}
    for i, (key, action, ru, kz, full) in enumerate(BUILTIN):
        if key not in have:
            db.execute("INSERT INTO menu_buttons (builtin_key, kind, action, full_row, enabled, sort_order, created_at) "
                       "VALUES (?, 'builtin', ?, ?, 1, ?, ?)", (key, action, full, (i + 1) * 10, utils.now_iso()))


def all_buttons() -> list:
    ensure_seeded()
    return [dict(r) for r in db.fetchall("SELECT * FROM menu_buttons ORDER BY sort_order, id")]


def _buttons_cached() -> list:
    with _lock:
        if _cache["buttons"] is None:
            _cache["buttons"] = all_buttons()
        return _cache["buttons"]


def label(b: dict, lang: str) -> str:
    if lang == "kz" and (b.get("label_kz") or "").strip():
        return b["label_kz"].strip()
    if (b.get("label_ru") or "").strip():
        return b["label_ru"].strip()
    for key, _, ru, kz, _ in BUILTIN:
        if key == b.get("builtin_key"):
            return kz if lang == "kz" else ru
    return "Кнопка"


def is_live(b: dict, now: Optional[str] = None) -> bool:
    """Включена и сейчас в своих датах показа."""
    if not b.get("enabled"):
        return False
    now = now or _now_local()
    if b.get("show_from") and now < b["show_from"]:
        return False
    if b.get("show_until") and now >= b["show_until"]:
        return False
    return True


def status(b: dict) -> str:
    if not b.get("enabled"):
        return "⏸ выключена"
    now = _now_local()
    if b.get("show_from") and now < b["show_from"]:
        return f"⏳ появится {b['show_from']}"
    if b.get("show_until") and now >= b["show_until"]:
        return f"⌛ срок вышел {b['show_until']}"
    if b.get("show_until"):
        return f"✅ до {b['show_until']}"
    return "✅ видна"


def _button(b: dict, lang: str):
    from aiogram.types import InlineKeyboardButton, WebAppInfo
    text = label(b, lang)[:64]
    kind = b.get("kind")
    if kind == "link" and (b.get("url") or "").startswith(("https://", "http://", "tg://")):
        return InlineKeyboardButton(text=text, url=b["url"])
    if kind == "webapp" and b.get("url"):
        from services.broadcast_service import site_url
        url = b["url"] if b["url"].startswith("https://") else site_url() + "/" + b["url"].lstrip("/")
        if url.startswith("https://"):
            return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))
        return InlineKeyboardButton(text=text, url=url)
    if kind == "text":
        return InlineKeyboardButton(text=text, callback_data=f"mm:{b['id']}")
    if kind in ("builtin", "action") and b.get("action"):
        return InlineKeyboardButton(text=text, callback_data=b["action"])
    return None


def keyboard(lang: str, is_admin: bool = False):
    """Клавиатура главного меню по настройкам. Кнопки «пополам» встают по две в ряд."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows, half = [], []
    now = _now_local()
    for b in _buttons_cached():
        if not is_live(b, now):
            continue
        btn = _button(b, lang)
        if btn is None:
            continue
        if b.get("full_row"):
            if half:
                rows.append(half)
                half = []
            rows.append([btn])
        else:
            half.append(btn)
            if len(half) == 2:
                rows.append(half)
                half = []
    if half:
        rows.append(half)
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Admin", callback_data="m:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_button(bid) -> Optional[dict]:
    r = db.fetchone("SELECT * FROM menu_buttons WHERE id=?", (int(bid or 0),))
    return dict(r) if r else None


def _clean_url(url: str, kind: str) -> str:
    url = (url or "").strip()
    if kind == "link":
        if url.startswith("@"):
            url = "https://t.me/" + url[1:]
        if not re.match(r"^(https?://|tg://)\S+$", url):
            raise ValueError("Ссылка должна начинаться с https:// (или @имя для Telegram).")
    elif kind == "webapp":
        if not (url.startswith("/") or url.startswith("https://")):
            raise ValueError("Страница мини-приложения: путь вида /learn/5 или полная ссылка https://.")
    return url[:500]


def _clean_dt(raw: str, end_of_day: bool = False) -> Optional[str]:
    """«2026-12-31», «31.12.2026 18:00» → «2026-12-31 18:00» (время Астаны). Пусто — без срока.
    Дата без времени: «с» — с начала дня, «до» — до конца дня."""
    raw = (raw or "").strip().replace("T", " ")
    if not raw or raw.lower() in ("-", "нет", "сбросить", "бессрочно"):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt in ("%Y-%m-%d", "%d.%m.%Y") and end_of_day:
            dt = dt + timedelta(days=1)            # «до 31.12» — включая весь день 31 декабря
        return dt.strftime("%Y-%m-%d %H:%M")
    raise ValueError("Дата как 2026-12-31 или 31.12.2026 18:00.")


def create(kind: str, label_ru: str, *, url: str = "", body: str = "", action: str = "", photo: str = "",
           label_kz: str = "", full_row: int = 1, enabled: int = 1, show_from: str = "", show_until: str = "") -> int:
    if kind not in ("link", "text", "webapp", "action"):
        raise ValueError("Неизвестный вид кнопки.")
    label_ru = (label_ru or "").strip()[:64]
    if not label_ru:
        raise ValueError("Нужна надпись на кнопке.")
    if kind in ("link", "webapp"):
        url = _clean_url(url, kind)
    if kind == "action" and action not in ACTIONS:
        raise ValueError("Выберите раздел бота.")
    ensure_seeded()
    sort = (db.fetchone("SELECT COALESCE(MAX(sort_order),0)+10 AS n FROM menu_buttons")["n"])
    bid = db.execute(
        "INSERT INTO menu_buttons (kind, label_ru, label_kz, url, body_ru, photo_file_id, action, full_row, enabled, "
        "sort_order, show_from, show_until, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, label_ru, (label_kz or "").strip()[:64] or None, url or None, (body or "")[:4000] or None,
         photo or None, action or None, 1 if full_row else 0, 1 if enabled else 0, sort,
         _clean_dt(show_from), _clean_dt(show_until, True), utils.now_iso())).lastrowid
    invalidate()
    return bid


def update(bid, **fields) -> None:
    b = get_button(bid)
    if not b:
        raise ValueError("Кнопка не найдена.")
    allowed = {"label_ru", "label_kz", "url", "body_ru", "body_kz", "photo_file_id", "action", "full_row", "enabled",
               "show_from", "show_until", "links"}
    upd = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("label_ru", "label_kz"):
            v = (v or "").strip()[:64] or None
            if k == "label_ru" and not v and b["kind"] != "builtin":
                raise ValueError("Нужна надпись на кнопке.")
        elif k == "url":
            v = _clean_url(v, b["kind"]) or None
        elif k in ("show_from", "show_until"):
            v = _clean_dt(v, k == "show_until")
        elif k in ("full_row", "enabled"):
            v = 1 if v else 0
        elif k == "action":
            if b["kind"] == "builtin" or v not in ACTIONS:
                raise ValueError("Раздел бота не найден.")
        elif k in ("body_ru", "body_kz"):
            v = (v or "")[:4000] or None
        elif k == "links":
            v = (v or "").strip()[:2000] or None
        upd[k] = v
    if upd:
        db.execute(f"UPDATE menu_buttons SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", list(upd.values()) + [int(bid)])
    invalidate()


def move(bid, direction: str) -> None:
    rows = all_buttons()
    ids = [r["id"] for r in rows]
    if int(bid) not in ids:
        return
    i = ids.index(int(bid))
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for n, x in enumerate(ids):
        db.execute("UPDATE menu_buttons SET sort_order=? WHERE id=?", ((n + 1) * 10, x))
    invalidate()


def delete(bid, actor=None, source: str = "bot") -> bool:
    """Свою кнопку — в корзину (вернуть можно 48 часов). Встроенную только выключить."""
    b = get_button(bid)
    if not b:
        return False
    if b["kind"] == "builtin":
        raise ValueError("Встроенную кнопку удалить нельзя — её можно выключить.")
    from services import trash
    trash.move("menu_button", b.get("label_ru") or "Кнопка меню", actor, source, [("menu_buttons", int(bid))],
               meta={"location": "Главное меню бота"})
    invalidate()
    return True


def reset_builtins() -> None:
    """Вернуть встроенным кнопкам прежние надписи, порядок и видимость. Свои кнопки остаются."""
    for i, (key, action, ru, kz, full) in enumerate(BUILTIN):
        db.execute("UPDATE menu_buttons SET label_ru=NULL, label_kz=NULL, enabled=1, full_row=?, sort_order=?, "
                   "show_from=NULL, show_until=NULL WHERE builtin_key=?", (full, (i + 1) * 10, key))
    invalidate()


def section_links(b: dict) -> list:
    """Кнопки-ссылки под текстом раздела: строки «Текст | https://…»."""
    out = []
    for line in (b.get("links") or "").splitlines():
        if "|" in line:
            t, u = [x.strip() for x in line.split("|", 1)]
            if t and re.match(r"^(https?://|tg://)\S+$", u):
                out.append((t[:64], u))
    return out


def parse_links(raw: str) -> str:
    lines, bad = [], []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            bad.append(line)
            continue
        t, u = [x.strip() for x in line.split("|", 1)]
        if u.startswith("@"):
            u = "https://t.me/" + u[1:]
        if not t or not re.match(r"^(https?://|tg://)\S+$", u):
            bad.append(line)
            continue
        lines.append(f"{t[:64]} | {u}")
    if bad:
        raise ValueError("Не понял строки: " + "; ".join(bad[:3]) + ". Формат: Текст кнопки | https://ссылка")
    return "\n".join(lines)


_TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler",
            "span", "blockquote", "br", "tg-emoji"}


def check_html(text: str) -> str:
    """Текст с сайта: только разметка, которую понимает Telegram, и все теги закрыты.
    Переносы <br> превращаются в обычные строки."""
    from html.parser import HTMLParser
    text = re.sub(r"<br\s*/?>", "\n", (text or "").replace("\r\n", "\n"), flags=re.I).strip()
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)          # «5 & 6» — Telegram требует &amp;
    text = re.sub(r"<(?![a-zA-Z/])", "&lt;", text)          # «5 < 6»
    stack, bad = [], []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in _TG_TAGS:
                bad.append(tag)
            else:
                stack.append(tag)

        def handle_endtag(self, tag):
            if stack and stack[-1] == tag:
                stack.pop()
            else:
                bad.append("/" + tag)

    P(convert_charrefs=True).feed(text)
    if bad or stack:
        raise ValueError("В тексте можно только <b>, <i>, <u>, <s>, <a href=\"…\">, <code>, <blockquote>, "
                         "и каждый тег нужно закрыть. Не понял: " + ", ".join((bad + stack)[:3]))
    return text


def set_photo(bid, value: str) -> None:
    """Картинка раздела: file_id из бота или ссылка https:// на картинку. Пусто — убрать."""
    value = (value or "").strip()
    if value and not value.startswith("https://") and not re.match(r"^[A-Za-z0-9_-]{20,}$", value):
        raise ValueError("Картинка: ссылка https://… или пришлите картинку в боте.")
    update(bid, photo_file_id=value or None)


# ───────────────────────── тексты разделов ─────────────────────────

def _texts_cached() -> dict:
    with _lock:
        if _cache["texts"] is None:
            try:
                _cache["texts"] = {(r["key"], r["lang"]): r["value"] for r in
                                   db.fetchall("SELECT key, lang, value FROM bot_texts")}
            except Exception:
                return {}
        return _cache["texts"]


def text_override(key: str, lang: str) -> Optional[str]:
    """Своя версия текста бота (для locales.t). None — как в коде."""
    if key not in TEXTS:
        return None
    try:
        d = _texts_cached()
    except Exception:
        return None
    return d.get((key, lang)) or (d.get((key, "ru")) if lang != "ru" else None)


def get_text(key: str, lang: str = "ru") -> str:
    if key == "support_text":
        # только то, что владелец задал сам (без текста по умолчанию)
        r = db.fetchone("SELECT value FROM settings WHERE key='support_text'")
        return ((r["value"] if r else "") or "").strip()
    return (_texts_cached().get((key, lang)) or "")


def default_text(key: str, lang: str = "ru") -> str:
    if key == "support_text":
        from services import study_settings as ss
        return ss.DEFAULTS.get("support_text", "")     # его и видят ученики, пока нет своего
    import locales
    entry = locales.TEXTS.get(key) or {}
    return entry.get(lang) or entry.get("ru") or ""


def set_text(key: str, lang: str, value: str) -> None:
    if key not in TEXTS or lang not in ("ru", "kz"):
        raise ValueError("Неизвестный текст.")
    value = (value or "").strip()[:4000]
    if key == "support_text":
        from services import study_settings as ss
        ss.set_value("support_text", value[:1000])   # та же настройка, что и раньше
        return
    if value:
        db.execute("INSERT INTO bot_texts (key, lang, value, updated_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(key, lang) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (key, lang, value, utils.now_iso()))
    else:
        db.execute("DELETE FROM bot_texts WHERE key=? AND lang=?", (key, lang))
    invalidate()


def menu_text(lang: str) -> str:
    import locales
    return locales.t("main_menu", lang)


# ───────────────────────── раздел со своим текстом ─────────────────────────

async def show_section(target_msg, b: dict, lang: str) -> None:
    """Показать раздел своей кнопки: картинка (если есть), текст и кнопки-ссылки, внизу «Меню»."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from aiogram.exceptions import TelegramBadRequest
    body = ((b.get("body_kz") if lang == "kz" else None) or b.get("body_ru") or label(b, lang)).strip()
    rows = [[InlineKeyboardButton(text=t, url=u)] for t, u in section_links(b)]
    back = "↩️ Мәзір" if lang == "kz" else "↩️ Главное меню"
    rows.append([InlineKeyboardButton(text=back, callback_data="m:menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    bot = target_msg.bot
    chat = target_msg.chat.id
    try:
        if b.get("photo_file_id"):
            if len(body) <= 1000:
                await bot.send_photo(chat, b["photo_file_id"], caption=body, parse_mode="HTML", reply_markup=kb)
                return
            await bot.send_photo(chat, b["photo_file_id"])
        await bot.send_message(chat, body, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)
    except TelegramBadRequest:
        # Разметка не понравилась Telegram — показываем как обычный текст
        plain = re.sub(r"<[^>]+>", "", body)
        await bot.send_message(chat, plain, reply_markup=kb)
