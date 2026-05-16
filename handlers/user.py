“”“Хендлеры пользователя: каталог тестов, карточка теста, запуск, шеринг.”””
import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import config
import database as db
import utils
from locales import t
from keyboards import (tests_list_kb, test_card_kb, paid_test_kb, subscription_kb,
back_kb, main_menu_kb)
from services import test_runner, subscription_service, share_service

router = Router(name=“user”)
log = logging.getLogger(**name**)

def _resolve_lang(user: dict) -> str:
return user.get(‘language’) or ‘ru’

def *ttype_label(ttype: str, lang: str) -> str:
return t(f”ttype*{ttype}”, lang)

def _access_label(test: dict, lang: str) -> str:
if test[‘is_paid’]:
return t(“access_paid”, lang)
return t(“access_free”, lang)

def _list_active_tests(language: str, ttype_filter: str = None) -> list[dict]:
if ttype_filter:
rows = db.fetchall(
“”“SELECT * FROM tests WHERE status=‘active’ AND language=? AND test_type=?
ORDER BY id DESC”””, (language, ttype_filter))
else:
rows = db.fetchall(
“”“SELECT * FROM tests WHERE status=‘active’ AND language=?
AND test_type NOT IN (‘daily’,‘duel’,‘tournament’) ORDER BY id DESC”””,
(language,))
return [dict(r) for r in rows]

