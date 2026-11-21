"""«👤 Аккаунты» в боте: учебные аккаунты учеников создаёт только админ.
Логика — services/study_accounts.py (общая с админкой сайта).

v101: вместо списка из 12 кнопок — файл Excel со всеми аккаунтами (логины и
пароли, выданные админом), кнопка «⚙️ Изменить настройки аккаунта» (поиск по
ID, @username или логину), удаление аккаунта в корзину, срок доступа к
предметам, лимит устройств, пароль от админа. Сроки, предметы, лимит и пароль
можно написать вручную, а не только выбрать кнопкой."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import utils
from filters import IsOwner
from services import study_accounts as sa

router = Router(name="accounts_admin")
log = logging.getLogger(__name__)
esc = utils.escape_html


class AccStates(StatesGroup):
    ident = State()
    text = State()          # ручной ввод: что именно ждём — в data["await"]


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


def _name(u: dict) -> str:
    return ("@" + u["username"]) if u.get("username") else (u.get("first_name") or str(u.get("tg_id") or ""))


TERM_HELP = ("✍️ Или напишите срок вручную: <code>45</code>, <code>45 дней</code>, <code>3 месяца</code>, "
             "<code>1 год</code>, <code>до 01.09.2027</code>, <code>бессрочно</code>")


# ───────────────────────── меню ─────────────────────────

def _menu():
    n = sa.counts()
    waiting = sa.waiting_invites()
    lines = ["👤 <b>Аккаунты учеников</b>\n",
             f"Всего: <b>{n['all']}</b> · активных {n['active']} · отключённых {n['disabled']}"]
    if waiting:
        lines.append(f"⏳ Ждут запуска бота: {len(waiting)} (" + ", ".join("@" + esc(w["username"]) for w in waiting[:10])
                     + ("…" if len(waiting) > 10 else "") + ")")
    lines.append("\n📊 Полный список с логинами и паролями — в файле Excel.\n"
                 "⚙️ Чтобы поменять предметы, Premium, пароль, отключить или удалить аккаунт — "
                 "«Изменить настройки аккаунта».")
    kb = [[_btn("➕ Создать аккаунт", "acc:new")],
          [_btn("⚙️ Изменить настройки аккаунта", "acc:edit")],
          [_btn("📊 Excel со всеми аккаунтами", "acc:xlsx")],
          [_btn("↩️ В админку", "m:admin")]]
    return "\n".join(lines), _kb(kb)


async def _send_xlsx(call: CallbackQuery) -> bool:
    n = await asyncio.to_thread(sa.counts)
    if not n["all"] and not n["waiting"]:
        return False
    data = await asyncio.to_thread(sa.export_xlsx)
    try:
        await call.bot.send_document(
            call.from_user.id, BufferedInputFile(data, filename=sa.export_name()),
            caption=(f"📊 Учебные аккаунты: {n['all']}" + (f", ждут запуска бота: {n['waiting']}" if n["waiting"] else "")
                     + "\n\nВ колонке «Пароль» — пароль, который выдал админ. Если ученик сменил пароль сам, "
                       "его не видит никто: выдайте новый в «Изменить настройки аккаунта».\n"
                       "🔒 В файле пароли: не пересылайте его в общие чаты."))
    except Exception as e:
        log.warning("Excel аккаунтов: %s", e)
        return False
    sa._audit(call.from_user.id, "bot", "export", {"tg_id": 0, "username": "аккаунты"}, "Excel со списком аккаунтов")
    return True


@router.callback_query(F.data == "acc:menu", IsOwner())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    """Вход из админ-панели: меню и сразу файл Excel."""
    await state.clear()
    await call.answer()
    await _show(call, *_menu())
    await _send_xlsx(call)


@router.callback_query(F.data == "acc:home", IsOwner())
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *_menu())


@router.callback_query(F.data == "acc:xlsx", IsOwner())
async def cb_xlsx(call: CallbackQuery):
    ok = await _send_xlsx(call)
    await call.answer("📊 Файл отправлен" if ok else "Аккаунтов пока нет", show_alert=not ok)


# ───────────────────────── поиск: создать или изменить ─────────────────────────

@router.callback_query(F.data.in_({"acc:new", "acc:find", "acc:edit"}), IsOwner())
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    mode = "new" if call.data == "acc:new" else "find"
    await state.update_data(acc_mode=mode)
    await state.set_state(AccStates.ident)
    await call.answer()
    if mode == "new":
        text = "👤 Пришлите <b>@username</b> или <b>Telegram ID</b> ученика.\n\n/cancel — отмена"
    else:
        text = ("⚙️ <b>Изменить настройки аккаунта</b>\n\nНапишите <b>Telegram ID</b>, <b>@username</b> "
                "или <b>логин</b> ученика.\n\n/cancel — отмена")
    await call.message.answer(text, parse_mode="HTML")


@router.message(AccStates.ident, IsOwner())
async def msg_ident(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.startswith("/cancel") or not text:
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_kb([[_btn("↩️ К аккаунтам", "acc:home")]]))
        return
    mode = (await state.get_data()).get("acc_mode") or "new"
    user = await asyncio.to_thread(sa.find_user, text, mode == "find")
    await state.set_state(None)
    if user and mode == "find":
        await _show(message, *_card(user))
        return
    if not user:
        if text.lstrip("-").isdigit() or mode == "find":
            await message.answer("Такого ученика не нашёл: нет пользователя бота с таким ID, @username или логином. "
                                 "Проверьте и пришлите ещё раз (или /cancel).\n\n"
                                 "Если человек ещё не запускал бота, создайте аккаунт по его @username — "
                                 "аккаунт появится, когда он сам нажмёт /start.")
            await state.set_state(AccStates.ident)
            return
        await state.update_data(acc=_new_data(invite=text))
        await message.answer(f"⏳ @{esc(text.lstrip('@'))} ещё не запускал бота. Бот не пишет первым, поэтому "
                             "сохраню приглашение: аккаунт создастся и сообщение придёт, когда он сам нажмёт /start.",
                             parse_mode="HTML")
    else:
        if user.get("study_account"):
            await message.answer("У этого ученика уже есть учебный аккаунт — второй не создаётся. Открываю его:")
            await _show(message, *_card(user))
            return
        await state.update_data(acc=_new_data(tg=user["tg_id"]))
        await message.answer(f"✅ Нашёл: <b>{esc(_name(user))}</b> · ID <code>{user['tg_id']}</code>", parse_mode="HTML")
    await _subjects_step(message, state)


def _new_data(**kw) -> dict:
    d = {"subjects": [], "term": "0", "premium": "none", "wm": "on", "dev": "", "pw": "auto", "password": ""}
    d.update(kw)
    return d


# ───────────────────────── мастер создания ─────────────────────────

def _subjects_list() -> str:
    subs = sa.subjects()[:40]
    return "\n".join(f"{i}. {esc(s['title'])}" for i, s in enumerate(subs, start=1))


def _subjects_kb(chosen: list, done: str, back: str, done_text: str = "➡️ Далее"):
    rows = [[_btn(("☑ " if s["id"] in chosen else "☐ ") + s["title"][:40], f"acc:s:{s['id']}")] for s in sa.subjects()[:40]]
    return _kb(rows + [[_btn(done_text, done)], [_btn("❌ Отмена" if back == "acc:home" else "↩️ Назад", back)]])


async def _await(state: FSMContext, what: str) -> None:
    await state.update_data(**{"await": what})
    await state.set_state(AccStates.text)


async def _subjects_step(target, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    await _await(state, "subjects")
    await _show(target, "📚 <b>Шаг 2. Предметы для «Моих классов»</b>\n\nОтметьте один или несколько.\n\n"
                        "✍️ Или напишите вручную номера или названия через запятую (можно начало слова), "
                        "или <code>все</code>:\n" + _subjects_list(),
                _subjects_kb(d.get("subjects") or [], "acc:s:next", "acc:home"))


async def _premium_step(target, state: FSMContext):
    await _await(state, "premium")
    await _show(target, "👑 <b>Шаг 3. Premium</b>\n\nPremium действует в боте, мини-приложении и на сайте. "
                        "Платные предметы он сам не открывает — только выбранные выше.\n\n" + TERM_HELP
                        + ", <code>нет</code>",
                _kb([[_btn(v, f"acc:p:{k}")] for k, v in sa.PREMIUM_CHOICES.items()] + [[_btn("❌ Отмена", "acc:home")]]))


async def _wm_step(target, state: FSMContext):
    await state.set_state(None)
    await _show(target, "💧 <b>Шаг 4. Водяные знаки</b>",
                _kb([[_btn(v, f"acc:w:{k}")] for k, v in sa.WM_MODES.items()] + [[_btn("❌ Отмена", "acc:home")]]))


def _pw_text(d: dict) -> str:
    if d.get("pw") == "manual" and d.get("password"):
        return f"вручную: <code>{esc(d['password'])}</code>"
    if d.get("pw") == "link":
        return "ученик задаст сам по ссылке (в Excel не будет)"
    return "создам автоматически (будет в Excel и в сообщении ученику)"


async def _confirm(target, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    d["ready"] = True
    await state.update_data(acc=d)
    await state.set_state(None)
    titles = [s["title"] for s in sa.subjects() if s["id"] in (d.get("subjects") or [])]
    who = f"@{esc(d['invite'].lstrip('@'))} (ещё не запускал бота)" if d.get("invite") else f"ID {d.get('tg')}"
    prem = sa.PREMIUM_CHOICES.get(d.get("premium")) or sa.term_label(d.get("premium"))
    await _show(target, "✅ <b>Шаг 5. Проверьте</b>\n\n"
                        f"👤 Ученик: {who}\n📚 Предметы: {esc(', '.join(titles)) or 'не выбраны'}\n"
                        f"⏳ Срок предметов: {sa.term_label(d.get('term') or '0')}\n"
                        f"👑 Premium: {esc(prem)}\n💧 Водяные знаки: {sa.WM_MODES.get(d.get('wm') or 'on')}\n"
                        f"📱 Устройств: {sa.device_label(d.get('dev') or '')}\n🔑 Пароль: {_pw_text(d)}\n\n"
                        "Любой пункт можно поменять кнопками ниже. После создания ученику придёт личное "
                        "сообщение с логином и паролем.",
                _kb([[_btn("✅ Создать аккаунт", "acc:ok")],
                     [_btn("📚 Предметы", "acc:s:edit"), _btn("⏳ Срок", "acc:t:ask")],
                     [_btn("👑 Premium", "acc:p:ask"), _btn("💧 Знаки", "acc:w:ask")],
                     [_btn("📱 Устройства", "acc:d:ask"), _btn("🔑 Пароль", "acc:wp:ask")],
                     [_btn("❌ Отмена", "acc:home")]]))


async def _after_choice(target, state: FSMContext, nxt):
    """В мастере — следующий шаг; если уже были на проверке — обратно к ней."""
    d = (await state.get_data()).get("acc") or {}
    if d.get("ready"):
        await _confirm(target, state)
    else:
        await nxt(target, state)


@router.callback_query(F.data.startswith("acc:s:"), IsOwner())
async def cb_subject(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    d = data.get("acc") or {}
    arg = call.data.split(":")[2]
    if arg == "next":
        await call.answer()
        await _after_choice(call, state, _premium_step)
        return
    if arg == "edit":
        await call.answer()
        await _subjects_step(call, state)
        return
    sid = int(arg)
    chosen = d.get("subjects") or []
    chosen = [x for x in chosen if x != sid] if sid in chosen else chosen + [sid]
    d["subjects"] = chosen
    await state.update_data(acc=d)
    await call.answer()
    if d.get("edit_tg"):
        await _edit_subjects_screen(call, state, d["edit_tg"], chosen)
    else:
        await _subjects_step(call, state)


@router.callback_query(F.data.startswith("acc:p:"), IsOwner())
async def cb_premium(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    await call.answer()
    if arg == "ask":
        await _premium_step(call, state)
        return
    d = (await state.get_data()).get("acc") or {}
    d["premium"] = arg
    await state.update_data(acc=d)
    await _after_choice(call, state, _wm_step)


@router.callback_query(F.data.startswith("acc:w:"), IsOwner())
async def cb_wm(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    await call.answer()
    if arg == "ask":
        await _wm_step(call, state)
        return
    d = (await state.get_data()).get("acc") or {}
    d["wm"] = arg if arg in sa.WM_MODES else "on"
    await state.update_data(acc=d)
    await _confirm(call, state)


@router.callback_query(F.data.startswith("acc:t:"), IsOwner())
async def cb_term(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    await call.answer()
    if arg == "ask":
        await _await(state, "term")
        await _show(call, "⏳ <b>Срок доступа к предметам</b>\n\nСколько будут открыты выбранные предметы.\n\n" + TERM_HELP,
                    _kb([[_btn(v, f"acc:t:{k}")] for k, v in sa.TERM_CHOICES.items()] + [[_btn("↩️ Назад", "acc:back")]]))
        return
    d = (await state.get_data()).get("acc") or {}
    d["term"] = arg if arg in sa.TERM_CHOICES else "0"
    await state.update_data(acc=d)
    await _confirm(call, state)


@router.callback_query(F.data.startswith("acc:d:"), IsOwner())
async def cb_devices(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    await call.answer()
    if arg == "ask":
        await _await(state, "devices")
        await _show(call, "📱 <b>Сколько устройств (браузеров) можно одновременно</b>\n\n"
                          "✍️ Или напишите число от 1 до 50, <code>без лимита</code> или <code>как у всех</code>.",
                    _kb([[_btn(v, f"acc:d:{k or 'def'}")] for k, v in sa.DEVICE_CHOICES.items()]
                        + [[_btn("↩️ Назад", "acc:back")]]))
        return
    d = (await state.get_data()).get("acc") or {}
    d["dev"] = "" if arg == "def" else (arg if arg in sa.DEVICE_CHOICES else "")
    await state.update_data(acc=d)
    await _confirm(call, state)


@router.callback_query(F.data.startswith("acc:wp:"), IsOwner())
async def cb_wizard_pw(call: CallbackQuery, state: FSMContext):
    arg = call.data.split(":")[2]
    await call.answer()
    d = (await state.get_data()).get("acc") or {}
    if arg == "ask":
        await state.set_state(None)
        await _show(call, "🔑 <b>Пароль ученика</b>\n\n"
                          "• <b>Автоматически</b> — бот придумает пароль, он будет в Excel и в сообщении ученику.\n"
                          "• <b>Вручную</b> — напишете пароль сами.\n"
                          "• <b>По ссылке</b> — ученик задаст пароль сам; такой пароль не видит никто, в Excel его не будет.",
                    _kb([[_btn("🎲 " + sa.PW_MODES["auto"], "acc:wp:auto")],
                         [_btn("✍️ " + sa.PW_MODES["manual"], "acc:wp:manual")],
                         [_btn("🔗 " + sa.PW_MODES["link"], "acc:wp:link")],
                         [_btn("↩️ Назад", "acc:back")]]))
        return
    if arg == "manual":
        await _await(state, "password")
        await _show(call, f"✍️ Напишите пароль для ученика: не короче {sa.acc.MIN_PASSWORD} символов, без пробелов.",
                    _kb([[_btn("↩️ Назад", "acc:back")]]))
        return
    d["pw"] = arg if arg in sa.PW_MODES else "auto"
    d["password"] = ""
    await state.update_data(acc=d)
    await _confirm(call, state)


@router.callback_query(F.data == "acc:back", IsOwner())
async def cb_back(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _confirm(call, state)


@router.callback_query(F.data == "acc:ok", IsOwner())
async def cb_create(call: CallbackQuery, state: FSMContext):
    d = (await state.get_data()).get("acc") or {}
    await state.clear()
    if not d or not (d.get("invite") or d.get("tg")):
        await call.answer("Мастер устарел — начните заново", show_alert=True)
        await _show(call, *_menu())
        return
    # «Как у всех» в мастере — не трогать личный лимит, если он уже был
    opts = dict(term=None if (d.get("term") or "0") == "0" else d.get("term"), devices=d.get("dev") or None,
                pw_mode=d.get("pw") or "auto", password=d.get("password") or "")
    if d.get("invite"):
        ok = await asyncio.to_thread(sa.invite, d["invite"], d.get("subjects") or [], d.get("premium"), d.get("wm"),
                                     call.from_user.id, **opts)
        await call.answer()
        await _show(call, ("⏳ Приглашение сохранено. Когда ученик нажмёт /start, аккаунт создастся и ему придёт "
                           "сообщение с логином и паролем. Пароль уже есть в Excel.") if ok else "⚠️ Неверный username.",
                    _kb([[_btn("↩️ К аккаунтам", "acc:home")]]))
        return
    user = await asyncio.to_thread(sa.find_user, str(d.get("tg") or ""))
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    info = await asyncio.to_thread(sa.create, user, d.get("subjects") or [], d.get("premium"), d.get("wm"),
                                   call.from_user.id, "bot", **opts)
    user = await asyncio.to_thread(sa.find_user, str(user["tg_id"]))
    sent = await sa.notify(call.bot, user, info)
    await call.answer("Аккаунт создан")
    if info.get("password"):
        pw_line = f"🔑 Пароль: <code>{esc(info['password'])}</code>\n"
        how = "✉️ Ученику отправлено сообщение с логином и паролем."
    else:
        pw_line = ""
        how = "✉️ Ученику отправлено сообщение с кнопкой для пароля."
    await call.message.answer(
        "🎉 <b>Учебный аккаунт создан</b>\n\n"
        f"👤 Логин: <code>{esc(info['login'])}</code>\n{pw_line}📚 {esc(', '.join(info['subjects'])) or 'без предметов'}"
        f" · {esc(info.get('term') or 'бессрочно')}\n"
        f"👑 Premium: {esc(info['premium'] or 'нет')}\n\n"
        + (how if sent else "⚠️ Сообщение ученику не доставлено (он закрыл бота?). Логин и пароль есть в Excel, "
                            "их можно передать самому."),
        parse_mode="HTML")
    await _show(call.message, *_card(user))


# ───────────────────────── ручной ввод ─────────────────────────

@router.message(AccStates.text, IsOwner())
async def msg_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    what = data.get("await") or ""
    d = data.get("acc") or {}
    if text.startswith("/cancel") or not text:
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=_kb([[_btn("↩️ К аккаунтам", "acc:home")]]))
        return
    edit_tg = d.get("edit_tg")
    user = await asyncio.to_thread(sa.find_user, str(edit_tg)) if edit_tg else None
    actor = message.from_user.id

    if what in ("subjects", "e_subjects"):
        ids, bad = await asyncio.to_thread(sa.parse_subjects, text)
        if bad and not ids:
            await message.answer("Не нашёл такие предметы: " + esc(", ".join(bad)) + ". Напишите номера из списка "
                                 "или названия через запятую.", parse_mode="HTML")
            return
        d["subjects"] = ids
        await state.update_data(acc=d)
        if bad:
            await message.answer("⚠️ Не нашёл: " + esc(", ".join(bad)) + ". Остальные отмечены.", parse_mode="HTML")
        if what == "subjects":
            await _subjects_step(message, state)
        else:
            await _edit_subjects_screen(message, state, edit_tg, ids)
        return

    if what in ("term", "e_term", "premium", "e_premium"):
        spec = sa.parse_term(text, allow_none=what in ("premium", "e_premium"))
        if not spec:
            await message.answer("Не понял срок. Примеры: <code>45</code>, <code>3 месяца</code>, <code>1 год</code>, "
                                 "<code>до 01.09.2027</code>, <code>бессрочно</code>"
                                 + (", <code>нет</code>" if "premium" in what else "") + ". Дата должна быть в будущем.",
                                 parse_mode="HTML")
            return
        if what == "term":
            d["term"] = spec
            await state.update_data(acc=d)
            await _confirm(message, state)
        elif what == "premium":
            d["premium"] = spec
            await state.update_data(acc=d)
            await _after_choice(message, state, _wm_step)
        elif user and what == "e_term":
            n = await asyncio.to_thread(sa.set_term, user, spec, actor, "bot")
            await state.clear()
            await message.answer(f"✅ Срок доступа к предметам ({n}): {sa.term_label(spec)}.")
            await _show(message, *_card(await asyncio.to_thread(sa.find_user, str(edit_tg))))
        elif user:
            await _set_premium(user, spec, actor)
            await state.clear()
            await message.answer("✅ Premium: " + (sa.term_label(spec) if spec != "none" else "отключён (в корзину)") + ".")
            await _show(message, *_card(await asyncio.to_thread(sa.find_user, str(edit_tg))))
        return

    if what in ("devices", "e_devices"):
        spec = sa.parse_device_limit(text)
        if spec is None:
            await message.answer("Напишите число от 1 до 50, <code>без лимита</code> или <code>как у всех</code>.",
                                 parse_mode="HTML")
            return
        if what == "devices":
            d["dev"] = spec
            await state.update_data(acc=d)
            await _confirm(message, state)
        elif user:
            await asyncio.to_thread(sa.set_device_limit, user, spec, actor, "bot")
            await state.clear()
            await message.answer("✅ Лимит устройств: " + sa.device_label(spec) + ".")
            await _show(message, *_card(await asyncio.to_thread(sa.find_user, str(edit_tg))))
        return

    if what in ("password", "e_password"):
        problem = sa.password_problem(text)
        try:
            await message.delete()                    # пароль не остаётся в переписке
        except Exception:
            pass
        if problem:
            await message.answer(problem + " Напишите другой.")
            return
        if what == "password":
            d["pw"], d["password"] = "manual", text
            await state.update_data(acc=d)
            await _confirm(message, state)
        elif user:
            await state.clear()
            await asyncio.to_thread(sa.set_admin_password, user, text, actor, "bot")
            await _show(message, *_pw_done(await asyncio.to_thread(sa.find_user, str(edit_tg))))
        return

    await state.clear()
    await message.answer("Ввод устарел — откройте раздел заново.", reply_markup=_kb([[_btn("↩️ К аккаунтам", "acc:home")]]))


# ───────────────────────── карточка аккаунта ─────────────────────────

def _card(user: dict):
    c = sa.card(user)
    prem = c["premium"]
    active = (user.get("study_status") or "active") == "active"
    lines = [f"👤 <b>{esc(_name(user))}</b> · ID <code>{user['tg_id']}</code>"]
    if user.get("study_account"):
        lines.append(f"Логин: <code>{esc(user.get('study_login') or '')}</code> · {'✅ активен' if active else '⛔ отключён'}")
        if c["admin_pw"]:
            lines.append(f"🔑 Пароль: <tg-spoiler><code>{esc(c['admin_pw'])}</code></tg-spoiler> (выдал админ)")
        else:
            lines.append("🔑 Пароль: " + esc(c["pw_state"]))
    else:
        lines.append("Учебного аккаунта нет — обычный пользователь бота.")
    lines.append("")
    if c["classes"]:
        lines.append("📚 Мои классы:")
        for x in c["classes"]:
            lines.append(f"   • {esc(x['title'])} — " + ("бессрочно" if not x["expires_at"] else "до " + sa.local_date(x["expires_at"])))
    else:
        lines.append("📚 Мои классы: нет")
    lines += ["👑 Premium: " + (("до " + sa.local_date(prem["expires_at"])) if prem and prem.get("expires_at") else ("бессрочно" if prem else "нет")),
              f"💧 Водяные знаки: {c['wm']}"]
    from services import devices as _dev
    sess = _dev.sessions(user["tg_id"])
    lim = _dev.user_limit(user["tg_id"])
    lines.append(f"📱 Лимит устройств: {c['devices']} · сейчас {len(sess)} (в лимите {sum(1 for x in sess if x['counted'])} из {lim or '∞'})"
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
        kb += [[_btn("📚 Предметы", f"acc:es:{t}"), _btn("⏳ Срок предметов", f"acc:et:{t}")],
               [_btn("👑 Premium", f"acc:ep:{t}"), _btn("💧 Водяные знаки", f"acc:ew:{t}")],
               [_btn("📱 Устройства", f"acc:ed:{t}"), _btn("🔑 Пароль", f"acc:pw:{t}")],
               [_btn("⛔ Отключить" if active else "✅ Включить", f"acc:st:{t}"), _btn("🗑 Удалить", f"acc:del:{t}")]]
    kb.append([_btn("⚙️ Другой аккаунт", "acc:edit"), _btn("↩️ К аккаунтам", "acc:home")])
    return "\n".join(lines), _kb(kb)


async def _user_or_fail(call: CallbackQuery, tg: str):
    user = await asyncio.to_thread(sa.find_user, tg)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
    return user


async def _set_premium(user: dict, spec: str, actor) -> None:
    if spec in ("off", "none"):
        from services import trash
        await asyncio.to_thread(trash.trash_premium, user["id"], actor, "bot")
    else:
        await asyncio.to_thread(sa.apply_premium, user, spec, actor)
        sa._audit(actor, "bot", "premium", user, sa.PREMIUM_CHOICES.get(spec) or sa.term_label(spec))


@router.callback_query(F.data.startswith("acc:card:"), IsOwner())
async def cb_card(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _user_or_fail(call, call.data.split(":")[2])
    if user:
        await call.answer()
        await _show(call, *_card(user))


@router.callback_query(F.data.startswith("acc:mk:"), IsOwner())
async def cb_make(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(acc=_new_data(tg=int(call.data.split(":")[2])))
    await call.answer()
    await _subjects_step(call, state)


async def _edit_subjects_screen(target, state: FSMContext, tg, chosen):
    await _await(state, "e_subjects")
    await _show(target, "📚 <b>Предметы ученика</b>\n\nСнятая галочка закрывает предмет. Не забудьте «💾 Сохранить».\n\n"
                        "✍️ Или напишите вручную номера или названия через запятую, или <code>все</code>:\n"
                        + _subjects_list(),
                _subjects_kb(chosen, "acc:es:save", f"acc:card:{tg}", "💾 Сохранить"))


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
    await state.clear()
    await state.update_data(acc={"edit_tg": user["tg_id"], "subjects": chosen})
    await call.answer()
    await _edit_subjects_screen(call, state, user["tg_id"], chosen)


@router.callback_query(F.data.startswith("acc:et:"), IsOwner())
async def cb_edit_term(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    user = await _user_or_fail(call, parts[2])
    if not user:
        return
    if len(parts) == 4:
        await state.clear()
        n = await asyncio.to_thread(sa.set_term, user, parts[3], call.from_user.id, "bot")
        await call.answer(f"Срок: {sa.term_label(parts[3])} ({n} предм.)")
        await _show(call, *_card(await asyncio.to_thread(sa.find_user, parts[2])))
        return
    await state.clear()
    await state.update_data(acc={"edit_tg": user["tg_id"]})
    await _await(state, "e_term")
    await call.answer()
    await _show(call, "⏳ <b>Срок доступа ко всем предметам ученика</b>\n\nСрок считается с сегодняшнего дня.\n\n" + TERM_HELP,
                _kb([[_btn(v, f"acc:et:{user['tg_id']}:{k}")] for k, v in sa.TERM_CHOICES.items()]
                    + [[_btn("↩️ Назад", f"acc:card:{user['tg_id']}")]]))


@router.callback_query(F.data.startswith("acc:ep:"), IsOwner())
async def cb_edit_premium(call: CallbackQuery, state: FSMContext = None):
    parts = call.data.split(":")
    user = await _user_or_fail(call, parts[2])
    if not user:
        return
    if len(parts) == 4:
        if state:
            await state.clear()
        await _set_premium(user, parts[3], call.from_user.id)
        await call.answer("Сохранено")
        await _show(call, *_card(user))
        return
    await call.answer()
    if state:
        await state.clear()
        await state.update_data(acc={"edit_tg": user["tg_id"]})
        await _await(state, "e_premium")
    rows = [[_btn(v, f"acc:ep:{user['tg_id']}:{k}")] for k, v in sa.PREMIUM_CHOICES.items() if k != "none"]
    await _show(call, "👑 <b>Premium</b>\n\nНовый срок считается с сегодняшнего дня.\n\n" + TERM_HELP + ", <code>нет</code>",
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


@router.callback_query(F.data.startswith("acc:ed:"), IsOwner())
async def cb_edit_devices(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    user = await _user_or_fail(call, parts[2])
    if not user:
        return
    if len(parts) == 4:
        await state.clear()
        if parts[3] == "out":
            from services import devices as _dev
            n = await asyncio.to_thread(_dev.end_all, user["tg_id"], "reset")
            sa._audit(call.from_user.id, "bot", "settings", user, f"выход на всех устройствах ({n})")
            await call.answer(f"Вход завершён на устройствах: {n}", show_alert=True)
        else:
            spec = "" if parts[3] == "def" else parts[3]
            await asyncio.to_thread(sa.set_device_limit, user, spec, call.from_user.id, "bot")
            await call.answer("Лимит: " + sa.device_label(spec))
        await _show(call, *_card(await asyncio.to_thread(sa.find_user, parts[2])))
        return
    await state.clear()
    await state.update_data(acc={"edit_tg": user["tg_id"]})
    await _await(state, "e_devices")
    await call.answer()
    t = user["tg_id"]
    await _show(call, "📱 <b>Сколько устройств (браузеров) можно одновременно</b>\n\n"
                      "✍️ Или напишите число от 1 до 50, <code>без лимита</code> или <code>как у всех</code>.",
                _kb([[_btn(v, f"acc:ed:{t}:{k or 'def'}")] for k, v in sa.DEVICE_CHOICES.items()]
                    + [[_btn("🚪 Выйти на всех устройствах", f"acc:ed:{t}:out")],
                       [_btn("↩️ Назад", f"acc:card:{t}")]]))


# ───────────────────────── пароль ─────────────────────────

def _pw_done(user: dict):
    pw = sa.admin_password(user) or ""
    t = user["tg_id"]
    return (f"🔑 <b>Новый пароль для {esc(_name(user))}</b>\n\n"
            f"👤 Логин: <code>{esc(user.get('study_login') or '')}</code>\n"
            f"🔑 Пароль: <code>{esc(pw)}</code>\n\n"
            "Прежний пароль и входы на сайте больше не действуют. Пароль есть в Excel.\n"
            "Отправить его ученику в бота?",
            _kb([[_btn("✉️ Отправить ученику", f"acc:pws:{t}")], [_btn("↩️ К карточке", f"acc:card:{t}")]]))


@router.callback_query(F.data.startswith("acc:pw:"), IsOwner())
async def cb_pw(call: CallbackQuery, state: FSMContext):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    await state.clear()
    await call.answer()
    c = sa.card(user)
    t = user["tg_id"]
    now = (f"<tg-spoiler><code>{esc(c['admin_pw'])}</code></tg-spoiler> (выдал админ)" if c["admin_pw"] else esc(c["pw_state"]))
    await _show(call, f"🔑 <b>Пароль</b>\n\nСейчас: {now}\n\n"
                      "• <b>Новый автоматически</b> — бот придумает пароль.\n"
                      "• <b>Вручную</b> — напишете сами.\n"
                      "• <b>Ссылка ученику</b> — задаст сам, такой пароль не увидит никто.",
                _kb([[_btn("🎲 Новый пароль автоматически", f"acc:pwg:{t}")],
                     [_btn("✍️ Написать пароль вручную", f"acc:pwm:{t}")],
                     [_btn("🔗 Ссылка ученику (задаст сам)", f"acc:link:{t}")],
                     [_btn("↩️ Назад", f"acc:card:{t}")]]))


@router.callback_query(F.data.startswith("acc:pwg:"), IsOwner())
async def cb_pw_gen(call: CallbackQuery):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    if not user.get("study_account"):
        await call.answer("Сначала создайте учебный аккаунт", show_alert=True)
        return
    await asyncio.to_thread(sa.set_admin_password, user, "", call.from_user.id, "bot")
    await call.answer("Пароль изменён")
    await _show(call, *_pw_done(await asyncio.to_thread(sa.find_user, str(user["tg_id"]))))


@router.callback_query(F.data.startswith("acc:pwm:"), IsOwner())
async def cb_pw_manual(call: CallbackQuery, state: FSMContext):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    await state.clear()
    await state.update_data(acc={"edit_tg": user["tg_id"]})
    await _await(state, "e_password")
    await call.answer()
    await _show(call, f"✍️ Напишите новый пароль: не короче {sa.acc.MIN_PASSWORD} символов, без пробелов. "
                      "Ваше сообщение с паролем бот сразу удалит.",
                _kb([[_btn("↩️ Назад", f"acc:card:{user['tg_id']}")]]))


@router.callback_query(F.data.startswith("acc:pws:"), IsOwner())
async def cb_pw_send(call: CallbackQuery):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    pw = sa.admin_password(user)
    if not pw:
        await call.answer("Пароль уже сменил ученик — выдайте новый", show_alert=True)
        return
    info = {"login": user.get("study_login") or "", "password": pw, "token": None,
            "subjects": [c["title"] for c in sa.my_classes(user["tg_id"])]}
    sent = await sa.notify(call.bot, user, info)
    sa._audit(call.from_user.id, "bot", "settings", user, "пароль отправлен ученику")
    await call.answer("✉️ Отправлено ученику" if sent else "⚠️ Ученику не доставлено", show_alert=True)


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


# ───────────────────────── отключить, удалить ─────────────────────────

@router.callback_query(F.data.startswith("acc:st:"), IsOwner())
async def cb_status(call: CallbackQuery):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    active = (user.get("study_status") or "active") != "active"
    await asyncio.to_thread(sa.set_status, user, active, call.from_user.id, "bot")
    await call.answer("Включён" if active else "Отключён: вход на сайт закрыт")
    await _show(call, *_card(await asyncio.to_thread(sa.find_user, str(user["tg_id"]))))


@router.callback_query(F.data.startswith("acc:del:"), IsOwner())
async def cb_delete_ask(call: CallbackQuery):
    user = await _user_or_fail(call, call.data.split(":")[2])
    if not user:
        return
    await call.answer()
    t = user["tg_id"]
    has_prem = bool(sa.card(user)["premium"])
    rows = [[_btn("🗑 Удалить аккаунт" + (" (Premium оставить)" if has_prem else ""), f"acc:delok:{t}:0")]]
    if has_prem:
        rows.append([_btn("🗑 Удалить вместе с Premium", f"acc:delok:{t}:1")])
    rows.append([_btn("↩️ Не удалять", f"acc:card:{t}")])
    await _show(call, f"🗑 <b>Удалить учебный аккаунт {esc(_name(user))}?</b>\n\n"
                      "Уйдут логин, пароль и предметы «Моих классов», входы на сайте завершатся.\n"
                      "Человек останется пользователем бота: баллы, прогресс и результаты не трогаются.\n"
                      "Всё можно вернуть из корзины.", _kb(rows))


@router.callback_query(F.data.startswith("acc:delok:"), IsOwner())
async def cb_delete(call: CallbackQuery):
    parts = call.data.split(":")
    user = await _user_or_fail(call, parts[2])
    if not user:
        return
    if not user.get("study_account"):
        await call.answer("Учебного аккаунта уже нет", show_alert=True)
        return
    item = await asyncio.to_thread(sa.delete_account, user, call.from_user.id, "bot", len(parts) > 3 and parts[3] == "1")
    await call.answer("Удалено в корзину" if item else "Нечего удалять")
    await _show(call, ("🗑 Учебный аккаунт удалён в корзину. Вернуть можно в «🗑 Корзина» (бот или сайт)."
                       if item else "Учебного аккаунта уже нет."),
                _kb([[_btn("⚙️ Другой аккаунт", "acc:edit")], [_btn("↩️ К аккаунтам", "acc:home")]]))
