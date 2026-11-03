"""
Обязательная подписка в группах: админка в боте и кнопка «✅ Проверить подписку».

Логика проверки — services/sub_gate.py. Здесь только экраны:
каналы для подписки, основные группы, исключения, статистика с выгрузкой,
настройки. Раздел виден владельцу бота (уровень 3).
"""
import asyncio
import logging
from datetime import date

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from filters import IsOwner
from services import sub_gate as sg

router = Router(name="sub_gate")
log = logging.getLogger(__name__)


class GateStates(StatesGroup):
    add_channel = State()     # @username, ссылка, ID или пересланный пост
    channel_link = State()    # ссылка для вступления
    exempt = State()          # ID / @username / пересланное сообщение человека
    period = State()          # свой период статистики


STATE_ICON = {"ok": "🟢", "error": "🔴", "unknown": "⚪"}


def _n(x) -> str:
    return f"{int(x):,}".replace(",", " ")


def _pct(x) -> str:
    return f"{float(x):.1f}".replace(".", ",") + "%"


def _ch_icon(ch: dict) -> str:
    if not int(ch.get("is_active") or 0):
        return "🔴" if (ch.get("disabled_reason") or "") == "check" else "🟡"
    return STATE_ICON.get(ch.get("check_state") or "unknown", "⚪")


def _ch_state_text(ch: dict) -> str:
    if not int(ch.get("is_active") or 0):
        if (ch.get("disabled_reason") or "") == "check":
            return ("🔴 Проверка НЕ работает — канал выключен, пока бот не сможет его проверять: "
                    f"{sg._h(ch.get('check_error') or '')}")
        return "🟡 Канал отключён"
    st = ch.get("check_state") or "unknown"
    if st == "ok":
        return "🟢 Проверка работает"
    if st == "error":
        return f"🔴 Проверка НЕ работает: {sg._h(ch.get('check_error') or '')}"
    return "⚪ Ещё не проверялась"


def _ref(ch: dict) -> str:
    return f"@{ch['username']}" if ch.get("username") else f"ID {ch['chat_id']}"


async def _show(call: CallbackQuery, text: str, kb=None):
    markup = kb.as_markup() if hasattr(kb, "as_markup") else kb
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        if "message is not modified" in str(e):
            return
        try:
            await call.message.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e2:
            log.warning("подписка, экран: %s", e2)


async def _answer(call: CallbackQuery, text: str = None, alert: bool = False):
    try:
        await call.answer(text, show_alert=alert)
    except Exception:
        pass


# ───────────────────────── кнопка в группе ─────────────────────────

@router.callback_query(F.data.startswith("gate:chk:"))
async def cb_gate_check(call: CallbackQuery, bot: Bot):
    try:
        chat_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await _answer(call)
        return
    user = call.from_user
    try:
        verdict = await sg.check_button(bot, chat_id, user)
    except Exception as e:
        log.warning("подписка: кнопка проверки: %s", e)
        await _answer(call, "Не получилось проверить. Попробуйте через минуту.", True)
        return
    if verdict.get("ok"):
        text = "✅ Подписка подтверждена!\nТеперь вы можете писать сообщения в группе."
        if verdict.get("exempt"):
            text = "✅ Вам писать в группе можно."
        await _answer(call, text, True)
        m = await asyncio.to_thread(sg.get_member, chat_id, user.id) or {}
        if call.message and m.get("notice_msg_id") == call.message.message_id:
            try:
                await call.message.edit_text(
                    f'✅ <a href="tg://user?id={user.id}">{sg._h(user.first_name or "Участник")}</a>: '
                    "подписка подтверждена, можно писать.", parse_mode="HTML")
            except Exception:
                pass
        return
    missing = verdict.get("missing") or []
    names = ", ".join((ch.get("title") or str(ch["chat_id"])) for ch in missing)
    text = "❌ Вы подписались ещё не на все обязательные каналы."
    if names:
        text += f"\n\nОсталось: {names}"
    await _answer(call, text[:190], True)
    m = await asyncio.to_thread(sg.get_member, chat_id, user.id) or {}
    if call.message and m.get("notice_msg_id") == call.message.message_id:
        # Своё уведомление — оставляем в нём только неоформленные подписки
        try:
            await call.message.edit_reply_markup(reply_markup=sg.notice_kb(chat_id, missing))
        except Exception:
            pass
    else:
        await sg.send_notice(bot, chat_id, user, verdict,
                             call.message.message_thread_id if call.message and call.message.is_topic_message else None)


# ───────────────────────── главное меню ─────────────────────────

def _menu_data() -> dict:
    chans = sg.list_channels()
    groups = sg.known_groups()
    return {"chans": chans, "groups": groups, "exempt": len(sg.list_exempt()), "problems": sg.system_problems()}


@router.callback_query(F.data == "sg:menu", IsOwner())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    d = await asyncio.to_thread(_menu_data)
    on = [g for g in d["groups"] if g.get("enabled")]
    active = [c for c in d["chans"] if c.get("is_active")]
    lines = ["🔐 <b>Обязательная подписка в группах</b>\n",
             "Пока участник основной группы не подписан на все обязательные каналы, писать он не может: "
             "бот удаляет его сообщения, ограничивает право писать и показывает кнопки с каналами. "
             "Как только Telegram подтверждает подписку, бот сам разрешает писать.\n",
             f"👥 Групп с проверкой: <b>{len(on)}</b> из {len(d['groups'])}",
             f"📢 Каналов для подписки: <b>{len(d['chans'])}</b> (активных {len(active)})",
             f"🙋 Исключений: <b>{d['exempt']}</b>"]
    lines.append("Режим: " + ("⛔ строгий" if sg.setting("gate_strict") else "⚠️ «пропускать» непроверяемые каналы"))
    if d["chans"]:
        lines.append("\n<b>Каналы</b>")
        for c in d["chans"]:
            lines.append(f"{_ch_icon(c)} {sg._h(c['title'] or _ref(c))} — {_ch_state_text(c)}")
    if d["problems"]:
        lines.append("\n🔴 <b>Сейчас защита не полная:</b>")
        lines += [f"• {p}" for p in d["problems"][:12]]
    else:
        lines.append("\n🟢 <b>Защита работает во всех группах с проверкой.</b>")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Проверить систему подписок", callback_data="sg:diag")
    kb.button(text="📢 Каналы для подписки", callback_data="sg:chs")
    kb.button(text="👥 Основные группы", callback_data="sg:grs")
    kb.button(text="🙋 Исключения", callback_data="sg:ex")
    kb.button(text="🧾 Журнал проверок", callback_data="sg:log:0")
    kb.button(text="📊 Статистика", callback_data="sg:st:ov:7d:0")
    kb.button(text="⚙️ Настройки", callback_data="sg:set")
    kb.button(text="⬅️ Назад", callback_data="m:admin")
    kb.adjust(1, 1, 1, 2, 2, 1)
    await _show(call, "\n".join(lines), kb)
    await _answer(call)


