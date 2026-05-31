
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


def _is_group(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup")


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
                              UNMUTE_WORDS | UNBAN_WORDS)))
async def cmd_moderation(message: Message, bot: Bot):
    if not _is_group(message):
        return
    # Только админы бота
    if not utils.is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    # «бан список» / «ban список»
    if cmd in BAN_WORDS and args and args[0].lower() in ("список", "list"):
        await _show_banned_list(message)
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
