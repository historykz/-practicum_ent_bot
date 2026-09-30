"""
Восстановление платформы прямо в боте.

Админ жмёт «Восстановить бэкап», кидает файлы — по одному, сколько нужно.
Бот складывает части в буфер и после каждой отвечает: что принял, какая это
часть, сколько весит и сколько всего уже собрано. Пока не нажата кнопка
«Все части отправлены», ничего не восстанавливается.

Дальше бот проверяет комплект, склеивает архив, показывает его паспорт и
спрашивает подтверждение. Перед заменой делает снимок текущего состояния,
чтобы при любой ошибке откатиться обратно.
"""
import asyncio
import logging
import os
import tempfile

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import utils
from filters import IsOwner
from services import restore_service as rs

router = Router(name="restore")
log = logging.getLogger(__name__)

# Телеграм не отдаёт боту файлы больше 20 МБ — как раз поэтому части ≤19 МБ
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024


class RestoreStates(StatesGroup):
    collecting = State()


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _collect_kb(has_parts: bool):
    rows = []
    if has_parts:
        rows.append([_btn("➕ Добавить ещё часть", "rst:more")])
        rows.append([_btn("✅ Все части отправлены", "rst:done")])
    rows.append([_btn("❌ Отменить восстановление", "rst:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_owner_now(tg_id: int) -> bool:
    """Права проверяем ещё раз перед каждым опасным шагом, не только на входе."""
    try:
        return utils.is_owner(tg_id)
    except Exception:
        return False


# ===================== экран приёма =====================

def _status_text(session_id: int) -> str:
    parts = rs.session_parts(session_id)
    sess = rs.get_session(session_id)
    total = (sess["total_parts"] if sess else 0) or 0
    lines = ["📥 <b>Приём частей бэкапа</b>", ""]
    if not parts:
        lines.append("Пришлите файл бэкапа. Если он разрезан на части — "
                     "отправляйте их по очереди, в любом порядке.")
    else:
        size = sum(p["size"] for p in parts)
        lines.append(f"Принято частей: <b>{len(parts)}</b>"
                     + (f" из <b>{total}</b>" if total else "")
                     + f" · {rs.human_size(size)}")
        lines.append("")
        for p in parts[-8:]:
            lines.append(f"✅ №{p['part_no']} · {utils.escape_html(p['orig_name'])}"
                         f" · {rs.human_size(p['size'])}")
        if total:
            got = {p["part_no"] for p in parts}
            missing = [n for n in range(1, total + 1) if n not in got]
            if missing:
                lines.append("")
                lines.append("⏳ Ждём: " + ", ".join(f"№{n}" for n in missing[:12])
                             + (" …" if len(missing) > 12 else ""))
            else:
                lines.append("")
                lines.append("🎉 Все части на месте — жмите «Все части отправлены».")
    lines.append("")
    lines.append("<i>Пока вы не нажали «Все части отправлены», ничего не меняется.</i>")
    return "\n".join(lines)


@router.callback_query(F.data == "adm:restore", IsOwner())
async def cb_restore_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await asyncio.to_thread(rs.cleanup_stale)          # подчищаем брошенное
    tg_id = call.from_user.id

    sess = await asyncio.to_thread(rs.active_session, tg_id)
    if sess:
        # Незаконченный приём — продолжаем с последней принятой части
        await state.set_state(RestoreStates.collecting)
        await state.update_data(session_id=sess["id"], backup_id=sess["backup_id"])
        parts = await asyncio.to_thread(rs.session_parts, sess["id"])
        text = ("↩️ <b>Продолжаем прерванный приём</b>\n\n"
                + await asyncio.to_thread(_status_text, sess["id"]))
        await call.message.answer(text, reply_markup=_collect_kb(bool(parts)),
                                  parse_mode="HTML")
        return

    await state.set_state(RestoreStates.collecting)
    await state.update_data(session_id=None, backup_id=None)
    await call.message.answer(
        "♻️ <b>Восстановление из бэкапа</b>\n\n"
        "Пришлите файл бэкапа <b>как документ</b>. Если копия разрезана на части — "
        "отправляйте их по очереди, порядок не важен, количество заранее знать не нужно.\n\n"
        "После каждого файла я скажу, что принял и чего ещё жду. "
        "Восстановление начнётся только после кнопки «Все части отправлены».",
        reply_markup=_collect_kb(False), parse_mode="HTML")


# ===================== приём файла =====================

@router.message(RestoreStates.collecting, F.document, IsOwner())
async def msg_part(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer(
            f"⚠️ Файл {rs.human_size(doc.file_size)} — Telegram не отдаёт боту файлы "
            f"больше 20 МБ.\n\nСкачайте бэкап кнопкой «Скачать» — он приходит "
            f"частями по 19 МБ, их и пришлите.")
        return

    tmp_dir = tempfile.mkdtemp(prefix="rst_in_")
    local = os.path.join(tmp_dir, doc.file_name or "part.zip")
    status = await message.answer("⏳ Принимаю файл…")
    try:
        tg_file = await bot.get_file(doc.file_id)
        await bot.download_file(tg_file.file_path, destination=local)
    except Exception as e:
        log.warning("download part: %s", e)
        await status.edit_text(f"⚠️ Не смог скачать файл: {e}\nПопробуйте прислать ещё раз.")
        return

    info = await asyncio.to_thread(rs.read_part, local)
    if info["kind"] == "bad":
        await status.edit_text(
            f"❌ <b>{utils.escape_html(doc.file_name or 'файл')}</b> не принят.\n\n"
            f"Причина: {info['reason']}.\n\n"
            f"Пришлите этот файл заново или другой.",
            parse_mode="HTML")
        return

    data = await state.get_data()
    backup_id = info.get("backup_id") or "single"
    known = data.get("backup_id")
    if known and known != backup_id:
        await status.edit_text(
            "❌ Эта часть — <b>от другого бэкапа</b>.\n\n"
            "Части разных копий смешивать нельзя: получится битая база. "
            "Пришлите часть от той же копии либо нажмите «Отменить восстановление» "
            "и начните заново.", parse_mode="HTML")
        return

    session_id = data.get("session_id")
    if not session_id:
        session_id = await asyncio.to_thread(rs.start_session, message.from_user.id, backup_id)
        await state.update_data(session_id=session_id, backup_id=backup_id)

    res = await asyncio.to_thread(
        rs.add_part, session_id, message.from_user.id, backup_id, info, local,
        doc.file_name or "backup.zip")

    head = ("🔁 Часть заменена" if res["replaced"] else "✅ Часть принята")
    lines = [
        f"{head}",
        "",
        f"📄 Файл: <b>{utils.escape_html(doc.file_name or 'backup.zip')}</b>",
        f"🔢 Часть: <b>№{res['part_no']}</b>"
        + (f" из <b>{res['total']}</b>" if res["total"] else ""),
        f"⚖️ Размер: <b>{rs.human_size(res['size'])}</b>",
        f"📦 Всего принято частей: <b>{res['count']}</b>",
    ]
    if info["kind"] == "whole":
        lines.append("")
        lines.append("Это целый бэкап одним файлом — можно сразу нажимать "
                     "«Все части отправлены».")
    lines.append("")
    lines.append(await asyncio.to_thread(_status_text, session_id))

    await status.edit_text("\n".join(lines), reply_markup=_collect_kb(True),
                           parse_mode="HTML")


@router.callback_query(F.data == "rst:more", IsOwner())
async def cb_more(call: CallbackQuery, state: FSMContext):
    """Явно сказать «жду следующий файл» — чтобы не гадать, принимает бот или нет."""
    await call.answer()
    await state.set_state(RestoreStates.collecting)
    data = await state.get_data()
    session_id = data.get("session_id")
    tail = ""
    if session_id:
        tail = "\n\n" + await asyncio.to_thread(_status_text, session_id)
    await call.message.answer(
        "➕ Жду следующую часть. Пришлите файл документом." + tail,
        reply_markup=_collect_kb(bool(session_id)), parse_mode="HTML")


@router.message(RestoreStates.collecting, F.photo, IsOwner())
async def msg_photo_instead(message: Message):
    await message.answer(
        "⚠️ Это фото, а не файл. Бэкап нужно присылать <b>документом</b>: "
        "скрепка → «Файл», без сжатия.", parse_mode="HTML")


# ===================== подтверждение =====================

@router.callback_query(F.data == "rst:cancel", IsOwner())
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    session_id = data.get("session_id")
    if session_id:
        await asyncio.to_thread(rs.finish_session, session_id, "cancelled")
        await asyncio.to_thread(rs.log_event, call.from_user.id, session_id,
                                "cancel", "ok", "отменено админом")
    await state.clear()
    await call.answer("Отменено")
    await call.message.answer("❌ Восстановление отменено, временные части удалены. "
                              "Данные не тронуты.")


@router.callback_query(F.data == "rst:done", IsOwner())
async def cb_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    session_id = data.get("session_id")
    if not session_id:
        await call.message.answer("Сначала пришлите хотя бы один файл бэкапа.")
        return

    msg = await call.message.answer("🔍 Проверяю комплект частей…")
    ready = await asyncio.to_thread(rs.check_complete, session_id)
    if not ready.get("ok"):
        await msg.edit_text(
            f"❌ <b>Комплект неполный</b>\n\n{ready.get('error')}",
            reply_markup=_collect_kb(True), parse_mode="HTML")
        return

    await msg.edit_text("🧩 Склеиваю части в один архив…")
    joined = await asyncio.to_thread(rs.assemble, session_id)
    if not joined.get("ok"):
        await msg.edit_text(f"❌ <b>Не удалось собрать архив</b>\n\n{joined.get('error')}",
                            reply_markup=_collect_kb(True), parse_mode="HTML")
        return

    await msg.edit_text("🔎 Читаю бэкап…")
    info = await asyncio.to_thread(rs.inspect_backup, joined["path"])
    if not info.get("ok"):
        await msg.edit_text(f"❌ <b>Бэкап не подходит</b>\n\n{info.get('error')}",
                            reply_markup=_collect_kb(True), parse_mode="HTML")
        await asyncio.to_thread(rs.log_event, call.from_user.id, session_id,
                                "check", "error", info.get("error", ""))
        return

    await state.update_data(assembled=joined["path"])
    lines = [
        "📦 <b>Бэкап готов к восстановлению</b>",
        "",
        f"📅 Создан: <b>{info.get('created', '—')}</b>",
        f"⚖️ Размер: <b>{rs.human_size(info['size'])}</b>",
        f"🧩 Частей склеено: <b>{ready['count']}</b>",
        f"🗂 Таблиц в базе: <b>{info.get('tables', 0)}</b>"
        f" · файлов: <b>{info.get('uploads', 0)}</b>",
        "",
        "<b>Что вернётся:</b>",
    ]
    for label, n in info.get("counts", []):
        lines.append(f"• {label}: <b>{n}</b>")
    lines += [
        "",
        "⚠️ Текущие данные будут <b>полностью заменены</b> содержимым бэкапа. "
        "Перед заменой я сделаю снимок нынешнего состояния — если что-то пойдёт "
        "не так, всё вернётся обратно автоматически.",
        "",
        "<b>Восстановить данные из этого бэкапа?</b>",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("♻️ Да, восстановить", "rst:go")],
        [_btn("❌ Нет, отменить", "rst:cancel")],
    ])
    await msg.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


# ===================== запуск =====================

@router.callback_query(F.data == "rst:go", IsOwner())
async def cb_go(call: CallbackQuery, state: FSMContext):
    await call.answer()
    tg_id = call.from_user.id
    # Повторная проверка прав прямо перед заменой базы
    if not _is_owner_now(tg_id):
        await call.message.answer("⛔️ Недостаточно прав для восстановления.")
        return

    data = await state.get_data()
    path = data.get("assembled")
    session_id = data.get("session_id")
    if not path or not os.path.exists(path):
        await call.message.answer("Архив потерялся. Начните восстановление заново.")
        await state.clear()
        return

    msg = await call.message.answer("♻️ Восстановление начато…")
    loop = asyncio.get_running_loop()
    last = {"pct": -1}

    def progress(pct, stage):
        if pct - last["pct"] < 10 and pct < 100:
            return
        last["pct"] = pct
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        asyncio.run_coroutine_threadsafe(
            _safe_edit(msg, f"♻️ <b>Восстановление</b>\n\n{bar} {pct}%\n{stage}…"), loop)

    report = await asyncio.to_thread(rs.restore_from_zip, path, tg_id, session_id, progress)

    if not report.get("ok"):
        text = [f"❌ <b>Восстановление не выполнено</b>", "",
                f"Причина: {utils.escape_html(str(report.get('error')))}"]
        if report.get("rolled_back"):
            text.append("")
            text.append("✅ Система возвращена в исходное состояние — данные целы.")
        elif report.get("safety_path"):
            text.append("")
            text.append("⚠️ Откат не сработал. Снимок прежнего состояния сохранён "
                        f"на сервере: <code>{utils.escape_html(report['safety_path'])}</code>")
        await _safe_edit(msg, "\n".join(text))
        await state.clear()
        return

    lines = ["✅ <b>Восстановление завершено</b>", "", "<b>Вернулось в базу:</b>"]
    for label, n in report.get("restored", []):
        lines.append(f"• {label}: <b>{n}</b>")
    if report.get("skipped_files"):
        lines.append("")
        lines.append(f"⚠️ Пропущено повреждённых файлов: <b>{report['skipped_files']}</b> "
                     f"(данные в базе при этом целы)")
    lines += [
        "",
        "Сайт и бот читают одну базу — они уже показывают одинаковые данные, "
        "перезапуск не нужен.",
        "",
        f"🛟 Снимок прежнего состояния сохранён на сервере — откат возможен.",
    ]
    await _safe_edit(msg, "\n".join(lines))

    if session_id:
        await asyncio.to_thread(rs.finish_session, session_id, "done")
    await state.clear()


async def _safe_edit(msg, text):
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


# ===================== журнал восстановлений =====================

@router.callback_query(F.data == "rst:log", IsOwner())
async def cb_log(call: CallbackQuery):
    await call.answer()
    rows = await asyncio.to_thread(rs.journal, 20)
    if not rows:
        await call.message.answer("Журнал пуст — восстановлений ещё не было.")
        return
    icons = {"ok": "✅", "error": "❌"}
    stages = {"check": "проверка", "safety_copy": "снимок для отката",
              "restore": "восстановление", "rollback": "откат", "cancel": "отмена"}
    lines = ["📋 <b>Журнал восстановлений</b>", ""]
    for r in rows:
        when = (r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"{icons.get(r['result'], '•')} {when} · admin {r['admin_tg_id']} · "
            f"{stages.get(r['stage'], r['stage'])}"
            + (f"\n   <i>{utils.escape_html((r.get('details') or '')[:120])}</i>"
               if r.get("details") else ""))
    await call.message.answer("\n".join(lines), parse_mode="HTML")