# ───────────────────────── каналы ─────────────────────────

@router.callback_query(F.data == "sg:chs", IsOwner())
async def cb_channels(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_channels(call)
    await _answer(call)


async def _render_channels(call: CallbackQuery, note: str = ""):
    chans = await asyncio.to_thread(sg.list_channels)
    lines = ["📢 <b>Каналы и группы для обязательной подписки</b>\n",
             "Порядок в списке — порядок кнопок у участника.\n"]
    kb = InlineKeyboardBuilder()
    if not chans:
        lines.append("<i>Пока пусто. Добавьте первый канал.</i>")
    for i, c in enumerate(chans, 1):
        scope = "для всех групп" if c.get("all_groups") else "для выбранных групп"
        lines.append(f"{i}. {_ch_icon(c)} {sg._h(c['title'] or '')} ({_ref(c)}) — {scope}")
        kb.button(text=f"{_ch_icon(c)} {i}. {(c['title'] or _ref(c))[:40]}", callback_data=f"sg:ch:{c['id']}")
    kb.button(text="➕ Добавить канал или группу", callback_data="sg:chadd")
    kb.button(text="⬅️ Назад", callback_data="sg:menu")
    kb.adjust(1)
    lines.append("\n🟢 проверка работает · 🔴 проверка НЕ работает · 🟡 канал отключён · ⚪ ещё не проверялся")
    await _show(call, (note + "\n\n" if note else "") + "\n".join(lines), kb)


def _channel_card(ch: dict) -> tuple:
    snap = sg.snapshot(force=True)
    groups = sg.known_groups()
    using = [g for g in groups if any(int(c["chat_id"]) == int(ch["chat_id"]) for c in sg.channels_for(
        {"channels": [ch], "links": snap["links"]}, g["chat_id"]))]
    gated = [g for g in using if g.get("enabled")]
    link = sg.channel_url(ch)
    lines = [f"{'📢' if ch.get('kind') == 'channel' else '👥'} <b>{sg._h(ch['title'] or _ref(ch))}</b>\n",
             f"Тип: {'канал' if ch.get('kind') == 'channel' else 'группа'}",
             f"Username: {'@' + ch['username'] if ch.get('username') else '—'}",
             f"ID: <code>{ch['chat_id']}</code>",
             f"Ссылка для вступления: {sg._h(link) if link else '⚠️ нет — участник не увидит кнопку'}",
             f"Статус: {'🟢 активен' if ch.get('is_active') else '🟡 отключён'}",
             f"Проверка: {_ch_state_text(ch)}",
             f"Где требуется: {'во всех группах с проверкой' if ch.get('all_groups') else 'в выбранных группах'}"
             f" (сейчас групп с проверкой: {len(gated)})"]
    if ch.get("checked_at"):
        lines.append(f"<i>Проверено: {sg.fmt_local(ch['checked_at'])}</i>")
    kb = InlineKeyboardBuilder()
    cid = ch["id"]
    kb.button(text="⏸ Отключить" if ch.get("is_active") else "▶️ Включить", callback_data=f"sg:chtog:{cid}")
    kb.button(text="⬆️ Выше", callback_data=f"sg:chup:{cid}")
    kb.button(text="⬇️ Ниже", callback_data=f"sg:chdn:{cid}")
    kb.button(text="🔄 Проверить права бота", callback_data=f"sg:chchk:{cid}")
    kb.button(text="🔗 Изменить ссылку", callback_data=f"sg:chlink:{cid}")
    kb.button(text="👥 В каких группах", callback_data=f"sg:chgr:{cid}")
    kb.button(text="🗑 Убрать из списка", callback_data=f"sg:chdel:{cid}")
    kb.button(text="⬅️ К списку", callback_data="sg:chs")
    kb.adjust(1, 2, 1, 1, 1, 1, 1)
    return "\n".join(lines), kb


async def _open_channel(call: CallbackQuery, ch_id: int, note: str = ""):
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    if not ch or ch.get("is_deleted"):
        await _answer(call, "Канал уже убран из списка.", True)
        return
    text, kb = await asyncio.to_thread(_channel_card, ch)
    await _show(call, (note + "\n\n" if note else "") + text, kb)


@router.callback_query(F.data.startswith("sg:ch:"), IsOwner())
async def cb_channel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _open_channel(call, int(call.data.split(":")[2]))
    await _answer(call)


@router.callback_query(F.data.startswith("sg:chtog:"), IsOwner())
async def cb_channel_toggle(call: CallbackQuery, bot: Bot):
    ch_id = int(call.data.split(":")[2])
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    if not ch:
        await _answer(call)
        return
    on = not bool(ch.get("is_active"))
    if on:
        res = await sg.check_channel_access(bot, ch, call.from_user.id)
        if not res["ok"]:
            await _answer(call, (sg.CANT_CHECK + " " + res["text"])[:195], True)
            await _open_channel(call, ch_id)
            return
    await asyncio.to_thread(sg.update_channel, ch_id, is_active=1 if on else 0, disabled_reason="")
    if not on:
        asyncio.create_task(sg.recheck_restricted(bot, 200))   # кто ограничен только из-за него — откроем
    await _answer(call, "Канал включён: проверка работает" if on else "Канал отключён: подписка на него сейчас не требуется")
    await _open_channel(call, ch_id)


@router.callback_query(F.data.startswith("sg:chup:") | F.data.startswith("sg:chdn:"), IsOwner())
async def cb_channel_move(call: CallbackQuery):
    _, op, ch_id = call.data.split(":")
    await asyncio.to_thread(sg.move_channel, int(ch_id), -1 if op == "chup" else 1)
    await _answer(call, "Порядок изменён")
    await _open_channel(call, int(ch_id))


@router.callback_query(F.data.startswith("sg:chchk:"), IsOwner())
async def cb_channel_check(call: CallbackQuery, bot: Bot):
    ch_id = int(call.data.split(":")[2])
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    if not ch:
        await _answer(call)
        return
    res = await sg.check_channel_access(bot, ch, call.from_user.id)
    if res["ok"]:
        msg = "🟢 Проверка работает" + (": канал снова включён" if res.get("activated") else "")
    else:
        msg = (sg.CANT_CHECK + " " + res["text"])[:195]
    await _answer(call, msg, not res["ok"])
    await _open_channel(call, ch_id)


@router.callback_query(F.data.startswith("sg:chlink:"), IsOwner())
async def cb_channel_link(call: CallbackQuery, state: FSMContext):
    ch_id = int(call.data.split(":")[2])
    await state.set_state(GateStates.channel_link)
    await state.update_data(gate_channel_id=ch_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Создать ссылку ботом", callback_data=f"sg:chmk:{ch_id}")
    kb.button(text="Отмена", callback_data=f"sg:ch:{ch_id}")
    kb.adjust(1)
    await _show(call, "🔗 Пришлите ссылку для вступления (например https://t.me/+AbCdEf…).\n\n"
                      "Для приватного канала бот может создать ссылку сам, если он администратор "
                      "с правом приглашать участников.", kb)
    await _answer(call)


@router.callback_query(F.data.startswith("sg:chmk:"), IsOwner())
async def cb_channel_make_link(call: CallbackQuery, state: FSMContext, bot: Bot):
    ch_id = int(call.data.split(":")[2])
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    link = await sg.make_invite_link(bot, ch) if ch else ""
    if not link:
        await _answer(call, "Бот не смог создать ссылку: нужен админ канала с правом «Пригласительные ссылки». "
                            "Пришлите ссылку сами.", True)
        return
    await asyncio.to_thread(sg.update_channel, ch_id, invite_link=link)
    await state.clear()
    await _answer(call, "Ссылка создана")
    await _open_channel(call, ch_id)


@router.message(GateStates.channel_link, IsOwner())
async def msg_channel_link(message: Message, state: FSMContext):
    data = await state.get_data()
    ch_id = data.get("gate_channel_id")
    link = (message.text or "").strip()
    if not link.startswith(("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/")):
        await message.answer("Это не похоже на ссылку Telegram. Пришлите ссылку вида https://t.me/… или нажмите «Отмена».")
        return
    if link.startswith("t.me/"):
        link = "https://" + link
    await asyncio.to_thread(sg.update_channel, ch_id, invite_link=link)
    await state.clear()
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    text, kb = await asyncio.to_thread(_channel_card, ch)
    await message.answer("✅ Ссылка сохранена.\n\n" + text, parse_mode="HTML", reply_markup=kb.as_markup(),
                         disable_web_page_preview=True)


@router.callback_query(F.data.startswith("sg:chdel:"), IsOwner())
async def cb_channel_delete(call: CallbackQuery):
    ch_id = int(call.data.split(":")[2])
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    if not ch:
        await _answer(call)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, убрать", callback_data=f"sg:chdel2:{ch_id}")
    kb.button(text="Отмена", callback_data=f"sg:ch:{ch_id}")
    kb.adjust(2)
    await _show(call, f"Убрать «{sg._h(ch['title'] or _ref(ch))}» из обязательных?\n\n"
                      "Статистика по каналу сохранится. Участников, которые ограничены только из-за него, "
                      "бот откроет сам.", kb)
    await _answer(call)


@router.callback_query(F.data.startswith("sg:chdel2:"), IsOwner())
async def cb_channel_delete2(call: CallbackQuery, bot: Bot):
    ch_id = int(call.data.split(":")[2])
    await asyncio.to_thread(sg.delete_channel, ch_id)
    asyncio.create_task(sg.recheck_restricted(bot, 200))
    await _answer(call, "Канал убран из списка")
    await _render_channels(call, "🗑 Канал убран из обязательных. Статистика по нему сохранена.")


@router.callback_query(F.data.startswith("sg:chgr:"), IsOwner())
async def cb_channel_groups(call: CallbackQuery):
    ch_id = int(call.data.split(":")[2])
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    if not ch:
        await _answer(call)
        return
    snap = await asyncio.to_thread(sg.snapshot, True)
    groups = await asyncio.to_thread(sg.known_groups)
    kb = InlineKeyboardBuilder()
    kb.button(text=("✅" if ch.get("all_groups") else "⬜") + " Во всех группах с проверкой",
              callback_data=f"sg:chall:{ch_id}")
    lines = [f"👥 <b>Где требуется подписка на «{sg._h(ch['title'] or _ref(ch))}»</b>\n",
             "Отметьте группы. «Во всех группах» — канал сразу действует и в группах, где проверку включат позже.\n"]
    for g in groups:
        applies = any(int(c["chat_id"]) == int(ch["chat_id"])
                      for c in sg.channels_for({"channels": [ch], "links": snap["links"]}, g["chat_id"]))
        mark = "✅" if applies else "⬜"
        state_txt = "проверка включена" if g.get("enabled") else "проверка выключена"
        lines.append(f"{mark} {sg._h(g.get('title') or g['chat_id'])} — {state_txt}")
        kb.button(text=f"{mark} {(g.get('title') or str(g['chat_id']))[:40]}",
                  callback_data=f"sg:chg:{ch_id}:{g['chat_id']}")
    kb.button(text="⬅️ К каналу", callback_data=f"sg:ch:{ch_id}")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb)
    await _answer(call)


@router.callback_query(F.data.startswith("sg:chall:"), IsOwner())
async def cb_channel_all(call: CallbackQuery, bot: Bot):
    ch_id = int(call.data.split(":")[2])
    ch = await asyncio.to_thread(sg.get_channel, ch_id)
    if not ch:
        await _answer(call)
        return
    on = not bool(ch.get("all_groups"))

    def _apply():
        sg.update_channel(ch_id, all_groups=1 if on else 0)
        db.execute("DELETE FROM gate_links WHERE channel_chat_id=?", (int(ch["chat_id"]),))
        sg.invalidate()
    await asyncio.to_thread(_apply)
    asyncio.create_task(sg.recheck_restricted(bot, 200))
    await cb_channel_groups(call)


@router.callback_query(F.data.startswith("sg:chg:"), IsOwner())
async def cb_channel_group_toggle(call: CallbackQuery, bot: Bot):
    _, _, ch_id, chat_id = call.data.split(":")
    ch = await asyncio.to_thread(sg.get_channel, int(ch_id))
    if not ch:
        await _answer(call)
        return
    snap = await asyncio.to_thread(sg.snapshot, True)
    applies = any(int(c["chat_id"]) == int(ch["chat_id"])
                  for c in sg.channels_for({"channels": [ch], "links": snap["links"]}, int(chat_id)))
    await asyncio.to_thread(sg.set_link, int(chat_id), int(ch["chat_id"]), not applies)
    asyncio.create_task(sg.recheck_restricted(bot, 200))
    await cb_channel_groups(call)


# ── добавление канала ──

@router.callback_query(F.data == "sg:chadd", IsOwner())
async def cb_channel_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(GateStates.add_channel)
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="sg:chs")
    await _show(call, "➕ <b>Новый канал или группа для подписки</b>\n\n"
                      "Пришлите одно из:\n"
                      "• @username канала или группы;\n"
                      "• ссылку вида https://t.me/имя;\n"
                      "• ID, например <code>-1001234567890</code>;\n"
                      "• или перешлите сюда любой пост из канала — так можно добавить и приватный канал.\n\n"
                      "Бот должен быть <b>администратором</b> канала: без этого Telegram не даёт проверять "
                      "подписчиков. В группе для подписки бот должен быть участником.", kb)
    await _answer(call)


