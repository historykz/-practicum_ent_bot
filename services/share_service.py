"""
Сервис шеринга в стиле @QuizBot.

Архитектура:
- build_test_card(test) — возвращает (текст, клавиатура) с тремя кнопками:
  ✅ Пройти тест           (url=deep-link)
  📤 Отправить в группу    (switch_inline_query)
  🔗 Поделиться             (switch_inline_query)
- build_inline_results(query) — для inline-режима. Поддерживает:
    "test:<id>"   → один конкретный тест
    "<search>"    → поиск по названию/предмету
    ""            → последние 30 активных
- Каждый inline-результат использует build_test_card,
  чтобы карточка в чате выглядела одинаково при любом способе шеринга.
"""
import logging
from typing import Optional

from aiogram.types import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

import config
import database as db
import utils

logger = logging.getLogger(__name__)


# === Deep-link'и ===

def build_test_deep_link(test_id: int, bot_username: str = None) -> str:
    bu = bot_username or config.BOT_USERNAME
    return f"https://t.me/{bu}?start=test_{test_id}"


def build_ref_link(user_tg_id: int, bot_username: str = None) -> str:
    bu = bot_username or config.BOT_USERNAME
    return f"https://t.me/{bu}?start=ref_{user_tg_id}"


def build_note_deep_link(note_id: int, bot_username: str = None) -> str:
    bu = bot_username or config.BOT_USERNAME
    return f"https://t.me/{bu}?start=note_{note_id}"


# === Карточка теста в стиле QuizBot ===

def _author_label(test: dict) -> str:
    """В карточке теста всегда указываем канал — а не реального админа."""
    return config.SHARE_AUTHOR_LABEL


def build_test_card(test: dict, bot_username: str = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    Возвращает (text, inline_keyboard) в стиле @QuizBot.
    Карточка используется и в личке (после "Поделиться" → выбор теста),
    и в любом чате, куда её отправили через inline-режим.
    """
    bu = bot_username or config.BOT_USERNAME or "bot"
    test_id = test["id"]

    qcount_row = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test_id,)
    )
    qcount = qcount_row["c"] if qcount_row else 0

    title = utils.escape_html(test.get("title") or "—")
    subject = utils.escape_html(test.get("subject") or "")
    author = utils.escape_html(_author_label(test))
    time_per_q = test.get("time_per_question") or 30

    lines = [
        f"🎲 <b>{title}</b>",
    ]
    if subject:
        lines.append(f"🏷 Тема: {subject}")
    lines.append(f"👤 Автор: {author}")
    lines.append(f"📚 {qcount} вопросов  ·  ⏱ {time_per_q} сек")
    text = "\n".join(lines)

    deep_link = build_test_deep_link(test_id, bu)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Пройти тест", url=deep_link)],
        [InlineKeyboardButton(
            text="📤 Отправить в группу",
            switch_inline_query=f"test:{test_id}",
        )],
        [InlineKeyboardButton(
            text="🔗 Поделиться",
            switch_inline_query=f"test:{test_id}",
        )],
    ])
    return text, kb


# === Inline-режим ===

def _build_inline_card(test: dict, bot_username: str) -> InlineQueryResultArticle:
    """Один InlineQueryResultArticle для inline-выдачи."""
    text, kb = build_test_card(test, bot_username)
    qcount_row = db.fetchone(
        "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test["id"],)
    )
    qcount = qcount_row["c"] if qcount_row else 0
    descr_parts = []
    if test.get("subject"):
        descr_parts.append(test["subject"])
    descr_parts.append(f"{qcount} вопросов")
    descr_parts.append(f"{test.get('time_per_question') or 30} сек")
    description = " · ".join(descr_parts)

    return InlineQueryResultArticle(
        id=f"test_{test['id']}",
        title=test.get("title") or "Тест",
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=text,
            parse_mode="HTML",
        ),
        reply_markup=kb,
    )


def _fetch_test(test_id: int) -> Optional[dict]:
    row = db.fetchone(
        "SELECT * FROM tests WHERE id=? AND status='active'", (test_id,))
    return dict(row) if row else None


def build_inline_results(query: str, user_lang: Optional[str],
                         bot_username: str = None) -> list[InlineQueryResultArticle]:
    """
    Возвращает результаты для inline-режима.

    Поддерживаемые форматы query:
        "test:<id>"      — конкретный тест по ID
        "<search text>"  — поиск по title/subject
        ""               — последние 30 активных
    """
    bu = bot_username or config.BOT_USERNAME or "bot"
    q = (query or "").strip()

    # 1) Точечный шеринг через switch_inline_query
    if q.lower().startswith("test:"):
        rest = q.split(":", 1)[1].strip()
        if rest.isdigit():
            test = _fetch_test(int(rest))
            if test:
                return [_build_inline_card(test, bu)]
            return []

    # 2) Поиск по фильтру языка — если язык задан, иначе все активные
    qlower = q.lower()
    if qlower:
        if user_lang:
            rows = db.fetchall(
                """SELECT * FROM tests WHERE status='active' AND language=?
                    AND (LOWER(title) LIKE ? OR LOWER(subject) LIKE ?)
                    ORDER BY id DESC LIMIT 30""",
                (user_lang, f"%{qlower}%", f"%{qlower}%"))
        else:
            rows = db.fetchall(
                """SELECT * FROM tests WHERE status='active'
                    AND (LOWER(title) LIKE ? OR LOWER(subject) LIKE ?)
                    ORDER BY id DESC LIMIT 30""",
                (f"%{qlower}%", f"%{qlower}%"))
    else:
        if user_lang:
            rows = db.fetchall(
                "SELECT * FROM tests WHERE status='active' AND language=? "
                "ORDER BY id DESC LIMIT 30",
                (user_lang,))
        else:
            rows = db.fetchall(
                "SELECT * FROM tests WHERE status='active' "
                "ORDER BY id DESC LIMIT 30"
            )

    return [_build_inline_card(dict(r), bu) for r in rows]
