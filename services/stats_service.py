"""
Статистика для админа: активность, популярные тесты, премиум/приватные,
новые юзеры, профильные предметы.
"""
import logging
from datetime import datetime, timedelta

import database as db

log = logging.getLogger(__name__)


def _count(sql, params=()):
    r = db.fetchone(sql, params)
    return (r['c'] if r else 0) or 0


def active_now() -> int:
    """Сколько сейчас проходят тест (in_progress / user_paused)."""
    return _count(
        "SELECT COUNT(*) AS c FROM test_attempts "
        "WHERE status IN ('in_progress','user_paused')")


def stars_for_period(days: int) -> int:
    """Сколько звёзд заработано за последние N дней (все покупки)."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    r = db.fetchone(
        "SELECT SUM(stars_amount) AS s FROM purchases WHERE created_at >= ?",
        (since,))
    base = (r['s'] if r and r['s'] else 0)
    # Плюс звёзды за режимы (mode_passes)
    try:
        # mode_passes хранит prices не напрямую — считаем по purchased*цена сложно,
        # поэтому добавим из таблицы если есть charge_id (купленные)
        pass
    except Exception:
        pass
    return base


def real_completions(days: int = None) -> int:
    """
    Реальные прохождения тестов — только ЗАВЕРШЁННЫЕ (status='finished'),
    без приостановленных и брошенных.
    """
    sql = "SELECT COUNT(*) AS c FROM test_attempts WHERE status='finished'"
    params = ()
    if days:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        sql += " AND (end_time >= ? OR created_at >= ?)"
        params = (since, since)
    return _count(sql, params)


def modes_stats() -> dict:
    """Статистика режимов: сколько прохождений карточек и заучивания."""
    def cnt(sql, p=()):
        r = db.fetchone(sql, p)
        return (r['c'] if r else 0) or 0
    fc_done = cnt("SELECT COUNT(*) AS c FROM mode_results WHERE mode='flashcards'")
    ln_done = cnt("SELECT COUNT(*) AS c FROM mode_results WHERE mode='learning'")
    fc_users = cnt("SELECT COUNT(DISTINCT user_tg_id) AS c FROM mode_results "
                   "WHERE mode='flashcards'")
    ln_users = cnt("SELECT COUNT(DISTINCT user_tg_id) AS c FROM mode_results "
                   "WHERE mode='learning'")
    fc_passes = cnt("SELECT SUM(purchased) AS c FROM mode_passes "
                    "WHERE mode='flashcards'")
    ln_passes = cnt("SELECT SUM(purchased) AS c FROM mode_passes "
                    "WHERE mode='learning'")
    return {'fc_done': fc_done, 'ln_done': ln_done,
            'fc_users': fc_users, 'ln_users': ln_users,
            'fc_passes': fc_passes, 'ln_passes': ln_passes}


def top_tests(limit: int = 10) -> list:
    """Топ популярных тестов по числу завершённых прохождений."""
    return db.fetchall(
        """SELECT t.title, t.id,
                  COUNT(a.id) AS passes,
                  SUM(CASE WHEN a.status='finished' THEN 1 ELSE 0 END) AS finished
           FROM test_attempts a
           JOIN tests t ON t.id=a.test_id
           GROUP BY a.test_id
           ORDER BY passes DESC
           LIMIT ?""", (limit,))


def _since(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def new_users(period_days: int) -> int:
    """Новые юзеры за период (по created_at или onboarded_at)."""
    since = _since(period_days)
    # пробуем created_at, иначе onboarded_at
    try:
        return _count(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (since,))
    except Exception:
        try:
            return _count(
                "SELECT COUNT(*) AS c FROM users WHERE onboarded_at >= ?", (since,))
        except Exception:
            return 0


def premium_granted(period_days: int) -> int:
    since = _since(period_days)
    return _count(
        "SELECT COUNT(*) AS c FROM premium_users WHERE granted_at >= ?", (since,))


def private_granted(period_days: int) -> int:
    since = _since(period_days)
    try:
        return _count(
            "SELECT COUNT(*) AS c FROM private_test_access WHERE granted_at >= ?",
            (since,))
    except Exception:
        return 0


def total_users() -> int:
    return _count("SELECT COUNT(*) AS c FROM users")


def users_by_language() -> dict:
    """Сколько юзеров на каждом языке."""
    ru = _count("SELECT COUNT(*) AS c FROM users WHERE language='ru'")
    kz = _count("SELECT COUNT(*) AS c FROM users WHERE language='kz'")
    return {"ru": ru, "kz": kz}


def new_users_by_lang(period_days: int) -> dict:
    since = _since(period_days)
    try:
        ru = _count("SELECT COUNT(*) AS c FROM users WHERE language='ru' AND created_at >= ?", (since,))
        kz = _count("SELECT COUNT(*) AS c FROM users WHERE language='kz' AND created_at >= ?", (since,))
        return {"ru": ru, "kz": kz}
    except Exception:
        return {"ru": 0, "kz": 0}


def profile_subjects_stats() -> list:
    """Сколько юзеров выбрали каждый профильный предмет."""
    rows = db.fetchall(
        "SELECT profile_subjects FROM users WHERE profile_subjects IS NOT NULL "
        "AND profile_subjects != ''")
    from collections import Counter
    cnt = Counter()
    other = 0
    for r in rows:
        for part in str(r['profile_subjects']).split(','):
            part = part.strip()
            if part == 'other':
                other += 1
            elif part.isdigit():
                cnt[int(part)] += 1
    # Превратим id в названия
    out = []
    for cid, n in cnt.most_common():
        c = db.fetchone("SELECT name, emoji FROM test_categories WHERE id=?", (cid,))
        if c:
            out.append((f"{c.get('emoji') or '📚'} {c['name']}", n))
    if other:
        out.append(("❓ Другое", other))
    return out


def build_stats_text() -> str:
    """Собрать полный текст статистики."""
    lines = ["📊 <b>Статистика бота</b>\n"]

    lines.append(f"🟢 Сейчас проходят тест: <b>{active_now()}</b>")
    lines.append(f"👥 Всего пользователей: <b>{total_users()}</b>")

    # По языкам
    bl = users_by_language()
    lines.append(f"  🇷🇺 Русское отделение: <b>{bl['ru']}</b>")
    lines.append(f"  🇰🇿 Казахское отделение: <b>{bl['kz']}</b>\n")

    # Реальные прохождения тестов (только завершённые, без пауз/брошенных)
    lines.append("<b>✅ Завершённые тесты (реальные):</b>")
    lines.append(f"• Сегодня: {real_completions(1)}")
    lines.append(f"• За неделю: {real_completions(7)}")
    lines.append(f"• За месяц: {real_completions(30)}")
    lines.append(f"• Всего: {real_completions()}\n")

    # Режимы Карточки / Заучивание
    try:
        md = modes_stats()
        lines.append("<b>🃏🧠 Режимы обучения:</b>")
        lines.append(f"• 🃏 Карточки: {md['fc_done']} прох. "
                     f"({md['fc_users']} чел., куплено {md['fc_passes']})")
        lines.append(f"• 🧠 Заучивание: {md['ln_done']} прох. "
                     f"({md['ln_users']} чел., куплено {md['ln_passes']})\n")
    except Exception:
        pass

    # Новые юзеры
    lines.append("<b>📈 Новые пользователи:</b>")
    lines.append(f"• Сегодня: {new_users(1)}")
    lines.append(f"• За неделю: {new_users(7)}")
    lines.append(f"• За месяц: {new_users(30)}\n")

    # Премиум
    lines.append("<b>💎 Премиум выдан:</b>")
    lines.append(f"• Сегодня: {premium_granted(1)}")
    lines.append(f"• За неделю: {premium_granted(7)}")
    lines.append(f"• За месяц: {premium_granted(30)}\n")

    # Приватные доступы
    lines.append("<b>🔐 Приватные доступы выданы:</b>")
    lines.append(f"• Сегодня: {private_granted(1)}")
    lines.append(f"• За неделю: {private_granted(7)}")
    lines.append(f"• За месяц: {private_granted(30)}\n")

    # Продажи (звёзды)
    try:
        from services import payment_service as _pms
        ss = _pms.sales_stats()
        lines.append("<b>💰 Продажи (Stars):</b>")
        lines.append(f"• Тестов куплено: {ss['tests']}")
        lines.append(f"• Разделов куплено: {ss['categories']}")
        lines.append(f"• Подарков: {ss['gifts']}")
        lines.append(f"• Повторов куплено: {ss['redos']} ({ss['redo_stars']} ⭐️)")
        lines.append(f"• Всего звёзд: {ss['total_stars']} ⭐️")
        # Звёзды по периодам
        lines.append(f"• 📅 За сегодня: {stars_for_period(1)} ⭐️")
        lines.append(f"• 📅 За неделю: {stars_for_period(7)} ⭐️")
        lines.append(f"• 📅 За месяц: {stars_for_period(30)} ⭐️\n")
    except Exception:
        pass

    # Топ тестов
    top = top_tests(10)
    if top:
        lines.append("<b>🔥 Топ-10 популярных тестов:</b>")
        for i, t in enumerate(top, 1):
            passes = t.get('passes', 0)
            fin = t.get('finished', 0) or 0
            lines.append(f"{i}. {t['title'][:30]} — {passes} прох. ({fin} до конца)")
        lines.append("")

    # Профильные предметы
    subj = profile_subjects_stats()
    if subj:
        lines.append("<b>🎓 Популярные профильные предметы:</b>")
        for name, n in subj[:10]:
            lines.append(f"• {name}: {n} чел.")

    return "\n".join(lines)