def _forwarded_chat(message: Message):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is None:
        chat = getattr(message, "forward_from_chat", None)
    return chat


@router.message(GateStates.add_channel, IsOwner())
async def msg_channel_add(message: Message, state: FSMContext, bot: Bot):
    fwd = _forwarded_chat(message)
    ref = str(fwd.id) if fwd is not None else (message.text or "").strip()
    info = await sg.resolve_resource(bot, ref)
    if info.get("error"):
        kb = InlineKeyboardBuilder()
        kb.button(text="Отмена", callback_data="sg:chs")
        await message.answer("⚠️ " + info["error"], reply_markup=kb.as_markup())
        return
    ch = await asyncio.to_thread(sg.save_channel, info, message.from_user.id)
    access = await sg.check_channel_access(bot, ch, message.from_user.id)
    if not access["ok"]:
        # Нельзя молча считать канал рабочим, если бот не умеет проверять его подписчиков
        await asyncio.to_thread(sg.update_channel, ch["id"], is_active=0, disabled_reason="check")
    if not sg.channel_url(ch):
        link = await sg.make_invite_link(bot, ch)
        if link:
            await asyncio.to_thread(sg.update_channel, ch["id"], invite_link=link)
    ch = await asyncio.to_thread(sg.get_channel, ch["id"])
    await state.clear()
    if access["ok"]:
        note = "✅ Добавлено. 🟢 Проверка работает: бот видит подписчиков канала."
    else:
        note = (f"{sg.CANT_CHECK}\n\n🔴 {sg._h(access['text'])}\n\nКанал сохранён <b>выключенным</b>. Выдайте права "
                "и нажмите «🔄 Проверить права бота» — когда проверка пройдёт, канал включится сам.")
    if not sg.channel_url(ch):
        note += "\n\n⚠️ Нет ссылки для вступления: нажмите «🔗 Изменить ссылку» и пришлите её."
        await state.set_state(GateStates.channel_link)
        await state.update_data(gate_channel_id=ch["id"])
    text, kb = await asyncio.to_thread(_channel_card, ch)
    await message.answer(note + "\n\n" + text, parse_mode="HTML", reply_markup=kb.as_markup(),
                         disable_web_page_preview=True)


