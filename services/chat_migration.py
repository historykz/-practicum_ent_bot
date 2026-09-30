"""
Группа стала супергруппой — у неё новый ID.

Когда обычную группу Telegram превращает в супергруппу (включили видимую
историю для новых участников, публичную ссылку, больше 200 участников и т. п.),
её ID меняется на -100…. На любой запрос по старому ID Telegram отвечает
«Bad Request: group chat was upgraded to a supergroup chat» и называет новый ID
(migrate_to_chat_id). Раньше бот этого не понимал: автозапуск раз за разом слал
тесты по старому ID и засыпал админа уведомлениями «тест не запустился».

Теперь:
  • ChatMigrationMiddleware — перехватчик ВСЕХ запросов бота: видит эту ошибку,
    обновляет ID везде, где бот его хранит, и повторяет тот же запрос уже в
    супергруппу (автозапуск, серии тестов, вопросы, модерация — всё сразу);
  • служебное сообщение Telegram о переходе (handlers/group_quiz.py) обновляет
    ID заранее, ещё до первой ошибки;
  • resolve() — актуальный ID по старому: для незаконченного мастера серии.
"""
import asyncio
import json
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramMigrateToChat

import database as db

log = logging.getLogger(__name__)

S_MAP = "chat_migrations"          # settings: {"старый id": "новый id"}


def _map() -> dict:
    row = db.fetchone("SELECT value FROM settings WHERE key=?", (S_MAP,))
    try:
        data = json.loads(row["value"]) if row and row["value"] else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def resolve(chat_id):
    """Актуальный ID чата: старый ID группы → ID её супергруппы (с цепочками)."""
    if chat_id in (None, "", "none"):
        return chat_id
    m, cur, seen = _map(), str(chat_id), set()
    while cur in m and cur not in seen:
        seen.add(cur)
        cur = m[cur]
    if cur == str(chat_id):
        return chat_id
    return int(cur) if isinstance(chat_id, int) else cur


def migrate_chat(old_id, new_id) -> dict:
    """Заменить старый ID группы новым ID супергруппы везде, где бот его хранит."""
    try:
        old, new = int(old_id), int(new_id)
    except (TypeError, ValueError):
        return {}
    if old == new:
        return {}
    stats = {}

    def run(label, sql, params):
        try:
            n = db.execute(sql, params).rowcount or 0
            if n:
                stats[label] = stats.get(label, 0) + n
        except Exception as e:
            log.warning("перенос ID чата %s → %s (%s): %s", old, new, label, e)

    run("расписания автозапуска", "UPDATE auto_schedule SET chat_id=? WHERE chat_id=?", (str(new), str(old)))
    run("канал автозапуска", "UPDATE auto_schedule SET channel_id=? WHERE channel_id=?", (str(new), str(old)))
    run("серии тестов", "UPDATE autopub_series SET test_chat_id=? WHERE test_chat_id=?", (str(new), str(old)))
    run("анонс серий", "UPDATE autopub_series SET announcement_channel_id=? WHERE announcement_channel_id=?",
        (str(new), str(old)))
    run("идущие тесты", "UPDATE group_quizzes SET chat_id=? WHERE chat_id=? AND status IN ('lobby','running','paused')",
        (new, old))
    run("группы бота", "UPDATE OR IGNORE known_groups SET chat_id=?, type='supergroup' WHERE chat_id=?", (new, old))
    run("активность чата", "UPDATE OR IGNORE chat_activity SET chat_id=? WHERE chat_id=?", (str(new), str(old)))
    run("модерация", "UPDATE OR IGNORE chat_moderation SET chat_id=? WHERE chat_id=?", (new, old))
    run("предупреждения", "UPDATE OR IGNORE link_warnings SET chat_id=? WHERE chat_id=?", (new, old))
    for key in ("autopub_chats", "autopub_channels"):
        row = db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        try:
            items = json.loads(row["value"]) if row and row["value"] else []
        except (ValueError, TypeError):
            items = []
        if not any(str(c.get("id")) == str(old) for c in items):
            continue
        existing = next((c for c in items if str(c.get("id")) == str(new)), None)
        out = []
        for c in items:
            if str(c.get("id")) == str(old):
                if existing is not None:
                    if c.get("invite") and not existing.get("invite"):
                        existing["invite"] = c["invite"]
                    continue
                c = dict(c, id=str(new))
            out.append(c)
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(out, ensure_ascii=False)))
        stats[key] = 1
    for key in ("autopub_chat_id", "autopub_channel_id", "admin_log_chat"):
        run(key, "UPDATE settings SET value=? WHERE key=? AND value=?", (str(new), key, str(old)))
    m = _map()
    m[str(old)] = str(new)
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (S_MAP, json.dumps(m)))
    log.warning("Группа %s стала супергруппой %s — ID обновлён: %s", old, new, stats or "больше нигде не хранился")
    return stats


class ChatMigrationMiddleware(BaseRequestMiddleware):
    """Перехватчик запросов бота: «группа стала супергруппой» → обновить ID и повторить."""

    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramMigrateToChat as e:
            old = getattr(method, "chat_id", None)
            new = getattr(e, "migrate_to_chat_id", None)
            if old is None or not new or str(old) == str(new):
                raise
            try:
                await asyncio.to_thread(migrate_chat, old, new)
            except Exception as ex:
                log.warning("перенос ID чата %s → %s: %s", old, new, ex)
            method.chat_id = new
            log.warning("Запрос %s повторён в супергруппу %s (был %s)", getattr(method, "__api_method__", "?"), new, old)
            return await make_request(bot, method)
