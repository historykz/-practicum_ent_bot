"""
Хендлер модерации чата.
Команды (только админы бота):
  бан / ban         — забанить (reply или @username)
  кик / kick        — выгнать
  мут <время> / mute — замутить на срок (1час, 30мин, 2дня...)
  размут / unmute   — снять мут
  разбан / unban    — снять бан
  бан список        — список забаненных
Бот действует от имени чата (личность админа не светится).
"""
import logging
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, ChatPermissions

import database as db
import utils
from services import moderation_service as mod

router = Router(name="moderation")
log = logging.getLogger(__name__)

# Триггеры команд
BAN_WORDS = {"бан", "ban", "/ban"}
KICK_WORDS = {"кик", "kick", "/kick"}
MUTE_WORDS = {"мут", "mute", "/mute"}
UNMUTE_WORDS = {"размут", "unmute", "/unmute"}
UNBAN_WORDS = {"разбан", "unban", "/unban"}
LOCK_WORDS = {"-чат", "-chat", "/lockchat", "закрыть"}
UNLOCK_WORDS = {"+чат", "+chat", "/unlockchat", "открыть"}


def _is_group(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup")


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверка: админ бота ИЛИ админ/создатель чата."""
    if utils.is_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _can_moderate(message: Message, bot: Bot) -> bool:
    """Может ли модерировать: анонимный админ чата ИЛИ обычный админ."""
    # Анонимный админ (сообщение от имени чата)
    if utils.is_anonymous_chat_admin(message):
        return True
    if message.from_user:
        return await _is_chat_admin(bot, message.chat.id, message.from_user.id)
    return False


async def _resolve_target(message: Message, args: list[str], bot: Bot):
    """
    Вернуть (user_tg_id, username, full_name) цели — из reply или @username.
    """
    # 1. Reply
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return (u.id, u.username or '', _full_name(u))
    # 2. @username из аргументов
    for a in args:
        if a.startswith('@'):
            uname = a[1:]
            # Пытаемся найти в нашей БД (по username)
            row = db.fetchone(
                "SELECT tg_id, username, first_name, last_name FROM users "
                "WHERE LOWER(username)=LOWER(?)", (uname,))
            if row:
                fn = " ".join(filter(None, [row.get('first_name'),
                                              row.get('last_name')])) or uname
                return (row['tg_id'], row.get('username') or uname, fn)
            # Не нашли в БД — попробуем как есть (Telegram не даёт résolve по username без доступа)
            return (None, uname, uname)
    return (None, None, None)


def _full_name(u) -> str:
    return " ".join(filter(None, [getattr(u, 'first_name', None),
                                   getattr(u, 'last_name', None)])) or "Пользователь"


def _mention(username: str, full_name: str, user_id=None) -> str:
    if username:
        return f"@{username}"
    if user_id:
        return f'<a href="tg://user?id={user_id}">{utils.escape_html(full_name or "пользователь")}</a>'
    return utils.escape_html(full_name or "пользователь")


# ===================== БАН =====================

@router.message(F.text.func(lambda t: t and t.strip().split()[0].lower() in
                             (BAN_WORDS | KICK_WORDS | MUTE_WORDS |
                              UNMUTE_WORDS | UNBAN_WORDS |
                              LOCK_WORDS | UNLOCK_WORDS)))
async def cmd_moderation(message: Message, bot: Bot):
    if not _is_group(message):
        return
    # Анонимный админ чата ИЛИ обычный админ
    if not await _can_moderate(message, bot):
        return
    parts = message.text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    # «бан список»
    if cmd in BAN_WORDS and args and args[0].lower() in ("список", "list"):
        await _show_banned_list(message)
        return

    # Закрыть/открыть чат
    if cmd in LOCK_WORDS:
        await _do_lock_chat(message, bot)
        return
    if cmd in UNLOCK_WORDS:
        await _do_unlock_chat(message, bot)
        return

    user_id, username, full_name = await _resolve_target(message, args, bot)

    if cmd in BAN_WORDS:
        await _do_ban(message, bot, user_id, username, full_name, args)
    elif cmd in KICK_WORDS:
        await _do_kick(message, bot, user_id, username, full_name)
    elif cmd in MUTE_WORDS:
        await _do_mute(message, bot, user_id, username, full_name, args)
    elif cmd in UNMUTE_WORDS:
        await _do_unmute(message, bot, user_id, username, full_name)
    elif cmd in UNBAN_WORDS:
        await _do_unban(message, bot, user_id, username, full_name)


async def _do_lock_chat(message: Message, bot: Bot):
    """Закрыть чат: нельзя писать/медиа/стикеры, но реакции и инвайты можно."""
    try:
        perms = ChatPermissions(
            can_send_messages=False,
            can_send_audios=False,
            can_send_documents=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_video_notes=False,
            can_send_voice_notes=False,
            can_send_polls=False,
            can_send_other_messages=False,   # стикеры/гифки
            can_add_web_page_previews=False,
            can_invite_users=True,            # добавлять людей можно
        )
        await bot.set_chat_permissions(message.chat.id, permissions=perms)
    except Exception as e:
        await message.reply(f"⚠️ Не смог закрыть чат: {e}\n\n"
                            f"Проверь что бот — админ с правом «Изменение профиля группы».")
        return
    await message.reply(
        "🔒 <b>Чат закрыт.</b>\n\n"
        "Писать, отправлять фото, голосовые и стикеры нельзя.\n"
        "Реакции и добавление участников — разрешены.\n\n"
        "Открыть: <code>+чат</code>", parse_mode="HTML")


async def _do_unlock_chat(message: Message, bot: Bot):
    """Открыть чат обратно."""
    try:
        perms = ChatPermissions(
            can_send_messages=True, can_send_audios=True,
            can_send_documents=True, can_send_photos=True,
            can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True,
            can_send_other_messages=True, can_add_web_page_previews=True,
            can_invite_users=True)
        await bot.set_chat_permissions(message.chat.id, permissions=perms)
    except Exception as e:
        await message.reply(f"⚠️ Не смог открыть чат: {e}")
        return
    await message.reply(
        "🔓 <b>Чат открыт!</b>\n\nМожно снова писать и общаться.",
        parse_mode="HTML")


async def _do_ban(message, bot, user_id, username, full_name, args):
    if not user_id:
        await message.reply(
            "Не понял кого банить. Ответь на сообщение юзера "
            "или укажи @username (юзер должен был писать боту).")
        return
    chat_id = message.chat.id
    # Длительность (опционально)
    dur_text = " ".join(a for a in args if not a.startswith('@'))
    seconds = mod.parse_duration(dur_text) if dur_text else None
    until_ts = None
    until_dt = None
    if seconds:
        until_dt = datetime.utcnow() + timedelta(seconds=seconds)
        until_ts = until_dt.isoformat()
    try:
        if until_dt:
            await bot.ban_chat_member(chat_id, user_id, until_date=until_dt)
        else:
            await bot.ban_chat_member(chat_id, user_id)
    except Exception as e:
        await message.reply(f"⚠️ Не смог забанить: {e}\n\n"
                            f"Проверь что бот — админ с правом «Блокировка участников».")
        return
    mod.record_action(chat_id, user_id, username, full_name, 'ban',
                       until_ts, message.from_user.id)
    dur_label = mod.humanize_duration(seconds) if seconds else "навсегда"
    await message.reply(
        f"🔨 {_mention(username, full_name, user_id)} забанен "
        f"<b>{dur_label}</b>.", parse_mode="HTML")


async def _do_kick(message, bot, user_id, username, full_name):
    if not user_id:
        await message.reply("Не понял кого кикнуть. Ответь на сообщение или @username.")
        return
    chat_id = message.chat.id
    try:
        # Кик = бан + разбан (чтобы мог вернуться)
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
    except Exception as e:
        await message.reply(f"⚠️ Не смог кикнуть: {e}\n\n"
                            f"Проверь права бота.")
        return
    await message.reply(
        f"👢 {_mention(username, full_name, user_id)} удалён из чата "
        f"(может вернуться по ссылке).", parse_mode="HTML")


async def _do_mute(message, bot, user_id, username, full_name, args):
    if not user_id:
        await message.reply("Не понял кого замутить. Ответь на сообщение или @username.")
        return
    chat_id = message.chat.id
    dur_text = " ".join(a for a in args if not a.startswith('@'))
    seconds = mod.parse_duration(dur_text) if dur_text else None
    until_dt = None
    until_ts = None
    if seconds:
        until_dt = datetime.utcnow() + timedelta(seconds=seconds)
        until_ts = until_dt.isoformat()
    perms = ChatPermissions(can_send_messages=False)
    try:
        if until_dt:
            await bot.restrict_chat_member(chat_id, user_id, permissions=perms,
                                            until_date=until_dt)
        else:
            await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    except Exception as e:
        await message.reply(f"⚠️ Не смог замутить: {e}\n\nПроверь права бота.")
        return
    mod.record_action(chat_id, user_id, username, full_name, 'mute',
                       until_ts, message.from_user.id)
    dur_label = mod.humanize_duration(seconds) if seconds else "навсегда"
    await message.reply(
        f"🔇 {_mention(username, full_name, user_id)} в муте на "
        f"<b>{dur_label}</b>.", parse_mode="HTML")


async def _do_unmute(message, bot, user_id, username, full_name):
    if not user_id:
        await message.reply("Не понял кого размутить. Ответь на сообщение или @username.")
        return
    chat_id = message.chat.id
    # Полные права обратно
    perms = ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True)
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    except Exception as e:
        await message.reply(f"⚠️ Не смог размутить: {e}")
        return
    mod.remove_action(chat_id, user_id, 'mute')
    await message.reply(
        f"🔊 {_mention(username, full_name, user_id)} снова может писать.",
        parse_mode="HTML")


async def _do_unban(message, bot, user_id, username, full_name):
    if not user_id:
        await message.reply(
            "Не понял кого разбанить. Ответь на сообщение или @username.")
        return
    chat_id = message.chat.id
    try:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except Exception as e:
        await message.reply(f"⚠️ Не смог разбанить: {e}")
        return
    mod.remove_action(chat_id, user_id, 'ban')
    await message.reply(
        f"✅ {_mention(username, full_name, user_id)} разбанен. "
        f"Может вернуться в чат.", parse_mode="HTML")


async def _show_banned_list(message: Message):
    chat_id = message.chat.id
    banned = mod.list_banned(chat_id)
    muted = mod.list_muted(chat_id)
    if not banned and not muted:
        await message.reply("📋 Список пуст — никто не забанен и не в муте.")
        return
    lines = []
    if banned:
        lines.append(f"🔨 <b>Забаненные ({len(banned)}):</b>")
        for b in banned:
            until = "навсегда"
            if b.get('until_ts'):
                try:
                    until = "до " + datetime.fromisoformat(
                        b['until_ts']).strftime("%d.%m.%Y")
                except Exception:
                    pass
            name = f"@{b['username']}" if b.get('username') else (b.get('full_name') or 'юзер')
            lines.append(f"• {name} — {until}")
    if muted:
        lines.append(f"\n🔇 <b>В муте ({len(muted)}):</b>")
        for m in muted:
            until = "навсегда"
            if m.get('until_ts'):
                try:
                    until = "до " + datetime.fromisoformat(
                        m['until_ts']).strftime("%d.%m.%Y %H:%M")
                except Exception:
                    pass
            name = f"@{m['username']}" if m.get('username') else (m.get('full_name') or 'юзер')
            lines.append(f"• {name} — {until}")
    await message.reply("\n".join(lines), parse_mode="HTML")


# ===================== АНТИ-ССЫЛКИ =====================

import re as _re
from datetime import timedelta as _td, datetime as _dt

# Чужие телеграм-ссылки: t.me/..., @channel, telegram.me/...
_LINK_PATTERNS = [
    _re.compile(r'(https?://)?t\.me/\S+', _re.IGNORECASE),
    _re.compile(r'(https?://)?telegram\.me/\S+', _re.IGNORECASE),
    _re.compile(r'(https?://)?telegram\.dog/\S+', _re.IGNORECASE),
]

LINK_WARN_LIMIT = 3
LINK_MUTE_SECONDS = 2 * 86400  # 2 дня


def _get_link_warns(chat_id: int, user_id: int) -> int:
    r = db.fetchone(
        "SELECT warns FROM link_warnings WHERE chat_id=? AND user_tg_id=?",
        (chat_id, user_id))
    return (r['warns'] if r else 0) or 0


def _add_link_warn(chat_id: int, user_id: int) -> int:
    cur = _get_link_warns(chat_id, user_id) + 1
    db.execute(
        """INSERT INTO link_warnings (chat_id, user_tg_id, warns, updated_at)
           VALUES (?,?,?,CURRENT_TIMESTAMP)
           ON CONFLICT(chat_id, user_tg_id) DO UPDATE SET
              warns=excluded.warns, updated_at=CURRENT_TIMESTAMP""",
        (chat_id, user_id, cur))
    return cur


def _reset_link_warns(chat_id: int, user_id: int):
    db.execute(
        "DELETE FROM link_warnings WHERE chat_id=? AND user_tg_id=?",
        (chat_id, user_id))


def _message_has_foreign_link(message: Message) -> bool:
    """Есть ли в сообщении чужая телеграм-ссылка или пересылка из канала."""
    # Пересылка из канала/чата
    if message.forward_from_chat is not None:
        return True
    # Текст и подпись
    text = (message.text or "") + " " + (message.caption or "")
    for pat in _LINK_PATTERNS:
        if pat.search(text):
            return True
    # Ссылки-сущности (entities) типа text_link на t.me
    entities = (message.entities or []) + (message.caption_entities or [])
    for e in entities:
        if getattr(e, 'type', None) == 'text_link' and e.url:
            low = e.url.lower()
            if 't.me/' in low or 'telegram.me/' in low or 'telegram.dog/' in low:
                return True
    return False


async def check_antilink(message: Message, bot: Bot):
    """Проверка чужих телеграм-ссылок. Вызывается из group_quiz.on_group_message."""
    # Сообщения от имени чата (анонимный админ) — не трогаем
    if utils.is_anonymous_chat_admin(message):
        return
    if not message.from_user:
        return
    if not _message_has_foreign_link(message):
        return
    # Админов чата и бота не трогаем
    try:
        if await _is_chat_admin(bot, message.chat.id, message.from_user.id):
            return
    except Exception:
        pass

    chat_id = message.chat.id
    user = message.from_user

    # Удаляем сообщение со ссылкой
    try:
        await bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    warns = _add_link_warn(chat_id, user.id)
    mention = _mention(user.username or '', _full_name(user), user.id)

    if warns >= LINK_WARN_LIMIT:
        # Мут на 2 дня
        until_dt = _dt.utcnow() + _td(seconds=LINK_MUTE_SECONDS)
        perms = ChatPermissions(can_send_messages=False)
        try:
            await bot.restrict_chat_member(chat_id, user.id, permissions=perms,
                                            until_date=until_dt)
        except Exception as e:
            log.warning("antilink mute: %s", e)
        mod.record_action(chat_id, user.id, user.username or '',
                          _full_name(user), 'mute', until_dt.isoformat(), 0)
        _reset_link_warns(chat_id, user.id)
        try:
            await bot.send_message(
                chat_id,
                f"🔇 {mention} получил <b>3/3</b> предупреждения за ссылки "
                f"и замучен на <b>2 дня</b>.", parse_mode="HTML")
        except Exception:
            pass
    else:
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ {mention}, ссылки на чужие каналы запрещены!\n"
                f"Предупреждение <b>{warns}/{LINK_WARN_LIMIT}</b>. "
                f"При 3 — мут на 2 дня.", parse_mode="HTML")
        except Exception:
            pass
