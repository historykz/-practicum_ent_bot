"""«📢 Анонсы и автотесты» в боте (v95): расписания, шаблоны анонсов, реклама после
тестов, журнал отправок с повтором, часовой пояс, ручной запуск и ручной анонс.
Логика — services/announce_service.py и services/auto_schedule_service.py."""
import asyncio
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
import utils
from filters import IsOwner
from services import announce_service as an
from services import auto_schedule_service as ass
from services import trash

router = Router(name="announce_admin")
log = logging.getLogger(__name__)
esc = utils.escape_html


class AnnStates(StatesGroup):
    tpl_text = State()
    tpl_sticker = State()
    ad_media = State()
    ad_button = State()
    ad_expiry = State()


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


# ───────────────────────── главное меню раздела ─────────────────────────

def _menu():
    cfg = an.ads_settings()
    n_err = db.fetchone("SELECT COUNT(*) AS c FROM announce_log WHERE status='error'")["c"]
    text = ("📢 <b>Анонсы и автотесты</b>\n\n"
            f"🗓 Расписаний: <b>{len(ass.list_schedules())}</b> · 🌐 пояс: {esc(an.TZ_CHOICES[an.tz_name()])}\n"
            f"📝 Шаблонов анонсов: <b>{len(an.templates())}</b>\n"
            f"🎬 Реклама: {'вкл' if cfg['enabled'] else 'выкл'}, активных роликов: <b>{len(an.ads(True))}</b>\n"
            + (f"\n⚠️ Неудачных отправок: <b>{n_err}</b> — их можно повторить в «Журнале»." if n_err else ""))
    return text, _kb([
        [_btn("🗓 Расписания", "sched:menu"), _btn("➕ Создать расписание", "sc:new")],
        [_btn("📝 Шаблоны анонсов", "ann:tpl")],
        [_btn("🎬 Реклама после тестов", "ann:ads")],
        [_btn("📜 Журнал отправок", "ann:log"), _btn("🌐 Часовой пояс", "ann:tz")],
        [_btn("↩️ В админку", "m:admin")]])


@router.callback_query(F.data == "ann:menu", IsOwner())
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *_menu())


# ───────────────────────── ручной запуск и ручной анонс ─────────────────────────

def _next_slot(s: dict) -> tuple:
    nxt = s.get("next_run_at") or ass.refresh_next_run(s["id"])
    if nxt:
        day, hm = nxt.split(" ", 1)
        return day, hm
    now = ass._now_almaty()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


def _slot_tests(s: dict, day: str, hm: str) -> list:
    """Тесты ближайшего запуска. Выбираются один раз и запоминаются — запуск возьмёт их же."""
    key = f"{day} {hm}"
    tests = an.planned_tests(s["id"], key)
    if tests is None:
        tests = ass.pick_for_slot(s)
        if tests:
            an.save_plan(s["id"], key, tests)
            tests = an.planned_tests(s["id"], key) or tests
    return tests or []


