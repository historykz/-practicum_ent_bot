"""
Реферальная система.
"""
import logging
from typing import Optional

import database as db
from utils import (
    get_user_by_id, get_user_by_tg, grant_paid_access, grant_premium, now_iso,
)

logger = logging.getLogger(__name__)


def register_referral(inviter_tg_id: int, invited_tg_id: int) -> Optional[str]:
    """
    Регистрирует реферал.
    Возвращает текст начисленного бонуса или None.
    """
    if inviter_tg_id == invited_tg_id:
        return None
    inviter = get_user_by_tg(inviter_tg_id)
    invited = get_user_by_tg(invited_tg_id)
    if not inviter or not invited:
        return None
    # Проверяем, не был ли уже зарегистрирован
    existing = db.fetchone(
        "SELECT id FROM referrals WHERE invited_id=?", (invited["id"],)
    )
    if existing:
        return None
    db.execute(
        "INSERT INTO referrals (inviter_id, invited_id) VALUES (?,?)",
        (inviter["id"], invited["id"]),
    )
    db.execute("UPDATE users SET invited_by=? WHERE id=?", (inviter["id"], invited["id"]))

    # Считаем количество приглашённых пригласившим
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM referrals WHERE inviter_id=?", (inviter["id"],)
    )
    count = row["c"] if row else 0
    bonus = None
    # Простые пороги: 3 -> один платный тест (бонус-флаг), 10 -> 7 дней Premium
    if count == 10:
        grant_premium(inviter["id"], days=7, admin_tg_id=0)
        bonus = "Premium на 7 дней"
    elif count == 3:
        # Открываем доступ к любому платному тесту - храним отметку
        db.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?,?)",
            (inviter["id"], "ref_3"),
        )
        bonus = "доступ к одному платному тесту"
    elif count == 1:
        db.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?,?)",
            (inviter["id"], "ref_1"),
        )
        bonus = "первый друг!"
    return bonus


def count_referrals(user_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM referrals WHERE inviter_id=?", (user_id,)
    )
    return row["c"] if row else 0
