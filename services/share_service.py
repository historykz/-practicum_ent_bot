"""Сервис для шеринга: deep-link'и и инлайн-результаты."""
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db
import utils
from locales import t


def build_test_deep_link(test_id: int, bot_username: str = None) -> str:
    bu = bot_username or config.BOT_USERNAME
    return f"https://t.me/{bu}?start=test_{test_id}"


def build_ref_link(user_tg_id: int, bot_username: str = None) -> str:
    bu = bot_username or config.BOT_USERNAME
    return f"https://t.me/{bu}?start=ref_{user_tg_id}"


def build_note_deep_link(note_id: int, bot_username: str = None) -> str:
    bu = bot_username or config.BOT_USERNAME
    return f"https://t.me/{bu}?start=note_{note_id}"


def build_inline_results(query: str, user_lang: str,
                         bot_username: str = None) -> list[InlineQueryResultArticle]:
    """Активные тесты пользовательского языка."""
    bu = bot_username or config.BOT_USERNAME
    q = query.strip().lower()
    if q:
        rows = db.fetchall(
            """SELECT * FROM tests WHERE status='active' AND language=?
                AND (LOWER(title) LIKE ? OR LOWER(subject) LIKE ?)
                ORDER BY id DESC LIMIT 30""",
            (user_lang, f"%{q}%", f"%{q}%"))
    else:
        rows = db.fetchall(
            "SELECT * FROM tests WHERE status='active' AND language=? ORDER BY id DESC LIMIT 30",
            (user_lang,))

    results = []
    for r in rows:
        title = r['title']
        descr = r['description'] or ''
        subject = r['subject'] or ''
        url = build_test_deep_link(r['id'], bu)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("inline_open_btn", user_lang), url=url)
        ]])
        msg_text = t("inline_share_text", user_lang,
                     title=utils.escape_html(title),
                     subject=utils.escape_html(subject),
                     url=url)
        article = InlineQueryResultArticle(
            id=f"test_{r['id']}",
            title=title,
            description=f"{subject} • {descr[:80]}" if descr else subject,
            input_message_content=InputTextMessageContent(
                message_text=msg_text, parse_mode="HTML"),
            reply_markup=kb,
        )
        results.append(article)
    return results