# ───────────────────────── группы ─────────────────────────

@router.callback_query(F.data == "sg:grs", IsOwner())
async def cb_groups(call: CallbackQuery, state: FSMContext):
    await state.clear()
    groups = await asyncio.to_thread(sg.known_groups)
    lines = ["👥 <b>Основные группы</b>\n",
             "Здесь группы, где есть бот. Включите проверку подписки в нужных.\n"]
    kb = InlineKeyboardBuilder()
    if not groups:
        lines.append("<i>Бот пока не видит ни одной группы. Добавьте его в группу администратором.</i>")
    probs = {g["chat_id"]: await asyncio.to_thread(sg.group_problems, g["chat_id"]) for g in groups if g.get("enabled")}
    for g in groups:
        icon = "🟢" if g.get("enabled") else "⚪"
        warn = " ⚠️" if probs.get(g["chat_id"]) else ""
        lines.append(f"{icon} {sg._h(g.get('title') or g['chat_id'])} <code>{g['chat_id']}</code>{warn}")
        kb.button(text=f"{icon} {(g.get('title') or str(g['chat_id']))[:40]}{warn}", callback_data=f"sg:gr:{g['chat_id']}")
    lines.append("\n🟢 проверка включена · ⚪ выключена · ⚠️ защита не полная (откройте группу)")
    kb.button(text="⬅️ Назад", callback_data="sg:menu")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb)
    await _answer(call)


