"""
Read-only данные для сайта. Никогда ничего не пишет в базу —
только читает то, что уже посчитано существующими сервисами бота.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import database as db
import utils
from services import rating_service, referral_reward_service, share_service

ALMATY = timezone(timedelta(hours=5))


def _format_dt(iso_str: Optional[str]) -> Optional[str]:
    """Хранимые в базе даты — наивный UTC ISO. Показываем по Астане (UTC+5)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    dt = dt.replace(tzinfo=timezone.utc).astimezone(ALMATY)
    return dt.strftime("%d.%m.%Y %H:%M")


def _dashboard_data_sync(tg_id: int) -> Optional[dict]:
    user = utils.get_user_by_tg(tg_id)
    if not user:
        return None

    user_id = user["id"]

    premium_info = utils.get_premium_info(user_id)
    is_premium = utils.is_premium(user_id)

    history = db.fetchall(
        """
        SELECT t.title AS title, ta.score AS score,
               ta.correct_answers AS correct, ta.wrong_answers AS wrong,
               ta.skipped_answers AS skipped, ta.created_at AS created_at
        FROM test_attempts ta
        JOIN tests t ON t.id = ta.test_id
        WHERE ta.user_id = ? AND ta.status = 'finished'
        ORDER BY ta.created_at DESC
        LIMIT 20
        """,
        (user_id,),
    )
    tests_done = db.fetchone(
        "SELECT COUNT(*) AS c FROM test_attempts WHERE user_id=? AND status='finished'",
        (user_id,),
    )["c"]

    ref_stats = referral_reward_service.count_referrals(tg_id)
    friends_needed = referral_reward_service._friends_needed()
    reward_days = referral_reward_service._reward_days()
    ref_link = share_service.build_ref_link(tg_id, config.WEB_BOT_USERNAME)

    counted = ref_stats["counted"]
    mod = counted % friends_needed if friends_needed else 0
    remaining_for_reward = friends_needed - mod if mod != 0 else friends_needed

    position, total_score = rating_service.user_overall_position(user_id)

    history_rows = []
    for row in history:
        row = dict(row)
        row["created_at_fmt"] = _format_dt(row["created_at"])
        history_rows.append(row)

    raw_expires_at = premium_info["expires_at"] if premium_info else None

    return {
        "user": user,
        "is_premium": is_premium,
        "premium_expires_at": raw_expires_at,
        "premium_expires_at_fmt": _format_dt(raw_expires_at),
        "premium_lifetime": is_premium and premium_info is not None and not raw_expires_at,
        "tests_done": tests_done,
        "history": history_rows,
        "referral": {
            "total": ref_stats["total"],
            "counted": counted,
            "needed": friends_needed,
            "remaining_for_reward": remaining_for_reward,
            "reward_days": reward_days,
            "link": ref_link,
        },
        "rating": {
            "position": position,
            "total_score": total_score,
        },
    }


async def get_dashboard_data(tg_id: int) -> Optional[dict]:
    """Собирает все данные для личного кабинета одним вызовом в отдельном потоке."""
    return await asyncio.to_thread(_dashboard_data_sync, tg_id)
