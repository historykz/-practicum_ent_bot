"""
Реферальная система v2.

Логика:
- Реферал создаётся при переходе по deep-link, но НЕ засчитывается
  пока приглашённый не подпишется на обязательный канал.
- Засчитываются только verified рефералы.
- Порог для доступа к платному тесту: 10 verified друзей.
- Один и тот же пользователь не может быть приглашён дважды (UNIQUE).
"""
import logging
from typing import Optional

from aiogram import Bot

import database as db
from utils import get_user_by_tg, now_iso

logger = logging.getLogger(__name__)

REFERRALS_FOR_PAID_TEST = 10


def register_referral(inviter_tg_id: int, invited_tg_id: int) -> Optional[str]:
    """Регистрирует переход по реф-ссылке (без верификации)."""
    if inviter_tg_id == invited_tg_id:
        return None
    inviter = get_user_by_tg(inviter_tg_id)
    invited = get_user_by_tg(invited_tg_id)
    if not inviter or not invited:
        return None
    existing = db.fetchone(
        "SELECT id FROM referrals WHERE invited_id=?", (invited["id"],)
    )
    if existing:
        return None
    db.execute(
        "INSERT INTO referrals (inviter_id, invited_id, verified) VALUES (?,?,0)",
        (inviter["id"], invited["id"]),
    )
    db.execute("UPDATE users SET invited_by=? WHERE id=?",
                (inviter["id"], invited["id"]))
    return "registered"


async def verify_referral(bot: Bot, invited_tg_id: int) -> bool:
    """Проверяет подписку на глобальный канал. Если ОК — отмечает verified."""
    invited = get_user_by_tg(invited_tg_id)
    if not invited:
        return False
    row = db.fetchone(
        "SELECT id, inviter_id, verified FROM referrals WHERE invited_id=?",
        (invited["id"],))
    if not row or row["verified"]:
        return False

    ch = db.fetchone(
        "SELECT channel_username FROM required_channels WHERE is_global=1 LIMIT 1"
    )
    if not ch:
        db.execute(
            "UPDATE referrals SET verified=1, verified_at=? WHERE id=?",
            (now_iso(), row["id"]))
        _maybe_grant_milestone(row["inviter_id"])
        return True

    from services.subscription_service import check_user_subscription
    ok = await check_user_subscription(bot, ch["channel_username"], invited_tg_id)
    if not ok:
        return False

    db.execute(
        "UPDATE referrals SET verified=1, verified_at=? WHERE id=?",
        (now_iso(), row["id"]))
    _maybe_grant_milestone(row["inviter_id"])
    return True


def _maybe_grant_milestone(inviter_id: int) -> None:
    if count_verified_referrals(inviter_id) >= REFERRALS_FOR_PAID_TEST:
        db.execute(
            "INSERT OR IGNORE INTO user_achievements (user_id, code) VALUES (?,?)",
            (inviter_id, "ref_10"),
        )


def count_referrals(user_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM referrals WHERE inviter_id=?", (user_id,))
    return row["c"] if row else 0


def count_verified_referrals(user_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM referrals WHERE inviter_id=? AND verified=1",
        (user_id,))
    return row["c"] if row else 0


def user_can_unlock_paid_test(user_id: int) -> bool:
    return count_verified_referrals(user_id) >= REFERRALS_FOR_PAID_TEST