def _group_card(chat_id: int) -> tuple:
    g = sg.get_group(chat_id)
    snap = sg.snapshot(force=True)
    chans = sg.list_channels()
    applied = {int(c["chat_id"]) for c in sg.channels_for({"channels": [c for c in chans if c.get("is_active")],
                                                           "links": snap["links"]}, chat_id)}
    restricted = db.fetchone("SELECT COUNT(*) AS n FROM gate_members WHERE chat_id=? AND restricted=1", (int(chat_id),))
    blocked = db.fetchone("SELECT COUNT(*) AS n FROM gate_members WHERE chat_id=? AND status IN ('blocked','relocked') "
                          "AND left_at IS NULL", (int(chat_id),))
    rs = g.get("rights_state") or "unknown"
    rights = {"ok": "✅ все нужные права есть", "unknown": "⚪ ещё не проверялись"}.get(
        rs, f"⚠️ не хватает: {sg._h(g.get('rights_missing') or '')}")
    problems = sg.group_problems(chat_id)
    lines = [f"👥 <b>{sg._h(g.get('title') or chat_id)}</b>  <code>{chat_id}</code>\n",
             f"Проверка подписки: {'🟢 включена' if g.get('enabled') else '⚪ выключена'}",
             f"Права бота: {rights}"]
    if g.get("enabled"):
        if problems:
            lines.append("\n🔴 <b>Защита не полная:</b>\n" + "\n".join(f"• {p}" for p in problems))
        else:
            lines.append("\n🟢 <b>Защита работает:</b> без подписки на все каналы писать в группе нельзя.")
    if g.get("checked_at"):
        lines.append(f"<i>Права проверены: {sg.fmt_local(g['checked_at'])}</i>")
    lines.append("\nНужные права администратора для бота:\n• Удаление сообщений\n"
                 "• Блокировка участников (ограничение и изменение их разрешений)")
    lines.append("\n<b>Каналы для этой группы</b> (нажмите, чтобы включить или выключить):")
    kb = InlineKeyboardBuilder()
    kb.button(text="⚪ Выключить проверку" if g.get("enabled") else "🟢 Включить проверку",
              callback_data=f"sg:grtog:{chat_id}")
    kb.button(text="🔄 Проверить права бота", callback_data=f"sg:grchk:{chat_id}")
    if not chans:
        lines.append("<i>Каналов пока нет — добавьте их в разделе «Каналы для подписки».</i>")
    for c in chans:
        on = int(c["chat_id"]) in applied
        mark = "✅" if on else "⬜"
        extra = "" if c.get("is_active") else " (канал отключён)"
        lines.append(f"{mark} {sg._h(c['title'] or _ref(c))}{extra}")
        kb.button(text=f"{mark} {(c['title'] or _ref(c))[:40]}", callback_data=f"sg:grc:{chat_id}:{c['id']}")
    lines.append(f"\nСейчас не подписаны: {_n(blocked['n'] if blocked else 0)} · ограничены ботом: "
                 f"{_n(restricted['n'] if restricted else 0)}")
    kb.button(text="🧾 Журнал проверок группы", callback_data=f"sg:log:{chat_id}")
    kb.button(text="📊 Статистика группы", callback_data=f"sg:st:ov:7d:{chat_id}")
    kb.button(text="⬅️ К группам", callback_data="sg:grs")
    kb.adjust(1)
    return "\n".join(lines), kb


async def _open_group(call: CallbackQuery, chat_id: int, note: str = ""):
    text, kb = await asyncio.to_thread(_group_card, chat_id)
    await _show(call, (note + "\n\n" if note else "") + text, kb)


@router.callback_query(F.data.startswith("sg:gr:"), IsOwner())
async def cb_group(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _open_group(call, int(call.data.split(":")[2]))
    await _answer(call)


@router.callback_query(F.data.startswith("sg:grtog:"), IsOwner())
async def cb_group_toggle(call: CallbackQuery, bot: Bot):
    chat_id = int(call.data.split(":")[2])
    g = await asyncio.to_thread(sg.get_group, chat_id)
    on = not bool(g.get("enabled"))
    note = ""
    if on:
        res = await sg.check_group_rights(bot, chat_id)
        await asyncio.to_thread(sg.set_group_enabled, chat_id, True)
        if res["ok"]:
            note = "🟢 Проверка включена."
        else:
            need = "\n".join(f"• {x}" for x in res["missing"]) or sg._h(res["error"])
            note = ("🟢 Проверка включена, но бот не сможет удалять сообщения и ограничивать участников, пока "
                    f"не получит права администратора:\n{need}")
        if not res.get("supergroup", True):
            note += ("\n\n⚠️ Это обычная группа: Telegram не даёт ограничивать в ней участников. Бот будет только "
                     "удалять сообщения. Чтобы ограничения работали, сделайте её супергруппой (например, "
                     "включите видимость истории для новых участников).")
    else:
        await asyncio.to_thread(sg.set_group_enabled, chat_id, False)
        asyncio.create_task(sg.release_disabled(bot, 500))
        note = "⚪ Проверка выключена. Ограничения, которые ставил бот, снимаются автоматически."
    await _answer(call)
    await _open_group(call, chat_id, note)


@router.callback_query(F.data.startswith("sg:grchk:"), IsOwner())
async def cb_group_check(call: CallbackQuery, bot: Bot):
    chat_id = int(call.data.split(":")[2])
    res = await sg.check_group_rights(bot, chat_id)
    if res["ok"]:
        note = "✅ Права в порядке: бот может удалять сообщения и ограничивать участников."
    else:
        need = "\n".join(f"• {x}" for x in res["missing"]) or sg._h(res["error"])
        note = f"⚠️ Боту нужны права администратора:\n{need}"
    await _answer(call)
    await _open_group(call, chat_id, note)


@router.callback_query(F.data.startswith("sg:grc:"), IsOwner())
async def cb_group_channel(call: CallbackQuery, bot: Bot):
    _, _, chat_id, ch_id = call.data.split(":")
    ch = await asyncio.to_thread(sg.get_channel, int(ch_id))
    if not ch:
        await _answer(call)
        return
    snap = await asyncio.to_thread(sg.snapshot, True)
    applies = any(int(c["chat_id"]) == int(ch["chat_id"])
                  for c in sg.channels_for({"channels": [ch], "links": snap["links"]}, int(chat_id)))
    await asyncio.to_thread(sg.set_link, int(chat_id), int(ch["chat_id"]), not applies)
    asyncio.create_task(sg.recheck_restricted(bot, 200))
    await _answer(call, "Канал для группы выключен" if applies else "Канал для группы включён")
    await _open_group(call, int(chat_id))


# ───────────────────────── исключения ─────────────────────────

@router.callback_query(F.data == "sg:ex", IsOwner())
async def cb_exempt(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await asyncio.to_thread(sg.list_exempt)
    lines = ["🙋 <b>Исключения</b>\n",
             "Этим людям подписка не нужна. Владелец и администраторы групп, сам бот и другие боты "
             "не проверяются и без этого списка.\n"]
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data="sg:exadd")
    if not rows:
        lines.append("<i>Список пуст.</i>")
    for r in rows:
        who = f"@{r['username']}" if r.get("username") else (r.get("first_name") or "")
        lines.append(f"• <code>{r['tg_id']}</code> {sg._h(who)} {sg._h(r.get('note') or '')}".rstrip())
        kb.button(text=f"🗑 {r['tg_id']} {who}"[:60], callback_data=f"sg:exdel:{r['tg_id']}")
    kb.button(text="⬅️ Назад", callback_data="sg:menu")
    kb.adjust(1)
    await _show(call, "\n".join(lines), kb)
    await _answer(call)


@router.callback_query(F.data == "sg:exadd", IsOwner())
async def cb_exempt_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(GateStates.exempt)
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="sg:ex")
    await _show(call, "Пришлите Telegram ID (можно несколько через пробел), @username человека, который "
                      "писал боту, или перешлите сюда его сообщение.", kb)
    await _answer(call)