@router.callback_query(F.data == “m:tests”)
async def cb_tests_menu(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
tests = _list_active_tests(lang)
text = t(“tests_catalog”, lang)
if not tests:
text += “\n\n” + t(“no_tests”, lang)
try:
await call.message.edit_text(text, reply_markup=tests_list_kb(tests, lang, page=0))
except Exception:
await call.message.answer(text, reply_markup=tests_list_kb(tests, lang, page=0))
await call.answer()

@router.callback_query(F.data.startswith(“testspage:”))
async def cb_tests_page(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
try:
page = int(call.data.split(”:”)[1])
except (ValueError, IndexError):
page = 0
tests = _list_active_tests(lang)
try:
await call.message.edit_reply_markup(reply_markup=tests_list_kb(tests, lang, page=page))
except Exception:
pass
await call.answer()

async def show_test_card(bot: Bot, chat_id: int, user_tg_id: int, test_id: int, lang: str):
“”“Показать карточку теста — используется и из callback, и из deep-link.”””
test = test_runner.get_test(test_id)
if not test or test[‘status’] != ‘active’:
await bot.send_message(chat_id, t(“test_not_found”, lang),
reply_markup=back_kb(lang, “m:tests”))
return

```
qcount = db.fetchone(
    "SELECT COUNT(*) AS c FROM questions WHERE test_id=?", (test['id'],))['c']
user = utils.get_user_by_tg(user_tg_id)
has_access = utils.has_paid_access(user['id'], test_id=test['id']) if user else False

text = t("test_card", lang,
         title=utils.escape_html(test['title']),
         description=utils.escape_html(test['description'] or ""),
         subject=utils.escape_html(test['subject'] or "—"),
         grade=test['grade'] or "—",
         category=utils.escape_html(test['category'] or "—"),
         langlabel=lang.upper(),
         ttype=_ttype_label(test['test_type'], lang),
         qcount=qcount,
         time_per_q=test['time_per_question'],
         attempts_limit=test['attempts_limit'] or "∞",
         access=_access_label(test, lang))

if test['is_paid'] and not has_access:
    await bot.send_message(chat_id, text)
    await bot.send_message(
        chat_id,
        t("paid_test_card", lang, price=test['price'], manager=config.MANAGER_USERNAME),
        reply_markup=paid_test_kb(test['id'], lang, config.MANAGER_USERNAME),
    )
    return

await bot.send_message(
    chat_id, text,
    reply_markup=test_card_kb(test['id'], lang,
                               allow_group=bool(test['allow_in_group'])),
)
```

@router.callback_query(F.data.startswith(“test:”))
async def cb_test_card(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
try:
test_id = int(call.data.split(”:”)[1])
except (ValueError, IndexError):
await call.answer()
return
try:
await call.message.delete()
except Exception:
pass
await show_test_card(call.bot, call.message.chat.id, call.from_user.id, test_id, lang)
await call.answer()

@router.callback_query(F.data.startswith(“checkacc:”))
async def cb_check_access(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
try:
test_id = int(call.data.split(”:”)[1])
except (ValueError, IndexError):
await call.answer()
return
test = test_runner.get_test(test_id)
if not test:
await call.answer(t(“test_not_found”, lang), show_alert=True)
return
if utils.has_paid_access(user[‘id’], test_id=test[‘id’]):
await call.answer(t(“access_granted”, lang), show_alert=True)
try:
await call.message.delete()
except Exception:
pass
await show_test_card(call.bot, call.message.chat.id, call.from_user.id,
test_id, lang)
else:
await call.answer(t(“access_still_none”, lang), show_alert=True)

@router.callback_query(F.data.startswith(“run:”))
async def cb_run_test(call: CallbackQuery, state: FSMContext, user: dict):
lang = _resolve_lang(user)
try:
test_id = int(call.data.split(”:”)[1])
except (ValueError, IndexError):
await call.answer()
return
test = test_runner.get_test(test_id)
if not test:
await call.answer(t(“test_not_found”, lang), show_alert=True)
return

```
# В группе ли мы? Только в личке стандартный запуск
if call.message.chat.type != "private":
    await call.answer(t("personal_chat_only", lang), show_alert=True)
    return

# Проверка доступа
if test['is_paid'] and not utils.has_paid_access(user['id'], test_id=test['id']):
    await call.answer(t("noaccess_test", lang, manager=config.MANAGER_USERNAME),
                      show_alert=True)
    return

# Лимит попыток
if test['attempts_limit']:
    used = test_runner.count_user_attempts(user['id'], test_id)
    if used >= test['attempts_limit']:
        await call.answer(t("attempts_exhausted", lang), show_alert=True)
        return

# Проверка подписки на канал
channel = subscription_service.get_required_channel_for_test(test_id)
if channel:
    ok = await subscription_service.check_user_subscription(
        call.bot, channel, call.from_user.id)
    if not ok:
        await call.message.answer(
            t("must_subscribe", lang),
            reply_markup=subscription_kb(channel, lang, f"checksub:test:{test_id}")
        )
        await call.answer()
        return

# Есть вопросы?
qs = test_runner.get_test_questions(test_id)
if not qs:
    await call.answer(t("no_questions", lang), show_alert=True)
    return

# Создаём попытку
attempt_id = test_runner.create_attempt(
    user['id'], test_id, lang, group_id=None,
    started_by_user_id=user['id'])

try:
    await call.message.delete()
except Exception:
    pass
await call.message.answer(t("test_start_warning", lang))
# Небольшая пауза перед стартом
await asyncio.sleep(min(config.TEST_START_COOLDOWN_SECONDS, 3))
await test_runner.send_current_question(call.bot, attempt_id, call.message.chat.id)
await call.answer()
```

@router.callback_query(F.data.startswith(“checksub:”))
async def cb_check_subscription(call: CallbackQuery, state: FSMContext, user: dict):
“”“checksub:test:{id} или checksub:note:{id}”””
lang = _resolve_lang(user)
parts = call.data.split(”:”)
if len(parts) < 3:
await call.answer()
return
kind, obj_id_s = parts[1], parts[2]
try:
obj_id = int(obj_id_s)
except ValueError:
await call.answer()
return

```
if kind == "test":
    channel = subscription_service.get_required_channel_for_test(obj_id)
elif kind == "note":
    channel = subscription_service.get_required_channel_for_note(obj_id)
else:
    channel = None

if not channel:
    await call.answer(t("sub_ok", lang), show_alert=True)
    return

ok = await subscription_service.check_user_subscription(
    call.bot, channel, call.from_user.id)
if ok:
    await call.answer(t("sub_ok", lang), show_alert=False)
    try:
        await call.message.delete()
    except Exception:
        pass
    # Имитируем повторное нажатие run/read
    if kind == "test":
        # Запустить тест
        fake_data = f"run:{obj_id}"
        call.data = fake_data
        await cb_run_test(call, state, user)
    elif kind == "note":
        from handlers.notes import show_note_card
        await show_note_card(call.bot, call.message.chat.id, call.from_user.id,
                             obj_id, lang)
else:
    await call.answer(t("sub_fail", lang), show_alert=True)
```

@router.callback_query(F.data == “m:share”)
async def cb_share(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
tests = _list_active_tests(lang)
if not tests:
await call.answer(t(“no_tests”, lang), show_alert=True)
return
# Выбор теста
from aiogram.utils.keyboard import InlineKeyboardBuilder
kb = InlineKeyboardBuilder()
for tst in tests[:20]:
kb.button(text=tst[‘title’][:60], callback_data=f”sharetest:{tst[‘id’]}”)
kb.button(text=t(“btn_back”, lang), callback_data=“m:menu”)
kb.adjust(1)
try:
await call.message.edit_text(t(“share_choose_test”, lang),
reply_markup=kb.as_markup())
except Exception:
await call.message.answer(t(“share_choose_test”, lang),
reply_markup=kb.as_markup())
await call.answer()

@router.callback_query(F.data.startswith(“sharetest:”))
async def cb_share_test(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
try:
test_id = int(call.data.split(”:”)[1])
except (ValueError, IndexError):
await call.answer()
return
link = share_service.build_test_deep_link(test_id)
text = t(“share_link_msg”, lang, link=link)
try:
await call.message.edit_text(text, reply_markup=back_kb(lang, “m:menu”),
disable_web_page_preview=True)
except Exception:
await call.message.answer(text, reply_markup=back_kb(lang, “m:menu”))
await call.answer()

@router.callback_query(F.data == “m:results”)
async def cb_my_results(call: CallbackQuery, user: dict):
lang = _resolve_lang(user)
rows = db.fetchall(
“”“SELECT a.id, a.score, a.correct_answers, a.wrong_answers, a.skipped_answers,
a.created_at, t.title
FROM test_attempts a JOIN tests t ON t.id = a.test_id
WHERE a.user_id=? AND a.status=‘finished’
ORDER BY a.id DESC LIMIT 20”””, (user[‘id’],))
if not rows:
text = t(“my_attempts_empty”, lang)
else:
lines = [t(“my_attempts_title”, lang), “”]
for r in rows:
total = r[‘correct_answers’] + r[‘wrong_answers’] + r[‘skipped_answers’]
pct = round(r[‘correct_answers’] / total * 100) if total else 0
line = (f”• <b>{utils.escape_html(r[‘title’])}</b> — “
f”{r[‘correct_answers’]}/{total} ({pct}%, {r[‘score’]} б.)”)
lines.append(line)
text = “\n”.join(lines)
try:
await call.message.edit_text(text, reply_markup=back_kb(lang, “m:menu”))
except Exception:
await call.message.answer(text, reply_markup=back_kb(lang, “m:menu”))
await call.answer()
