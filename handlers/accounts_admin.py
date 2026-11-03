"""«👤 Аккаунты» в боте (v95): учебные аккаунты учеников создаёт только админ.
Логика — services/study_accounts.py (общая с админкой сайта)."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import utils
from filters import IsOwner
from services import study_accounts as sa

router = Router(name="accounts_admin")
log = logging.getLogger(__name__)
esc = utils.escape_html


class AccStates(StatesGroup):
    ident = State()


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, text, kb=None):
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


def _menu():
    rows = sa.list_accounts(12)
    waiting = sa.waiting_invites()
    lines = ["👤 <b>Аккаунты учеников</b>\n",
             "Учебный аккаунт появляется только здесь (или в админке сайта). Обычные пользователи бота "
             "аккаунтов не получают.\n"]
    kb = [[_btn("➕ Создать аккаунт", "acc:new")]]
    for u in rows:
        name = ("@" + u["username"]) if u.get("username") else (u.get("first_name") or str(u["tg_id"]))
        mark = "✅" if (u.get("study_status") or "active") == "active" else "⛔"
        kb.append([_btn(f"{mark} {name} · {u['study_login']}"[:60], f"acc:card:{u['tg_id']}")])
    if waiting:
        lines.append("⏳ Ждут запуска бота: " + ", ".join("@" + esc(w["username"]) for w in waiting[:10]))
    kb.append([_btn("🔎 Найти аккаунт", "acc:find"), _btn("↩️ В админку", "m:admin")])
    return "\n".join(lines), _kb(kb)


@router.callback_query(F.data == "acc:menu", IsOwner())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *_menu())


# ───────────────────────── мастер создания ─────────────────────────

@router.callback_query(F.data.in_({"acc:new", "acc:find"}), IsOwner())
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(acc_mode=call.data.split(":")[1])
    await state.set_state(AccStates.ident)
    await call.answer()
    await call.message.answer("👤 Пришлите <b>@username</b> или <b>Telegram ID</b> ученика.\n\n/cancel — отмена",
                              parse_mode="HTML")


@router.message(AccStates.ident, IsOwner())
async def msg_ident(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    mode = (await state.get_data()).get("acc_mode") or "new"
    user = await asyncio.to_thread(sa.find_user, text)
    await state.set_state(None)
    if user and mode == "find":
        await _show(message, *_card(user))
        return
    if not user:
        if text.lstrip("-").isdigit() or mode == "find":
            await message.answer("Такого пользователя бот не знает: он ещё не запускал бота. "
                                 "Попросите его нажать /start или пришлите @username — тогда аккаунт "
                                 "создастся сам, когда он запустит бота.")
            await state.set_state(AccStates.ident)
            return
        await state.update_data(acc={"invite": text, "subjects": [], "premium": "none", "wm": "on"})
        await message.answer(f"⏳ @{esc(text.lstrip('@'))} ещё не запускал бота. Бот не пишет первым, поэтому "
                             "сохраню приглашение: аккаунт создастся и сообщение придёт, когда он сам нажмёт /start.",
                             parse_mode="HTML")
    else:
        if user.get("study_account"):
            await message.answer("У этого ученика уже есть учебный аккаунт — второй не создаётся. Открываю его:")
            await _show(message, *_card(user))
            return
        await state.update_data(acc={"tg": user["tg_id"], "subjects": [], "premium": "none", "wm": "on"})
        name = ("@" + user["username"]) if user.get("username") else (user.get("first_name") or "")
        await message.answer(f"✅ Нашёл: <b>{esc(name)}</b> · ID <code>{user['tg_id']}</code>", parse_mode="HTML")
    await _subjects_step(message, state)


def _subjects_kb(chosen: list, done: str, back: str):
    rows = [[_btn(("☑ " if s["id"] in chosen else "☐ ") + s["title"][:40], f"acc:s:{s['id']}")] for s in sa.subjects()[:40]]
    return _kb(rows + [[_btn("➡️ Далее", done)], [_btn("❌ Отмена", back)]])


async def _subjects_step(target, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    await _show(target, "📚 <b>Шаг 2. Предметы для «Моих классов»</b>\n\nОтметьте один или несколько.",
                _subjects_kb(d.get("subjects") or [], "acc:s:next", "acc:menu"))


@router.callback_query(F.data.startswith("acc:s:"), IsOwner())
async def cb_subject(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    d = data.get("acc") or {}
    arg = call.data.split(":")[2]
    if arg == "next":
        await call.answer()
        await _show(call, "👑 <b>Шаг 3. Premium</b>\n\nPremium действует в боте, мини-приложении и на сайте. "
                          "Платные предметы он сам не открывает — только выбранные выше.",
                    _kb([[_btn(v, f"acc:p:{k}")] for k, v in sa.PREMIUM_CHOICES.items()] + [[_btn("❌ Отмена", "acc:menu")]]))
        return
    sid = int(arg)
    chosen = d.get("subjects") or []
    chosen = [x for x in chosen if x != sid] if sid in chosen else chosen + [sid]
    d["subjects"] = chosen
    await state.update_data(acc=d)
    await call.answer()
    if d.get("edit_tg"):
        await _show(call, "📚 <b>Предметы ученика</b>", _subjects_kb(chosen, "acc:es:save", f"acc:card:{d['edit_tg']}"))
    else:
        await _subjects_step(call, state)


@router.callback_query(F.data.startswith("acc:p:"), IsOwner())
async def cb_premium(call: CallbackQuery, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    d["premium"] = call.data.split(":")[2]
    await state.update_data(acc=d)
    await call.answer()
    await _show(call, "💧 <b>Шаг 4. Водяные знаки</b>",
                _kb([[_btn(v, f"acc:w:{k}")] for k, v in sa.WM_MODES.items()] + [[_btn("❌ Отмена", "acc:menu")]]))


@router.callback_query(F.data.startswith("acc:w:"), IsOwner())
async def cb_wm(call: CallbackQuery, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    d["wm"] = call.data.split(":")[2]
    await state.update_data(acc=d)
    await call.answer()
    titles = [s["title"] for s in sa.subjects() if s["id"] in (d.get("subjects") or [])]
    who = f"@{esc(d['invite'].lstrip('@'))} (ещё не запускал бота)" if d.get("invite") else f"ID {d.get('tg')}"
    await _show(call, "✅ <b>Шаг 5. Проверьте</b>\n\n"
                      f"👤 Ученик: {who}\n📚 Предметы: {esc(', '.join(titles)) or 'не выбраны'}\n"
                      f"👑 Premium: {sa.PREMIUM_CHOICES.get(d['premium'], '—')}\n💧 Водяные знаки: {sa.WM_MODES[d['wm']]}\n\n"
                      "После создания ученику придёт личное сообщение с логином и кнопкой для пароля.",
                _kb([[_btn("✅ Создать аккаунт", "acc:ok")], [_btn("❌ Отмена", "acc:menu")]]))


@router.callback_query(F.data == "acc:ok", IsOwner())
async def cb_create(call: CallbackQuery, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    await state.clear()
    if d.get("invite"):
        ok = await asyncio.to_thread(sa.invite, d["invite"], d.get("subjects") or [], d.get("premium"), d.get("wm"),
                                     call.from_user.id)
        await call.answer()
        await _show(call, ("⏳ Приглашение сохранено. Когда ученик нажмёт /start, аккаунт создастся и ему придёт "
                           "сообщение с логином.") if ok else "⚠️ Неверный username.", _kb([[_btn("↩️ К аккаунтам", "acc:menu")]]))
        return
    user = await asyncio.to_thread(sa.find_user, str(d.get("tg") or ""))
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    info = await asyncio.to_thread(sa.create, user, d.get("subjects") or [], d.get("premium"), d.get("wm"),
                                   call.from_user.id, "bot")
    user = await asyncio.to_thread(sa.find_user, str(user["tg_id"]))
    sent = await sa.notify(call.bot, user, info)
    await call.answer("Аккаунт создан")
    await call.message.answer(
        "🎉 <b>Учебный аккаунт создан</b>\n\n"
        f"👤 Логин: <code>{esc(info['login'])}</code>\n📚 {esc(', '.join(info['subjects'])) or 'без предметов'}\n"
        f"👑 Premium: {esc(info['premium'] or 'нет')}\n\n"
        + ("✉️ Ученику отправлено сообщение с кнопкой для пароля." if sent else
           "⚠️ Сообщение ученику не доставлено (он закрыл бота?). Ссылку для пароля можно выпустить в карточке."),
        parse_mode="HTML")
    await _show(call.message, *_card(user))


# ───────────────────────── карточка аккаунта ─────────────────────────

def _card(user: dict):
    c = sa.card(user)
    name = ("@" + user["username"]) if user.get("username") else (user.get("first_name") or "")
    prem = c["premium"]
    lines = [f"👤 <b>{esc(name)}</b> · ID <code>{user['tg_id']}</code>"]
    if user.get("study_account"):
        lines += [f"Логин: <code>{esc(user.get('study_login') or '')}</code> · "
                  f"{'✅ активен' if (user.get('study_status') or 'active') == 'active' else '⛔ отключён'} · "
                  f"пароль {'задан' if c['has_password'] else 'ещё не задан'}"]
    else:
        lines.append("Учебного аккаунта нет — обычный пользователь бота.")
    lines += ["", "📚 Мои классы: " + (", ".join(esc(x["title"]) for x in c["classes"]) or "нет"),
              "👑 Premium: " + (("до " + prem["expires_at"][:10]) if prem and prem.get("expires_at") else ("бессрочно" if prem else "нет")),
              f"💧 Водяные знаки: {c['wm']}"]
    from services import devices as _dev
    sess = _dev.sessions(user["tg_id"])
    lim = _dev.user_limit(user["tg_id"])
    lines.append(f"📱 Устройств: {len(sess)} (в лимите {sum(1 for x in sess if x['counted'])} из {lim or '∞'})"
                 + (f"\n⚠️ Подозрительный: {esc(user.get('suspicious_note') or '')}" if user.get("suspicious_at") else ""))
    from services import activity as _act
    a = _act.summary(user["tg_id"])
    lines.append(f"🕓 Последний вход: {a['last_login']['ts'] + ' · ' + esc(a['last_login']['device'] or '') if a['last_login'] else 'нет данных'}"
                 f"\n🕓 Последнее действие: {a['last_action'] or 'нет данных'}"
                 f"\n✅ Уроков {a['lessons']} · тестов {a['tests']} · заданий {a['tasks']} · ДЗ {a['homework']}")
    for d in sess[:5]:
        lines.append(f"   • {esc(d['device_label'] or '')} · {esc(d['last_ip'] or d['ip'] or '')} · {(d['last_seen_at'] or '')[:16].replace('T', ' ')}")
    t = user["tg_id"]
    kb = []
    if not user.get("study_account"):
        kb.append([_btn("➕ Создать учебный аккаунт", f"acc:mk:{t}")])
    else:
        kb += [[_btn("📚 Предметы", f"acc:es:{t}"), _btn("👑 Premium", f"acc:ep:{t}")],
               [_btn("💧 Водяные знаки", f"acc:ew:{t}"), _btn("🔑 Ссылка для пароля", f"acc:link:{t}")],
               [_btn("⛔ Отключить" if (user.get("study_status") or "active") == "active" else "✅ Включить", f"acc:st:{t}")]]
    kb.append([_btn("↩️ К аккаунтам", "acc:menu")])
    return "\n".join(lines), _kb(kb)


async def _user_or_fail(call: CallbackQuery, tg: str):
    user = await asyncio.to_thread(sa.find_user, tg)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
    return user


@router.callback_query(F.data.startswith("acc:card:"), IsOwner())
async def cb_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _user_or_fail(call, call.data.split(":")[2])
    if user:
        await call.answer()
        await _show(call, *_card(user))


@router.callback_query(F.data.startswith("acc:mk:"), IsOwner())
async def cb_make(call: CallbackQuery, state: FSMContext):
    await state.update_data(acc={"tg": int(call.data.split(":")[2]), "subjects": [], "premium": "none", "wm": "on"})
    await call.answer()
    await _subjects_step(call, state)


@router.callback_query(F.data.startswith("acc:es:"), IsOwner())
async def cb_edit_subjects(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    if arg == "save":
        d = (await state.get_data()).get("acc") or {}
        user = await _user_or_fail(call, str(d.get("edit_tg")))
        if not user:
            return
        await asyncio.to_thread(sa.set_subjects, user, d.get("subjects") or [], call.from_user.id, "bot")
        await state.clear()
        await call.answer("Сохранено")
        await _show(call, *_card(user))
        return
    user = await _user_or_fail(call, arg)
    if not user:
        return
    chosen = [c["id"] for c in sa.my_classes(user["tg_id"])]
    await state.update_data(acc={"edit_tg": user["tg_id"], "subjects": chosen})
    await call.answer()
    await _show(call, "📚 <b>Предметы ученика</b>\n\nСнятая галочка закрывает предмет.",
                _subjects_kb(chosen, "acc:es:save", f"acc:card:{user['tg_id']}"))


@router.callback_query(F.data.startswith("acc:ep:"), IsOwner())
async def cb_edit_premium(call: CallbackQuery):
    parts = call.data.split(":")
    user = await _user_or_fail(call, parts[2])
    if not user:
        return
    if len(parts) == 4:
        choice = parts[3]
        if choice == "off":
            from services import trash
            await asyncio.to_thread(trash.trash_premium, user["id"], call.from_user.id, "bot")
        else:
            await asyncio.to_thread(sa.apply_premium, user, choice, call.from_user.id)
            sa._audit(call.from_user.id, "bot", "premium", user, sa.PREMIUM_CHOICES.get(choice, choice))
        await call.answer("Сохранено")
        await _show(call, *_card(user))
        return
    await call.answer()
    rows = [[_btn(v, f"acc:ep:{user['tg_id']}:{k}")] for k, v in sa.PREMIUM_CHOICES.items() if k != "none"]
    await _show(call, "👑 <b>Premium</b>\n\nНовый срок считается с сегодняшнего дня.",
                _kb(rows + [[_btn("⛔ Отключить Premium", f"acc:ep:{user['tg_id']}:off")],
                            [_btn("↩️ Назад", f"acc:card:{user['tg_id']}")]]))


@router.callback_query(F.data.startswith("acc:ew:"), IsOwner())
async def cb_edit_wm(call: CallbackQuery):
    parts = call.data.split(":")
    user = await _user_or_fail(call, parts[2])
    if not user:
        return
    if len(parts) == 4:
        await asyncio.to_thread(sa.set_watermark, user, parts[3], call.from_user.id, "bot")
        await call.answer("Сохранено")
        await _show(call, *_card(await asyncio.to_thread(sa.find_user, parts[2])))
        return
    await call.answer()
    await _show(call, "💧 <b>Водяные знаки этого ученика</b>\n\nНа права доступа не влияет.",
                _kb([[_btn(v, f"acc:ew:{user['tg_id']}:{k}")] for k, v in sa.WM_MODES.items()]
                    + [[_btn("↩️ Назад", f"acc:card:{user['tg_id']}")]]))


@router.callback_query(F.data.startswith("acc:link:"), IsOwner())
async def cb_link(call: CallbackQuery):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    token = await asyncio.to_thread(sa.issue_setup_link, user["id"], "reset")
    info = {"login": user.get("study_login") or "", "token": token,
            "subjects": [c["title"] for c in sa.my_classes(user["tg_id"])]}
    sent = await sa.notify(call.bot, user, info)
    sa._audit(call.from_user.id, "bot", "settings", user, "новая ссылка для пароля")
    await call.answer("✉️ Ссылка отправлена ученику" if sent else "⚠️ Ученику не доставлено", show_alert=True)


@router.callback_query(F.data.startswith("acc:st:"), IsOwner())
async def cb_status(call: CallbackQuery):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    active = (user.get("study_status") or "active") != "active"
    await asyncio.to_thread(sa.set_status, user, active, call.from_user.id, "bot")
    await call.answer("Включён" if active else "Отключён: вход на сайт закрыт")
    await _show(call, *_card(await asyncio.to_thread(sa.find_user, str(user["tg_id"]))))