def _resolve_people(text: str) -> tuple:
    ids, unknown = [], []
    for tok in (text or "").replace(",", " ").split():
        if tok.lstrip("-").isdigit():
            ids.append(int(tok))
        elif tok.startswith("@") or tok.isidentifier():
            row = db.fetchone("SELECT tg_id FROM users WHERE LOWER(username)=LOWER(?)", (tok.lstrip("@"),))
            if row:
                ids.append(int(row["tg_id"]))
            else:
                unknown.append(tok)
    return ids, unknown


@router.message(GateStates.exempt, IsOwner())
async def msg_exempt(message: Message, state: FSMContext):
    origin = getattr(message, "forward_origin", None)
    fwd_user = getattr(origin, "sender_user", None) or getattr(message, "forward_from", None)
    if fwd_user is not None:
        ids, unknown = [int(fwd_user.id)], []
    elif origin is not None:
        await message.answer("Человек скрыл свой аккаунт при пересылке — пришлите его ID или @username.")
        return
    else:
        ids, unknown = await asyncio.to_thread(_resolve_people, message.text or "")
    if not ids:
        await message.answer("Не нашёл ни одного человека. Пришлите числовой ID или @username того, кто писал боту.")
        return
    for tg in ids:
        await asyncio.to_thread(sg.add_exempt, tg, "", message.from_user.id)
        sg.forget_user(tg)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🙋 К исключениям", callback_data="sg:ex")
    text = f"✅ В исключения добавлено: {', '.join(str(x) for x in ids)}"
    if unknown:
        text += f"\n⚠️ Не найдены: {', '.join(unknown)}"
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("sg:exdel:"), IsOwner())
async def cb_exempt_del(call: CallbackQuery, state: FSMContext):
    await asyncio.to_thread(sg.remove_exempt, int(call.data.split(":")[2]))
    await _answer(call, "Убрано из исключений")
    await cb_exempt(call, state)


# ───────────────────────── настройки ─────────────────────────

SHORT = {"st": "gate_strict", "ok": "gate_ok_ttl_sec", "nt": "gate_notice_ttl_min", "cd": "gate_notice_cooldown_sec",
         "sd": "gate_success_delete_sec", "rc": "gate_recheck_min", "dl": "gate_debug_log"}


@router.callback_query(F.data == "sg:set", IsOwner())
async def cb_settings(call: CallbackQuery):
    lines = ["⚙️ <b>Настройки обязательной подписки</b>\n"]
    kb = InlineKeyboardBuilder()
    sizes = []
    for short, key in SHORT.items():
        title, unit = sg.SETTING_TITLES[key]
        cur = sg.setting(key)
        labels = sg.BOOL_LABELS.get(key)
        shown = labels.get(cur, cur) if labels else f"{cur} {unit}"
        lines.append(f"• {title}: <b>{shown}</b>")
        for v in sg.SETTING_CHOICES[key]:
            text = labels.get(v, v) if labels else f"{v} {unit}"
            kb.button(text=("• " if v == cur else "") + str(text), callback_data=f"sg:sv:{short}:{v}")
        sizes.append(len(sg.SETTING_CHOICES[key]))
    lines.append("\n<i>Ряды кнопок — в том же порядке, что и настройки. Строгий режим: если бот не может проверить "
                 "канал (нет прав, канал удалён), сообщения участников удаляются, а не пропускаются. Подписку бот "
                 "переспрашивает у Telegram не реже указанного; отписку от канала видит сразу, если он администратор канала.</i>")
    kb.button(text="⬅️ Назад", callback_data="sg:menu")
    kb.adjust(*sizes, 1)
    await _show(call, "\n".join(lines), kb)
    await _answer(call)


@router.callback_query(F.data.startswith("sg:sv:"), IsOwner())
async def cb_setting_value(call: CallbackQuery):
    _, _, short, value = call.data.split(":")
    key = SHORT.get(short)
    if key and int(value) in sg.SETTING_CHOICES[key]:
        await asyncio.to_thread(sg.set_setting, key, int(value))
        sg.invalidate()
    await cb_settings(call)


# ───────────────────────── диагностика и журнал ─────────────────────────

@router.callback_query(F.data == "sg:diag", IsOwner())
async def cb_diag(call: CallbackQuery, bot: Bot):
    await _answer(call, "Проверяю каналы и группы…")
    try:
        text = await sg.diagnose(bot, call.from_user.id)
    except Exception as e:
        log.exception("подписка: диагностика")
        text = f"⚠️ Диагностика не завершилась: {sg._h(type(e).__name__)}: {sg._h(str(e)[:200])}"
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Ещё раз", callback_data="sg:diag")
    kb.button(text="⬅️ Назад", callback_data="sg:menu")
    kb.adjust(2)
    await _show(call, text, kb)


def _log_text(chat_id: int) -> str:
    rows = sg.recent_decisions(chat_id or None, 25)
    head = "🧾 <b>Журнал проверок</b>"
    if chat_id:
        head += f" — {sg._h(sg.get_group(chat_id).get('title') or chat_id)}"
    lines = [head, "Почему каждое сообщение пропущено или удалено. Новые сверху.\n"]
    if not rows:
        lines.append("<i>Записей нет. Если участники пишут в группе с включённой проверкой, а здесь пусто — "
                     "бот не получает их сообщения: проверьте, что он администратор группы, и что "
                     "загружены все файлы новой версии.</i>")
    for r in rows:
        who = f"@{r['username']}" if r.get("username") else f"id {r['tg_id']}"
        icon = "✅" if r["action"].startswith("PASS") else "🗑"
        lines.append(f"{icon} {sg.fmt_local(r['ts'])[-5:]} {sg._h(who)} — {sg._h(r['action'])}")
        det = r.get("details") or ""
        chans = [x.strip("[]") for x in det.split(" [")[1:]]
        for c in chans[:4]:
            c = c.split("]")[0]
            lines.append(f"   <code>{sg._h(c[:150])}</code>")
    if not sg.setting("gate_debug_log"):
        lines.append("\n<i>Журнал выключен в настройках — новые записи не появляются.</i>")
    return "\n".join(lines)[:4000]


