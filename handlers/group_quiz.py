"""
Хендлеры групповых квизов.

- callback "groupsend:{test_id}" → показать список групп где бот+админ
- callback "gqstart:{test_id}:{chat_id}" → запустить тест в выбранной группе
- callback "gq:join:{gq_id}" → игрок присоединяется к лобби
- /stop в группе → остановка
- chat_member events → отслеживание групп где бот
"""
import logging

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, Message, ChatMemberUpdated,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
import utils
from services import group_quiz_service, test_runner

router = Router(name="group_quiz")
log = logging.getLogger(__name__)


# ============ Отслеживание групп ============

@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot):
    """Бот добавлен/удалён/повышен в группе."""
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status if event.old_chat_member else None
    actor_id = event.from_user.id if event.from_user else None

    if new_status in ("kicked", "left"):
        db.execute("DELETE FROM known_groups WHERE chat_id=?", (chat.id,))
        log.info("Бот удалён из группы %s (%s)", chat.id, chat.title)
        return

    is_admin = new_status == "administrator"

    existing = db.fetchone("SELECT chat_id FROM known_groups WHERE chat_id=?", (chat.id,))
    if existing:
        db.execute(
            "UPDATE known_groups SET title=?, type=?, is_bot_admin=?, seen_at=CURRENT_TIMESTAMP "
            "WHERE chat_id=?",
            (chat.title or "", chat.type, 1 if is_admin else 0, chat.id))
    else:
        db.execute(
            """INSERT INTO known_groups (chat_id, title, type, added_by, is_bot_admin)
               VALUES (?,?,?,?,?)""",
            (chat.id, chat.title or "", chat.type, actor_id, 1 if is_admin else 0))
    log.info("Группа известна: %s (%s), бот-админ: %s", chat.id, chat.title, is_admin)

    # Бот только что добавлен в группу. Если добавил админ бота — пишем
    # приветственное сообщение со ссылкой на /launch_X для последних тестов
    just_added = old_status in (None, "left", "kicked") and new_status in ("member", "administrator")
    if just_added and actor_id and utils.is_admin(actor_id):
        try:
            recent = db.fetchall(
                "SELECT id, title FROM tests WHERE status='active' ORDER BY id DESC LIMIT 5")
            lines = [
                "👋 <b>Бот добавлен в группу!</b>\n",
                "Чтобы запустить тест в этой группе, отправьте команду:",
                "<code>/launch_&lt;test_id&gt;</code>\n",
            ]
            if recent:
                lines.append("Недавние тесты:")
                for r in recent:
                    title = (r['title'] or '—')[:50]
                    lines.append(f"• <code>/launch_{r['id']}</code> — {utils.escape_html(title)}")
            if not is_admin:
                lines.append(
                    "\n⚠️ Для корректной работы Quiz Poll сделайте бота "
                    "<b>администратором</b> в этой группе.")
            await bot.send_message(chat.id, "\n".join(lines), parse_mode="HTML")
        except Exception as e:
            log.warning("Не удалось отправить приветствие в %s: %s", chat.id, e)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(message: Message):
    """Любое сообщение в группе — обновляем seen_at и добавляем если новая."""
    if not message.chat or message.chat.type not in ("group", "supergroup"):
        return
    chat = message.chat
    existing = db.fetchone("SELECT chat_id FROM known_groups WHERE chat_id=?", (chat.id,))
    if not existing:
        db.execute(
            """INSERT OR IGNORE INTO known_groups (chat_id, title, type, seen_at)
               VALUES (?,?,?, CURRENT_TIMESTAMP)""",
            (chat.id, chat.title or "", chat.type))
    else:
        db.execute(
            "UPDATE known_groups SET title=?, seen_at=CURRENT_TIMESTAMP WHERE chat_id=?",
            (chat.title or "", chat.id))


# ============ /stop в группе ============

