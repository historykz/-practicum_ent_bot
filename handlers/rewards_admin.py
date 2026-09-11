"""
Вознаграждения в карточке ученика (бот, раздел «🎯 Контроль обучения»).

По каждому предмету: начал ли обучение и когда, Премиум, участие в рейтинге,
место, баллы, прогресс, ДЗ, начисления, штрафы, баланс, история операций,
завершение и серийный номер грамоты. Ручной бонус / корректировка — только с
причиной и только у владельца; всё пишется в историю. Проверка грамоты по
серийному номеру или Telegram ID.
"""
import asyncio
import logging
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import utils
from filters import IsAdmin, IsOwner
from services import reward_service as rs

router = Router(name="rewards_admin")
log = logging.getLogger(__name__)


class RewardStates(StatesGroup):
    amount = State()
    reason = State()
    verify = State()


async def _show(target, text: str, kb=None):
    msg = target.message if isinstance(target, CallbackQuery) else target
    try:
        if isinstance(target, CallbackQuery):
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            return
    except Exception:
        pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


def _user_text(tg_id: int) -> tuple:
    u = utils.get_user_by_tg(tg_id) or {}
    rows = rs.user_participations(tg_id)
    name = u.get("first_name") or "без имени"
    lines = [f"💰 <b>Вознаграждения — {utils.escape_html(name)}</b>",
             (f"@{utils.escape_html(u['username'])}  " if u.get("username") else "") + f"<code>{tg_id}</code>", ""]
    if not rows:
        lines.append("Ученик ещё ни в одном предмете не нажимал «Начать обучение».")
    for r in rows:
        lines.append(f"📚 <b>{utils.escape_html(r.get('subject_title') or '—')}</b> — "
                     f"{rs.STATUS_TITLES.get(r['status'], r['status'])}"
                     + (f", место {r['current_rank']} из {r['rank_total']}" if r["is_active"] and r["current_rank"] else "")
                     + f", баланс {rs.fmt(r['balance'])}")
    return "\n".join(lines), rows


@router.callback_query(F.data.startswith("rw:user:"), IsAdmin())
async def cb_user(call: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = int(call.data.split(":")[2])
    text, rows = await asyncio.to_thread(_user_text, tg_id)
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"📚 {(r.get('subject_title') or '—')[:28]}", callback_data=f"rw:p:{r['id']}")
    kb.button(text="↩️ К карточке ученика", callback_data=f"lag:card:{tg_id}")
    kb.adjust(1)
    await _show(call, text, kb.as_markup())
    await call.answer()