@router.callback_query(F.data.startswith("sg:log:"), IsOwner())
async def cb_log(call: CallbackQuery):
    chat_id = int(call.data.split(":")[2])
    text = await asyncio.to_thread(_log_text, chat_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=f"sg:log:{chat_id}")
    kb.button(text="⬅️ Назад", callback_data=f"sg:gr:{chat_id}" if chat_id else "sg:menu")
    kb.adjust(2)
    await _show(call, text, kb)
    await _answer(call)


# ───────────────────────── статистика ─────────────────────────

VIEWS = {"ov": "📈 Общая статистика", "ch": "📢 По каналам", "gr": "👥 По группам", "dy": "📅 По дням"}
PERIOD_BUTTONS = [("td", "📅 За сегодня"), ("yd", "📅 Вчера"), ("7d", "📅 За 7 дней"), ("30d", "📅 За 30 дней"),
                  ("mo", "📅 Этот месяц"), ("pm", "📅 Прошлый месяц"), ("all", "📅 Всё время")]


def _custom(admin_id: int):
    row = db.fetchone("SELECT value FROM settings WHERE key=?", (f"gate_custom:{admin_id}",))
    if not row or not row["value"]:
        return None
    try:
        a, b = row["value"].split("|")
        return date.fromisoformat(a), date.fromisoformat(b)
    except ValueError:
        return None


def _stats_text(view: str, period: str, chat_id: int, admin_id: int) -> str:
    custom = _custom(admin_id) if period == "cu" else None
    if period == "cu" and not custom:
        period = "7d"
    gname = "все"
    if chat_id:
        g = sg.get_group(chat_id)
        gname = g.get("title") or str(chat_id)
    head = f"Группа: {sg._h(gname)}"
    if view == "ov":
        o = sg.overview(period, chat_id or None, custom)
        d = sg.daily(period if period != "all" else "30d", chat_id or None, custom)
        sign = "+" if d["delta"] > 0 else ""
        return "\n".join([
            "📊 <b>Обязательная подписка — общая статистика</b>",
            f"Период: {o['label']}", head, "",
            f"• Проверено пользователей: <b>{_n(o['checked'])}</b>",
            f"• Уже были подписаны: {_n(o['already'])}",
            f"• Не были подписаны: {_n(o['missing'])}",
            f"• Подписались после требования: <b>{_n(o['subscribed'])}</b>",
            f"• Не подписались: {_n(o['not_subscribed'])}",
            f"• Конверсия: <b>{_pct(o['conversion'])}</b>",
            f"• Получили доступ после проверки: {_n(o['access'])}",
            f"• Повторно отписались: {_n(o['relocked'])}",
            f"• Повторно подписались: {_n(o['resubscribed'])}", "",
            f"Подписок сегодня: {_n(d['today'])} · вчера: {_n(d['yesterday'])} ({sign}{d['delta']})",
            f"В среднем в день: {str(d['avg']).replace('.', ',')}",
            f"Новых участников: {_n(o['joined'])} · удалено сообщений: {_n(o['deleted'])}", "",
            "<i>Считаются уникальные люди. Подписка засчитывается, только когда Telegram подтвердил: "
            "до требования человека в канале не было, а после — появился.</i>"])
    if view == "ch":
        rows = sg.by_channels(period, chat_id or None, custom)
        label = sg.period_bounds(period, custom)[2]
        lines = ["📢 <b>Статистика по каналам</b>", f"Период: {label}", head, ""]
        if not rows:
            lines.append("<i>Данных пока нет.</i>")
        for r in rows:
            lines += [f"📢 <b>{sg._h(r['title'])}</b>",
                      f"• Показан пользователям: {_n(r['shown'])}",
                      f"• Подписались после требования: {_n(r['subscribed'])}",
                      f"• Не подписались: {_n(r['not_subscribed'])}",
                      f"• Отписались позже: {_n(r['unsubscribed'])}",
                      f"• Повторно подписались: {_n(r['resubscribed'])}",
                      f"• Конверсия: {_pct(r['conversion'])}", ""]
        return "\n".join(lines)[:3900]
    if view == "gr":
        rows = sg.by_groups(period, custom)
        if chat_id:
            rows = [r for r in rows if int(r["chat_id"]) == int(chat_id)]
        label = sg.period_bounds(period, custom)[2]
        lines = ["👥 <b>Статистика по группам</b>", f"Период: {label}", ""]
        if not rows:
            lines.append("<i>Данных пока нет.</i>")
        for r in rows:
            lines += [f"👥 <b>{sg._h(r['title'])}</b>",
                      f"• Новых участников: {_n(r['joined'])}",
                      f"• Проверено: {_n(r['checked'])}",
                      f"• Потребовалась подписка: {_n(r['required'])}",
                      f"• Подписались: {_n(r['subscribed'])} ({_pct(r['conversion'])})"]
            for title, n in r["channels"][:5]:
                lines.append(f"   📢 {sg._h(title)}: {_n(n)}")
            lines.append("")
        return "\n".join(lines)[:3900]
    d = sg.daily(period, chat_id or None, custom)
    lines = ["📅 <b>Подписки по дням</b>", f"Период: {d['label']}", head, "",
             f"Сегодня: {_n(d['today'])} · вчера: {_n(d['yesterday'])} · в среднем: {str(d['avg']).replace('.', ',')} в день",
             f"Всего за период: {_n(d['total'])}", ""]
    for day, n in d["days"][-31:]:
        lines.append(f"{day.strftime('%d.%m')} — {_n(n)}")
    return "\n".join(lines)[:3900]


def _stats_kb(view: str, period: str, chat_id: int):
    kb = InlineKeyboardBuilder()
    for v, title in VIEWS.items():
        kb.button(text=("• " if v == view else "") + title, callback_data=f"sg:st:{v}:{period}:{chat_id}")
    for p, title in PERIOD_BUTTONS:
        kb.button(text=("• " if p == period else "") + title, callback_data=f"sg:st:{view}:{p}:{chat_id}")
    kb.button(text=("• " if period == "cu" else "") + "🗓 Свой период", callback_data=f"sg:stcu:{view}:{chat_id}")
    if chat_id:
        kb.button(text="👥 Все группы", callback_data=f"sg:st:{view}:{period}:0")
    kb.button(text="📥 Выгрузить отчёт", callback_data=f"sg:xm:{period}:{chat_id}:0")
    kb.button(text="⬅️ Назад", callback_data="sg:menu")
    kb.adjust(2, 2, 2, 2, 2, 2, 1, 1, 1)
    return kb