@router.callback_query(F.data.startswith("ann:man:"), IsOwner())
async def cb_manual(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    s = ass.get_schedule(sid)
    if not s:
        await call.answer("Расписание не найдено", show_alert=True)
        return
    if not s.get("channel_id"):
        await call.answer("У расписания не выбран канал: «💬 Чат и канал».", show_alert=True)
        return
    day, hm = _next_slot(s)
    await call.answer()
    await _show(call, f"📣 <b>Анонс вручную</b>\n\nБлижайший запуск: <b>{esc(day)} {esc(hm)}</b>.\n"
                      "Сначала бот пришлёт предпросмотр сюда, потом спросит, публиковать ли в канал.",
                _kb([[_btn("📣 Анонс заранее", f"ann:prev:{sid}:pre"), _btn("🚀 «Мы начали»", f"ann:prev:{sid}:start")],
                     [_btn("↩️ К расписанию", f"sc:card:{sid}")]]))


@router.callback_query(F.data.startswith("ann:prev:"), IsOwner())
async def cb_preview(call: CallbackQuery):
    _, _, sid, kind = call.data.split(":")
    s = ass.get_schedule(int(sid))
    if not s:
        await call.answer("Расписание не найдено", show_alert=True)
        return
    day, hm = _next_slot(s)
    tests = await asyncio.to_thread(_slot_tests, s, day, hm)
    if not tests:
        await call.answer("Нет тестов для этого расписания.", show_alert=True)
        return
    await call.answer("Предпросмотр ниже")
    res = await an.preview(call.bot, call.from_user.id, kind, s, tests, hm, day)
    if not res.get("ok"):
        await call.message.answer("⚠️ Предпросмотр не отправлен: " + esc(res.get("error") or ""))
        return
    warn = "" if res.get("link") else "\n⚠️ Ссылка на чат не задана — кнопки не будет."
    await call.message.answer(f"☝️ Так анонс будет выглядеть в канале <code>{esc(str(s['channel_id']))}</code>.{warn}\n"
                              "Опубликовать?", parse_mode="HTML",
                              reply_markup=_kb([[_btn("✅ Опубликовать", f"ann:pub:{sid}:{kind}"),
                                                 _btn("❌ Отмена", f"sc:card:{sid}")]]))


@router.callback_query(F.data.startswith("ann:pub:"), IsOwner())
async def cb_publish(call: CallbackQuery):
    _, _, sid, kind = call.data.split(":")
    s = ass.get_schedule(int(sid))
    if not s:
        await call.answer("Расписание не найдено", show_alert=True)
        return
    day, hm = _next_slot(s)
    tests = await asyncio.to_thread(_slot_tests, s, day, hm)
    # Анонс заранее для ближайшего запуска — тот же ключ, что у автоматического: второй раз не уйдёт
    key = f"pre:{s['id']}:{day} {hm}" if kind == "pre" else ""
    res = await an.announce(call.bot, kind, s, tests, hm, day, key=key, manual=True)
    trash.audit(call.from_user.id, "", "bot", "announce", "", f"Анонс вручную ({an.KINDS[kind]})", s["id"],
                "отправлен" if res.get("ok") else res.get("error", ""))
    if res.get("already"):
        await call.answer("Этот анонс уже был опубликован.", show_alert=True)
    elif res.get("ok"):
        await call.answer("✅ Опубликовано", show_alert=True)
    else:
        await call.answer("⚠️ " + (res.get("error") or "ошибка")[:180], show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ann:run:"), IsOwner())
async def cb_run_now(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    await call.answer()
    await _show(call, "▶️ <b>Запустить тест сейчас?</b>\n\nВ чате начнётся тест из этого расписания, "
                      "в канал уйдёт «Мы начали», если он включён. Расписание дальше работает как обычно.",
                _kb([[_btn("✅ Запустить", f"ann:runok:{sid}"), _btn("❌ Отмена", f"sc:card:{sid}")]]))


@router.callback_query(F.data.startswith("ann:runok:"), IsOwner())
async def cb_run_ok(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    s = ass.get_schedule(sid)
    if not s:
        await call.answer("Расписание не найдено", show_alert=True)
        return
    key = ass._now_almaty().strftime("%Y-%m-%d %H:%M")
    tests = await asyncio.to_thread(ass.pick_for_slot, s)
    if not tests:
        await call.answer("Нет тестов для запуска.", show_alert=True)
        return
    added = sum(ass.record_run(sid, t["id"], slot_key=key, status="queued") for t in tests)
    if not added:
        await call.answer("Этот запуск уже в очереди.", show_alert=True)
        return
    an.save_plan(sid, key, tests)
    trash.audit(call.from_user.id, "", "bot", "run", "", "Запуск вручную", sid, f"тестов: {len(tests)}")
    await call.answer("▶️ Запускаю", show_alert=True)
    ok = await ass.launch_next(call.bot, sid, key)
    await call.message.answer("✅ Тест запущен." if ok else
                              "⏳ В чате сейчас другой тест — запуск начнётся после него. "
                              "Если запуск не удался, причина будет в «📜 История запусков».")


# ───────────────────────── шаблоны ─────────────────────────

def _tpl_screen():
    rows = an.templates()
    lines = ["📝 <b>Шаблоны анонсов</b>\n",
             "Подстановки: " + ", ".join("{" + k + "}" for k in an.PLACEHOLDERS),
             "Premium-эмодзи: пришлите текст с ними — бот сохранит их. Если Telegram не разрешит боту "
             "такие эмодзи, в канал уйдут обычные.\n"]
    kb = []
    for t in rows:
        cat = db.fetchone("SELECT name FROM test_categories WHERE id=?", (t["category_id"],)) if t["category_id"] else None
        lines.append(f"• {an.KINDS[t['kind']]}: <b>{esc(t['title'] or '')}</b>"
                     f"{' · ' + esc(cat['name']) if cat else ' · для всех предметов'}{' · 🖼 стикер' if t['sticker_file_id'] else ''}")
        kb.append([_btn(f"👁 {t['title'][:24]}", f"ann:tpv:{t['id']}"), _btn("🗑", f"ann:tpd:{t['id']}")])
    if not rows:
        lines.append("<i>Своих шаблонов нет — используются встроенные тексты.</i>")
    kb += [[_btn("➕ Анонс заранее", "ann:tpn:pre"), _btn("➕ «Мы начали»", "ann:tpn:start")],
           [_btn("↩️ Назад", "ann:menu")]]
    return "\n".join(lines), _kb(kb)


@router.callback_query(F.data == "ann:tpl", IsOwner())
async def cb_tpl(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *_tpl_screen())


@router.callback_query(F.data.startswith("ann:tpn:"), IsOwner())
async def cb_tpl_new(call: CallbackQuery, state: FSMContext):
    kind = call.data.split(":")[2]
    cats = db.fetchall("SELECT id, name FROM test_categories ORDER BY sort_order, id LIMIT 40")
    await state.update_data(tpl_kind=kind)
    await call.answer()
    rows = [[_btn("🌐 Для всех предметов", "ann:tpc:0")]] + [[_btn(c["name"][:40], f"ann:tpc:{c['id']}")] for c in cats]
    await _show(call, f"📝 <b>{an.KINDS[kind]}</b>\n\nДля какого предмета (раздела) этот шаблон?",
                _kb(rows + [[_btn("❌ Отмена", "ann:tpl")]]))


@router.callback_query(F.data.startswith("ann:tpc:"), IsOwner())
async def cb_tpl_cat(call: CallbackQuery, state: FSMContext):
    await state.update_data(tpl_cat=int(call.data.split(":")[2]) or None)
    await state.set_state(AnnStates.tpl_text)
    kind = (await state.get_data()).get("tpl_kind") or "pre"
    await call.answer()
    await call.message.answer(
        "✍️ Пришлите текст анонса одним сообщением. Можно жирный шрифт, ссылки и Premium-эмодзи.\n\n"
        "Подстановки: " + ", ".join("<code>{" + k + "}</code>" for k in an.PLACEHOLDERS) + "\n\n"
        "Пример встроенного текста:\n\n" + an.DEFAULT_TEXTS[kind] + "\n\n/cancel — отмена", parse_mode="HTML")


@router.message(AnnStates.tpl_text, IsOwner())
async def msg_tpl_text(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    body = message.html_text if (message.text or message.caption) else ""
    if not body.strip():
        await message.answer("Нужен текст. Пришлите ещё раз или /cancel.")
        return
    await state.update_data(tpl_body=body)
    await state.set_state(AnnStates.tpl_sticker)
    await message.answer("🖼 Пришлите стикер, который будет перед анонсом, или нажмите «Без стикера».",
                         reply_markup=_kb([[_btn("⏭ Без стикера", "ann:tps:skip")]]))


async def _save_tpl(target, state: FSMContext, sticker: str = ""):
    d = await state.get_data()
    tid = an.save_template(None, (d.get("tpl_body") or "")[:30].split("\n")[0], d.get("tpl_kind") or "pre",
                           d.get("tpl_body") or "", d.get("tpl_cat"), sticker)
    await state.clear()
    trash.audit(target.from_user.id, "", "bot", "save", "", "Шаблон анонса", tid, an.KINDS[d.get("tpl_kind") or "pre"])
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer("✅ Шаблон сохранён. Предпросмотр:")
    await _send_tpl_preview(msg.bot, target.from_user.id, tid)
    await _show(msg, *_tpl_screen())


@router.message(AnnStates.tpl_sticker, IsOwner())
async def msg_tpl_sticker(message: Message, state: FSMContext):
    if not message.sticker:
        await message.answer("Это не стикер. Пришлите стикер или нажмите «Без стикера».")
        return
    await _save_tpl(message, state, message.sticker.file_id)


@router.callback_query(F.data == "ann:tps:skip", IsOwner())
async def cb_tpl_nosticker(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _save_tpl(call, state)


async def _send_tpl_preview(bot, to: int, tid: int):
    t = an.get_template(tid)
    if not t:
        return
    sched = (ass.list_schedules() or [{}])[0]
    sample = {"предмет": "Биология", "темы": "• Строение клетки\n• Деление клетки", "вопросов": "20",
              "время": "19:00", "дата": datetime.now().strftime("%d.%m.%Y"), "тестов": "2",
              "ссылка": an.chat_link(sched) or "https://t.me/+пример"}
    try:
        await an.send_html(bot, to, an.render(t["body"], sample), an.button(an.chat_link(sched) or "https://t.me/telegram"),
                           t.get("sticker_file_id"))
    except Exception as e:
        await bot.send_message(to, "⚠️ Предпросмотр не отправлен: " + esc(str(e))[:300])


@router.callback_query(F.data.startswith("ann:tpv:"), IsOwner())
async def cb_tpl_view(call: CallbackQuery):
    await call.answer("Предпросмотр ниже")
    await _send_tpl_preview(call.bot, call.from_user.id, int(call.data.split(":")[2]))


@router.callback_query(F.data.startswith("ann:tpd:"), IsOwner())
async def cb_tpl_del(call: CallbackQuery):
    tid = int(call.data.split(":")[2])
    an.delete_template(tid)
    trash.audit(call.from_user.id, "", "bot", "delete", "", "Шаблон анонса", tid, "")
    await call.answer("Удалён")
    await _show(call, *_tpl_screen())


# ───────────────────────── реклама ─────────────────────────

def _ads_screen():
    cfg = an.ads_settings()
    rows = an.ads()
    now = utils.now_iso()
    lines = ["🎬 <b>Реклама после тестов</b>\n",
             "После итогов каждого группового теста бот отправляет рекламу в тот же чат.\n",
             f"Сейчас: <b>{'включено' if cfg['enabled'] else 'выключено'}</b> · порядок: "
             f"<b>{'по очереди' if cfg['mode'] == 'sequence' else 'случайно'}</b> · за раз: <b>{cfg['per_test']}</b> · "
             f"частота: <b>{'после каждого теста' if cfg['every_n'] == 1 else 'раз в ' + str(cfg['every_n']) + ' теста'}</b>\n"]
    kb = []
    for a in rows:
        expired = a.get("expires_at") and a["expires_at"] <= now
        mark = "⏰" if expired else ("✅" if a["enabled"] else "⏸")
        lines.append(f"{mark} {an.AD_KINDS[a['kind']]}: <b>{esc(a['title'] or '')}</b> · показов {a['sent_count'] or 0}"
                     + (f" · до {a['expires_at'][:10]}" if a.get("expires_at") else ""))
        kb.append([_btn(f"{mark} {a['title'][:22]}", f"ann:adt:{a['id']}"), _btn("👁", f"ann:adv:{a['id']}"),
                   _btn("🗑", f"ann:add:{a['id']}")])
    if not rows:
        lines.append("<i>Роликов пока нет.</i>")
    kb += [[_btn("➕ Добавить рекламу", "ann:adn")],
           [_btn("⏸ Выключить" if cfg["enabled"] else "▶️ Включить", "ann:ads:on"),
            _btn("🔀 Случайно" if cfg["mode"] == "sequence" else "🔁 По очереди", "ann:ads:mode")],
           [_btn(f"{'• ' if cfg['per_test'] == n else ''}{n} за раз", f"ann:ads:per:{n}") for n in (1, 2, 3)],
           [_btn(f"{'• ' if cfg['every_n'] == n else ''}{'каждый' if n == 1 else 'раз в ' + str(n)}", f"ann:ads:ev:{n}")
            for n in (1, 2, 3, 5)],
           [_btn("↩️ Назад", "ann:menu")]]
    return "\n".join(lines), _kb(kb)


@router.callback_query(F.data == "ann:ads", IsOwner())
async def cb_ads(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show(call, *_ads_screen())


@router.callback_query(F.data.startswith("ann:ads:"), IsOwner())
async def cb_ads_set(call: CallbackQuery):
    cfg = an.ads_settings()
    arg = call.data.split(":")[2:]
    if arg[0] == "on":
        cfg["enabled"] = not cfg["enabled"]
    elif arg[0] == "mode":
        cfg["mode"] = "random" if cfg["mode"] == "sequence" else "sequence"
    elif arg[0] == "per":
        cfg["per_test"] = int(arg[1])
    elif arg[0] == "ev":
        cfg["every_n"] = int(arg[1])
    an.save_ads_settings(cfg["enabled"], cfg["mode"], cfg["per_test"], cfg["every_n"])
    trash.audit(call.from_user.id, "", "bot", "settings", "", "Реклама после тестов", None, str(cfg))
    await call.answer("Сохранено")
    await _show(call, *_ads_screen())


@router.callback_query(F.data.startswith("ann:adt:"), IsOwner())
async def cb_ad_toggle(call: CallbackQuery):
    an.toggle_ad(int(call.data.split(":")[2]))
    await call.answer("Готово")
    await _show(call, *_ads_screen())


@router.callback_query(F.data.startswith("ann:adv:"), IsOwner())
async def cb_ad_view(call: CallbackQuery):
    ad = an.get_ad(int(call.data.split(":")[2]))
    await call.answer()
    if ad:
        try:
            await an.send_ad(call.bot, call.from_user.id, ad)
        except Exception as e:
            await call.message.answer("⚠️ " + esc(str(e))[:300])


@router.callback_query(F.data.startswith("ann:add:"), IsOwner())
async def cb_ad_delete(call: CallbackQuery):
    ad_id = int(call.data.split(":")[2])
    ad = an.get_ad(ad_id)
    if ad:
        await asyncio.to_thread(trash.move, "ad", ad["title"] or "Реклама", call.from_user.id, "bot", [("ads", ad_id)],
                                meta={"location": "Реклама после тестов"})
    await call.answer(trash.MOVED_MSG, show_alert=True)
    await _show(call, *_ads_screen())


@router.callback_query(F.data == "ann:adn", IsOwner())
async def cb_ad_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(AnnStates.ad_media)
    await call.answer()
    await call.message.answer("🎬 Пришлите рекламу одним сообщением: видео, картинку, GIF или просто текст. "
                              "Подпись к видео или картинке тоже сохранится.\n\n/cancel — отмена")


@router.message(AnnStates.ad_media, IsOwner())
async def msg_ad_media(message: Message, state: FSMContext):
    if (message.text or "").startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    if message.video:
        kind, fid = "video", message.video.file_id
    elif message.animation:
        kind, fid = "animation", message.animation.file_id
    elif message.photo:
        kind, fid = "photo", message.photo[-1].file_id
    elif message.text:
        kind, fid = "text", None
    else:
        await message.answer("Поддерживаются видео, картинка, GIF и текст.")
        return
    caption = message.html_text if (message.caption or message.text) else ""
    title = (message.caption or message.text or an.AD_KINDS[kind]).strip().split("\n")[0][:40]
    await state.update_data(ad={"kind": kind, "file_id": fid, "caption": caption, "title": title})
    await state.set_state(AnnStates.ad_button)
    await message.answer("🔗 Кнопка под рекламой: пришлите <code>Текст кнопки | https://ссылка</code> "
                         "или нажмите «Без кнопки».", parse_mode="HTML",
                         reply_markup=_kb([[_btn("⏭ Без кнопки", "ann:adb:skip")]]))


@router.message(AnnStates.ad_button, IsOwner())
async def msg_ad_button(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if "|" not in raw or not raw.split("|", 1)[1].strip().startswith(("http://", "https://")):
        await message.answer("Формат: <code>Купить курс | https://example.kz</code>", parse_mode="HTML")
        return
    text, url = [x.strip() for x in raw.split("|", 1)]
    d = await state.get_data()
    d["ad"].update(button_text=text[:60], button_url=url[:500])
    await state.update_data(ad=d["ad"])
    await _ask_expiry(message, state)


@router.callback_query(F.data == "ann:adb:skip", IsOwner())
async def cb_ad_nobutton(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _ask_expiry(call.message, state)


async def _ask_expiry(msg: Message, state: FSMContext):
    await state.set_state(AnnStates.ad_expiry)
    await msg.answer("📅 До какой даты показывать? Пришлите дату как <code>2026-12-31</code> или нажмите «Бессрочно».",
                     parse_mode="HTML", reply_markup=_kb([[_btn("♾ Бессрочно", "ann:ade:none")]]))


async def _save_ad(target, state: FSMContext, expires: str = ""):
    d = (await state.get_data()).get("ad") or {}
    ad_id = an.save_ad(None, title=d.get("title") or "", kind=d.get("kind") or "text", caption=d.get("caption") or "",
                       button_text=d.get("button_text") or "", button_url=d.get("button_url") or "",
                       file_id=d.get("file_id"), expires_at=expires, actor=target.from_user.id)
    await state.clear()
    trash.audit(target.from_user.id, "", "bot", "create", "", "Реклама после тестов", ad_id, d.get("title") or "")
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer("✅ Реклама добавлена. Так её увидят в чате:")
    try:
        await an.send_ad(msg.bot, target.from_user.id, an.get_ad(ad_id))
    except Exception as e:
        await msg.answer("⚠️ " + esc(str(e))[:300])
    await _show(msg, *_ads_screen())


@router.message(AnnStates.ad_expiry, IsOwner())
async def msg_ad_expiry(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not ass._valid_date(raw):
        await message.answer("Дата как 2026-12-31, или нажмите «Бессрочно».")
        return
    await _save_ad(message, state, raw)


@router.callback_query(F.data == "ann:ade:none", IsOwner())
async def cb_ad_noexpiry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _save_ad(call, state)


# ───────────────────────── журнал и пояс ─────────────────────────

def _log_screen():
    rows = an.journal(15)
    lines = ["📜 <b>Журнал анонсов и рекламы</b> (последние 15)\n"]
    kb = []
    for r in rows:
        st = {"sent": "✅", "error": "⚠️", "sending": "⏳"}.get(r["status"], r["status"])
        lines.append(f"{st} {esc(r['kind_title'])} → <code>{esc(str(r['target']))}</code> · "
                     f"{esc((r.get('sent_at') or r.get('created_at') or '')[:16].replace('T', ' '))}"
                     + (f"\n   {esc(r['error'][:120])}" if r.get("error") else ""))
        if r["status"] == "error":
            kb.append([_btn(f"🔁 Повторить #{r['id']}", f"ann:retry:{r['id']}")])
    if not rows:
        lines.append("<i>Пока пусто.</i>")
    kb.append([_btn("↩️ Назад", "ann:menu")])
    return "\n".join(lines), _kb(kb)


@router.callback_query(F.data == "ann:log", IsOwner())
async def cb_log(call: CallbackQuery):
    await call.answer()
    await _show(call, *_log_screen())


@router.callback_query(F.data.startswith("ann:retry:"), IsOwner())
async def cb_retry(call: CallbackQuery):
    res = await an.retry(call.bot, int(call.data.split(":")[2]))
    await call.answer("✅ Отправлено" if res.get("ok") else "⚠️ " + (res.get("error") or "")[:180], show_alert=True)
    await _show(call, *_log_screen())


@router.callback_query(F.data == "ann:tz", IsOwner())
async def cb_tz(call: CallbackQuery):
    cur = an.tz_name()
    await call.answer()
    await _show(call, "🌐 <b>Часовой пояс расписаний</b>\n\nВремя запусков и анонсов считается в этом поясе.",
                _kb([[_btn(("• " if k == cur else "") + v, f"ann:tzs:{k}")] for k, v in an.TZ_CHOICES.items()]
                    + [[_btn("↩️ Назад", "ann:menu")]]))


@router.callback_query(F.data.startswith("ann:tzs:"), IsOwner())
async def cb_tz_set(call: CallbackQuery):
    name = call.data.split(":", 2)[2]
    if an.set_tz(name):
        for s in ass.list_schedules():
            ass.refresh_next_run(s["id"])
        trash.audit(call.from_user.id, "", "bot", "settings", "", "Часовой пояс расписаний", None, name)
    await call.answer("Сохранено")
    await _show(call, *_menu())