def _participant_text(pid: int) -> tuple:
    p = rs.participant_by_id(pid)
    if not p:
        return None, None
    p = rs.refresh(p, rank=False)
    subj = rs.subject_row(p["subject_id"]) or {}
    ps = utils.premium_status(p["user_id"])
    u = utils.get_user_by_tg(p["tg_id"]) or {}
    cert = rs.get_certificate(p["user_id"], p["subject_id"])
    m = rs.money(p)
    prem = ("♾ бессрочно" if ps.get("forever") else
            (f"✅ до {ps['until_date']}" if ps.get("active") else
             (f"❌ истёк {ps['until_date']}" if ps.get("until_date") else "❌ нет")))
    lines = [
        f"📚 <b>{utils.escape_html(subj.get('title', ''))}</b>",
        f"👤 {utils.escape_html(u.get('first_name') or '')}"
        + (f" @{utils.escape_html(u['username'])}" if u.get("username") else "") + f"  <code>{p['tg_id']}</code>",
        "",
        f"▶️ Начал обучение: <b>{rs.fmt_dt(p['started_at'])}</b>",
        f"🕐 Последняя учёба: <b>{rs.fmt_dt(p['last_activity_at'])}</b>",
        f"💎 Премиум: <b>{prem}</b>",
        f"🏆 В рейтинге: <b>{'да' if p['is_active'] else 'нет'}</b>"
        + (f" · место <b>{p['current_rank']}</b> из {p['rank_total']}" if p["is_active"] and p["current_rank"] else ""),
        f"⭐ Баллы: <b>{p['total_points']}</b> · 🏅 достижений: {p['achievements']}",
        f"📚 Уроки: <b>{p['lessons_completed']}</b> из {p['lessons_total']}"
        + (f" ({round(p['lessons_completed'] * 100 / p['lessons_total'])}%)" if p["lessons_total"] else ""),
        f"✍️ ДЗ и пробники сдано: <b>{p['homework_completed']}</b> · активных дней: {p['learning_days']}",
        "",
        f"💰 Начислено: <b>{rs.fmt(m['earned'])}</b>",
        f"⚠️ Штрафы: <b>{rs.fmt(m['penalties'])}</b>",
        f"✏️ Бонусы и корректировки: <b>{rs.fmt(m['adjustments'], sign=True)}</b>",
        f"💳 Баланс: <b>{rs.fmt(m['balance'])}</b> · к выплате {rs.fmt(m['payout'])}",
        f"📌 Статус: <b>{rs.STATUS_TITLES.get(p['status'], p['status'])}</b>"
        + (f" ({utils.escape_html(p['annul_reason'] or '')})" if p["status"] == "annulled" else ""),
    ]
    if p.get("completed_at"):
        lines.append(f"🏁 Завершил: <b>{rs.fmt_dt(p['completed_at'])}</b>")
    if cert:
        lines.append(f"🏆 Грамота: <code>{cert['serial']}</code> на имя {utils.escape_html(cert['full_name'])}")
    txs = rs.transactions(p, limit=8)
    if txs:
        lines += ["", "<b>Последние операции:</b>"]
        for t in txs:
            lines.append(f"{t['amount_fmt']} — {utils.escape_html(t['reason'] or t['type_title'])} "
                         f"<i>({rs.fmt_dt(t['created_at'])})</i>")
    return "\n".join(lines), p


@router.callback_query(F.data.startswith("rw:p:"), IsAdmin())
async def cb_participant(call: CallbackQuery, state: FSMContext):
    await state.clear()
    pid = int(call.data.split(":")[2])
    text, p = await asyncio.to_thread(_participant_text, pid)
    if not p:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Бонус", callback_data=f"rw:adj:{pid}:bonus")
    kb.button(text="➖ Корректировка", callback_data=f"rw:adj:{pid}:correction")
    kb.button(text="📜 Вся история", callback_data=f"rw:tx:{pid}")
    kb.button(text="↩️ Назад", callback_data=f"rw:user:{p['tg_id']}")
    kb.adjust(2, 1, 1)
    await _show(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("rw:tx:"), IsAdmin())
async def cb_history(call: CallbackQuery):
    pid = int(call.data.split(":")[2])

    def _build():
        p = rs.participant_by_id(pid)
        if not p:
            return None
        rows = rs.transactions(p, limit=500)
        lines = ["📜 <b>История операций</b>", ""]
        size, shown = sum(len(x) + 1 for x in lines), 0
        for t in rows:
            who = f" · админ {t['created_by']}" if t.get("created_by") else ""
            line = (f"{t['amount_fmt']} — {utils.escape_html((t['reason'] or t['type_title'])[:200])} "
                    f"<i>[{t['type']}] {rs.fmt_dt(t['created_at'])}{who}</i>")
            # Добавляем строку целиком или не добавляем: обрезка посередине
            # тега <i> ломала разметку, и Telegram отвечал ошибкой.
            if size + len(line) + 1 > 3700:
                break
            lines.append(line)
            size += len(line) + 1
            shown += 1
        if not rows:
            lines.append("Операций нет.")
        elif shown < len(rows):
            lines += ["", f"…и ещё {len(rows) - shown} более ранних — полный список на сайте, "
                          f"в таблице участников предмета."]
        return "\n".join(lines)

    text = await asyncio.to_thread(_build)
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Назад", callback_data=f"rw:p:{pid}")
    await _show(call, text or "Запись не найдена.", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("rw:adj:"), IsOwner())