@router.message(Command("stop"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_stop_group(message: Message, bot: Bot):
    # Если сообщение от имени канала/чата — берём ID отправителя-юзера если есть
    # иначе разрешаем (это от имени самой группы)
    requester_tg_id = message.from_user.id if message.from_user else None
    if requester_tg_id is None and message.sender_chat:
        # Сообщение от имени чата — считаем что отправил админ группы
        requester_tg_id = 0  # placeholder, ниже разрешим если sender_chat == текущий чат
        if message.sender_chat.id == message.chat.id:
            # От имени той же группы → разрешаем сразу
            ok, key = await group_quiz_service.stop_quiz(
                bot, message.chat.id, requester_tg_id=0)
            # force-stop: убираем проверку
            if not ok and key == "no_rights":
                # Принудительный стоп для anonymous group admin
                gq = __import__('database').fetchone(
                    "SELECT id FROM group_quizzes WHERE chat_id=? AND status IN ('lobby','running')",
                    (message.chat.id,))
                if gq:
                    await group_quiz_service._cancel_quiz_timers(gq['id'])
                    __import__('database').execute(
                        "UPDATE group_quizzes SET status='cancelled', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                        (gq['id'],))
                    try:
                        await message.reply("🛑 Тест остановлен.")
                    except Exception:
                        pass
            return

    if requester_tg_id is None:
        return

    ok, key = await group_quiz_service.stop_quiz(bot, message.chat.id, requester_tg_id)
    if not ok:
        if key == "no_active":
            return  # молча игнорим — нет активного теста
        if key == "no_rights":
            try:
                await message.reply("⛔ Команду /stop может выполнить только админ группы, "
                                    "автор теста или администратор бота.")
            except Exception:
                pass


# ============ Запуск теста в группу ============

@router.callback_query(F.data.startswith("groupsend:"))
async def cb_group_send(call: CallbackQuery):
    """
    Админ нажал «Отправить в группу» в личке.
    Показываем инструкцию + кнопку «Добавить бота в группу».
    """
    log.info("cb_group_send triggered: callback_data=%s from user=%s",
             call.data, call.from_user.id if call.from_user else None)
    try:
        test_id = int(call.data.split(":")[1])
    except (ValueError, IndexError) as e:
        log.warning("groupsend parse error: %s", e)
        await call.answer("Ошибка callback.", show_alert=True)
        return

    if not utils.is_admin(call.from_user.id):
        await call.answer("⛔ Только администратор бота может отправлять тесты в группы.",
                          show_alert=True)
        return

    try:
        test = test_runner.get_test(test_id)
    except Exception as e:
        log.exception("groupsend get_test error: %s", e)
        await call.answer(f"Ошибка БД: {e}", show_alert=True)
        return

    if not test:
        await call.answer("Тест не найден.", show_alert=True)
        return

    try:
        bot_user = await call.bot.me()
        bot_username = bot_user.username
    except Exception as e:
        log.exception("groupsend get_me error: %s", e)
        await call.answer(f"Ошибка: {e}", show_alert=True)
        return

    title = utils.escape_html(test.get('title') or '—')
    text = (
        f"📤 <b>Запуск теста «{title}» в группе</b>\n\n"
        f"<b>Способ 1 — добавить бота:</b>\n"
        f"Нажмите кнопку ниже → выберите группу → бот добавится и сразу запустит тест.\n\n"
        f"<b>Способ 2 — бот уже в группе:</b>\n"
        f"Зайдите в группу и отправьте там команду:\n"
        f"<code>/launch_{test_id}</code>\n\n"
        f"⚠️ Запустить тест может только администратор бота. "
        f"Бот должен быть в группе администратором, иначе Quiz Poll "
        f"не сможет отслеживать ответы участников."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить бота в группу",
            url=f"https://t.me/{bot_username}?startgroup=launch_{test_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Назад к тесту",
            callback_data=f"opentest:{test_id}",
        )],
    ])

    try:
        await call.message.answer(text, reply_markup=kb,
                                    parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.exception("groupsend answer error: %s", e)
        await call.answer(f"Ошибка отправки: {e}", show_alert=True)
        return

    await call.answer()


@router.message(F.text.regexp(r"^/launch_(\d+)(@\w+)?$").as_("m"),
                 F.chat.type.in_({"group", "supergroup"}))
async def cmd_launch_test(message: Message, m):
    """
    /launch_<test_id> в группе — запуск теста.
    Может быть отправлено:
      • Лично админом бота (message.from_user.id ∈ admins)
      • От имени группы (anonymous admin) — разрешаем
      • Из связанного канала — разрешаем
    """
    # Определяем кто отправил
    from_user_id = message.from_user.id if message.from_user else None
    is_anonymous_admin = (message.sender_chat is not None
                           and message.sender_chat.id == message.chat.id)
    is_channel_repost = (message.sender_chat is not None
                          and message.sender_chat.id != message.chat.id)
    is_bot_admin = utils.is_admin(from_user_id) if from_user_id else False

    allowed = is_bot_admin or is_anonymous_admin or is_channel_repost

    if not allowed:
        try:
            warn = await message.reply(
                "⛔ Запустить тест может только администратор бота.")
            import asyncio
            async def _cleanup():
                await asyncio.sleep(5)
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await warn.delete()
                except Exception:
                    pass
            asyncio.create_task(_cleanup())
        except Exception:
            pass
        return

    # ID для started_by — если есть юзер, берём его, иначе ID чата
    starter_id = from_user_id if from_user_id else message.chat.id

    try:
        test_id = int(m.group(1))
    except (ValueError, AttributeError):
        return

    test = test_runner.get_test(test_id)
    if not test:
        await message.reply("❌ Тест не найден.")
        return

    # Записываем группу
    db.execute(
        """INSERT OR IGNORE INTO known_groups (chat_id, title, type, added_by, seen_at)
           VALUES (?,?,?,?, CURRENT_TIMESTAMP)""",
        (message.chat.id, message.chat.title or "",
         message.chat.type, starter_id))

    ok, key, gq_id = await group_quiz_service.start_lobby(
        message.bot, dict(test), message.chat.id, starter_id)

    if not ok:
        if key == "already_running":
            try:
                await message.reply(
                    "⚠️ В этой группе уже идёт тест. Сначала /stop.")
            except Exception:
                pass
        else:
            try:
                await message.reply(f"❌ Не удалось запустить: {key}")
            except Exception:
                pass
        return

    # Удаляем команду /launch_X — она больше не нужна
    try:
        await message.delete()
    except Exception:
        pass


# ============ Игрок присоединяется ============

@router.callback_query(F.data.startswith("gq:join:"))
async def cb_gq_join(call: CallbackQuery):
    try:
        gq_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return

    ok, key = await group_quiz_service.join_player(call.bot, gq_id, call.from_user)
    if ok:
        await call.answer("✅ Вы в игре!")
    else:
        msgs = {
            "not_found": "Сессия не найдена.",
            "already_running": "Тест уже идёт — присоединиться нельзя.",
            "finished": "Тест уже завершён.",
            "already_in": "Вы уже в списке.",
        }
        await call.answer(msgs.get(key, "Не удалось присоединиться."), show_alert=False)