@router.callback_query(F.data.startswith("sg:st:"), IsOwner())
async def cb_stats(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, view, period, chat_id = call.data.split(":")
    chat_id = int(chat_id)
    text = await asyncio.to_thread(_stats_text, view, period, chat_id, call.from_user.id)
    await _show(call, text, _stats_kb(view, period, chat_id))
    await _answer(call)


@router.callback_query(F.data.startswith("sg:stcu:"), IsOwner())
async def cb_stats_custom(call: CallbackQuery, state: FSMContext):
    _, _, view, chat_id = call.data.split(":")
    await state.set_state(GateStates.period)
    await state.update_data(gate_view=view, gate_chat=int(chat_id))
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data=f"sg:st:{view}:7d:{chat_id}")
    await _show(call, "🗓 Пришлите период в виде <code>01.09.2026 - 15.09.2026</code> (или одну дату).", kb)
    await _answer(call)


@router.message(GateStates.period, IsOwner())
async def msg_stats_custom(message: Message, state: FSMContext):
    rng = sg.parse_custom_period(message.text or "")
    if not rng:
        await message.answer("Не понял даты. Пример: 01.09.2026 - 15.09.2026")
        return
    data = await state.get_data()
    await state.clear()
    await asyncio.to_thread(db.execute, "INSERT INTO settings (key, value) VALUES (?, ?) "
                                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"gate_custom:{message.from_user.id}", f"{rng[0].isoformat()}|{rng[1].isoformat()}"))
    view, chat_id = data.get("gate_view") or "ov", int(data.get("gate_chat") or 0)
    text = await asyncio.to_thread(_stats_text, view, "cu", chat_id, message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=_stats_kb(view, "cu", chat_id).as_markup())


# ── выгрузка ──

def _export_text(period: str, chat_id: int, ch: int, admin_id: int) -> str:
    custom = _custom(admin_id) if period == "cu" else None
    label = sg.period_bounds(period if (period != "cu" or custom) else "7d", custom)[2]
    gname = (sg.get_group(chat_id).get("title") or str(chat_id)) if chat_id else "все"
    cname = sg.channel_title(ch) if ch else "все"
    return ("📥 <b>Выгрузка отчёта</b>\n\n"
            f"Период: {label}\nГруппа: {sg._h(gname)}\nКанал: {sg._h(cname)}\n\n"
            "В отчёте: итог, каналы, группы, подписки по дням и подробная таблица по людям "
            "(кто, из какой группы, на какой канал, был ли подписан до проверки, когда подписался, "
            "получил доступ, отписался и подписался снова).")


@router.callback_query(F.data.startswith("sg:xm:"), IsOwner())
async def cb_export_menu(call: CallbackQuery):
    _, _, period, chat_id, ch = call.data.split(":")
    text = await asyncio.to_thread(_export_text, period, int(chat_id), int(ch), call.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 CSV", callback_data=f"sg:xf:csv:{period}:{chat_id}:{ch}")
    kb.button(text="📊 Excel", callback_data=f"sg:xf:xlsx:{period}:{chat_id}:{ch}")
    kb.button(text="👥 Выбрать группу", callback_data=f"sg:xg:{period}:{chat_id}:{ch}")
    kb.button(text="📢 Выбрать канал", callback_data=f"sg:xc:{period}:{chat_id}:{ch}")
    kb.button(text="⬅️ К статистике", callback_data=f"sg:st:ov:{period}:{chat_id}")
    kb.adjust(2, 1, 1, 1)
    await _show(call, text, kb)
    await _answer(call)


@router.callback_query(F.data.startswith("sg:xg:") | F.data.startswith("sg:xc:"), IsOwner())
async def cb_export_pick(call: CallbackQuery):
    _, what, period, chat_id, ch = call.data.split(":")
    kb = InlineKeyboardBuilder()
    if what == "xg":
        kb.button(text="Все группы", callback_data=f"sg:xm:{period}:0:{ch}")
        for g in await asyncio.to_thread(sg.known_groups):
            kb.button(text=(g.get("title") or str(g["chat_id"]))[:50], callback_data=f"sg:xm:{period}:{g['chat_id']}:{ch}")
        title = "Для какой группы выгрузить?"
    else:
        kb.button(text="Все каналы", callback_data=f"sg:xm:{period}:{chat_id}:0")
        for c in await asyncio.to_thread(sg.list_channels, True):
            kb.button(text=(c["title"] or str(c["chat_id"]))[:50], callback_data=f"sg:xm:{period}:{chat_id}:{c['chat_id']}")
        title = "Для какого канала выгрузить?"
    kb.button(text="⬅️ Назад", callback_data=f"sg:xm:{period}:{chat_id}:{ch}")
    kb.adjust(1)
    await _show(call, title, kb)
    await _answer(call)


@router.callback_query(F.data.startswith("sg:xf:"), IsOwner())
async def cb_export_file(call: CallbackQuery):
    _, _, fmt, period, chat_id, ch = call.data.split(":")
    custom = await asyncio.to_thread(_custom, call.from_user.id) if period == "cu" else None
    if period == "cu" and not custom:
        period = "7d"
    args = (period, int(chat_id) or None, int(ch) or None, custom)
    await _answer(call, "Готовлю отчёт…")
    try:
        if fmt == "csv":
            data = await asyncio.to_thread(sg.export_csv, *args)
            name = f"podpiska_{period}.csv"
        else:
            data = await asyncio.to_thread(sg.export_xlsx, *args)
            name = f"podpiska_{period}.xlsx"
        label = (await asyncio.to_thread(sg.period_bounds, period, custom))[2]
        await call.message.answer_document(BufferedInputFile(data, filename=name),
                                           caption=f"📥 Обязательная подписка — {label}")
    except Exception as e:
        log.warning("подписка: выгрузка: %s", e)
        await call.message.answer("⚠️ Не получилось собрать отчёт. Попробуйте ещё раз.")