async def cb_adjust(call: CallbackQuery, state: FSMContext):
    _, _, pid, kind = call.data.split(":")
    await state.set_state(RewardStates.amount)
    await state.update_data(rw_pid=int(pid), rw_kind=kind, rw_key=uuid.uuid4().hex)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=f"rw:p:{pid}")
    await _show(call, ("➕ <b>Бонус</b>" if kind == "bonus" else "➖ <b>Корректировка</b>")
                + "\n\nНапишите сумму в тенге, например <code>500</code> или <code>62,50</code>.",
                kb.as_markup())
    await call.answer()


@router.message(RewardStates.amount, IsOwner())
async def msg_amount(message: Message, state: FSMContext):
    try:
        tiyn = rs.to_tiyn(message.text or "")
    except Exception:
        tiyn = 0
    if tiyn <= 0:
        await message.answer("Нужна сумма больше нуля, например 500.")
        return
    await state.update_data(rw_tiyn=tiyn)
    await state.set_state(RewardStates.reason)
    await message.answer(f"Сумма: <b>{rs.fmt(tiyn)}</b>.\nТеперь напишите причину — она попадёт в историю "
                         f"и ученик её увидит.", parse_mode="HTML")


@router.message(RewardStates.reason, IsOwner())
async def msg_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    pid, kind, tiyn, key = data.get("rw_pid"), data.get("rw_kind"), data.get("rw_tiyn"), data.get("rw_key")
    if not (pid and kind and tiyn and key):
        return                           # диалог уже завершён другим сообщением

    def _do():
        p = rs.participant_by_id(pid)
        if not p:
            raise ValueError("Запись ученика не найдена.")
        return rs.admin_adjust(p, kind, tiyn, message.text or "", message.from_user.id,
                               op_key=key)

    try:
        tx = await asyncio.to_thread(_do)
    except ValueError as e:
        if "уже записана" in str(e):
            return                       # повторное сообщение того же диалога — молча
        await message.answer(f"⚠️ {utils.escape_html(str(e))}")
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К ученику", callback_data=f"rw:p:{pid}")
    await message.answer(f"✅ Записано: <b>{rs.fmt(tx['amount'], sign=True)}</b> — "
                         f"{utils.escape_html(tx['reason'])}", parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "rw:verify", IsAdmin())
async def cb_verify(call: CallbackQuery, state: FSMContext):
    await state.set_state(RewardStates.verify)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="adm:study")
    await _show(call, "🔎 <b>Проверка грамоты</b>\n\nПришлите серийный номер "
                      "(например <code>IK-2026-00019482</code>) или Telegram ID ученика.", kb.as_markup())
    await call.answer()


@router.message(RewardStates.verify, IsAdmin())
async def msg_verify(message: Message, state: FSMContext):
    await state.clear()
    certs = await asyncio.to_thread(rs.verify, message.text or "")
    if not certs:
        await message.answer("❌ Грамота не найдена / недействительна.")
        return
    parts = []
    for c in certs:
        parts.append(
            ("❌ <b>Грамота отозвана</b>" if c["revoked"] else "✅ <b>Грамота подлинная</b>") + "\n"
            f"Номер: <code>{c['serial']}</code>\n"
            f"Ученик: {utils.escape_html(c['full_name'])} (<code>{c['tg_id']}</code>)\n"
            f"Предмет: {utils.escape_html(c['subject_title'])}\n"
            f"Обучение: {rs.fmt_date(c['started_at'])} — {rs.fmt_date(c['completed_at'])} ({c['days']} дн.)\n"
            f"Уроков: {c['lessons']} · к выплате: {rs.fmt(c['payout'])}"
            + (f" · место {c['rank']} из {c['rank_total']}" if c.get("rank") else "")
            + f"\nВыдана: {rs.fmt_date(c['issued_at'])}")
    await message.answer("\n\n".join(parts)[:4000], parse_mode="HTML")
