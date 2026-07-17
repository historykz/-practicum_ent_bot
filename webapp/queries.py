"""
Read-only данные для сайта. Никогда ничего не пишет в базу —
только читает то, что уже посчитано существующими сервисами бота.

Каждый блок обёрнут в try/except: если в боевой базе не хватает
какой-то таблицы (старые/неполные миграции), кабинет всё равно
откроется — просто с нулями/пустым списком в этом блоке, вместо 500.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import database as db
import utils
from services import rating_service, referral_reward_service, share_service

logger = logging.getLogger(__name__)

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


def _safe_history(user_id: int) -> list[dict]:
    try:
        rows = db.fetchall(
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
    except Exception:
        logger.exception("webapp: не удалось получить историю тестов user_id=%s", user_id)
        return []
    result = []
    for row in rows:
        row = dict(row)
        row["created_at_fmt"] = _format_dt(row["created_at"])
        result.append(row)
    return result


def _safe_tests_done(user_id: int) -> int:
    try:
        return db.fetchone(
            "SELECT COUNT(*) AS c FROM test_attempts WHERE user_id=? AND status='finished'",
            (user_id,),
        )["c"]
    except Exception:
        logger.exception("webapp: не удалось посчитать тесты user_id=%s", user_id)
        return 0


def _safe_referral(tg_id: int) -> dict:
    try:
        ref_stats = referral_reward_service.count_referrals(tg_id)
        friends_needed = referral_reward_service._friends_needed()
        reward_days = referral_reward_service._reward_days()
        counted = ref_stats["counted"]
        mod = counted % friends_needed if friends_needed else 0
        remaining = friends_needed - mod if mod != 0 else friends_needed
        return {
            "total": ref_stats["total"],
            "counted": counted,
            "needed": friends_needed,
            "remaining_for_reward": remaining,
            "reward_days": reward_days,
            "link": share_service.build_ref_link(tg_id, config.WEB_BOT_USERNAME),
        }
    except Exception:
        logger.exception("webapp: не удалось получить рефералов tg_id=%s", tg_id)
        return {
            "total": 0, "counted": 0, "needed": 10, "remaining_for_reward": 10,
            "reward_days": 30,
            "link": share_service.build_ref_link(tg_id, config.WEB_BOT_USERNAME),
        }


def _safe_rating(user_id: int) -> dict:
    try:
        position, total_score = rating_service.user_overall_position(user_id)
        return {"position": position, "total_score": total_score}
    except Exception:
        logger.exception("webapp: не удалось получить рейтинг user_id=%s", user_id)
        return {"position": 0, "total_score": 0}


def _dashboard_data_sync(tg_id: int) -> Optional[dict]:
    user = utils.get_user_by_tg(tg_id)
    if not user:
        return None

    user_id = user["id"]

    premium_info = utils.get_premium_info(user_id)
    is_premium = utils.is_premium(user_id)
    raw_expires_at = premium_info["expires_at"] if premium_info else None

    return {
        "user": user,
        "is_premium": is_premium,
        "premium_expires_at": raw_expires_at,
        "premium_expires_at_fmt": _format_dt(raw_expires_at),
        "premium_lifetime": is_premium and premium_info is not None and not raw_expires_at,
        "tests_done": _safe_tests_done(user_id),
        "history": _safe_history(user_id),
        "referral": _safe_referral(tg_id),
        "rating": _safe_rating(user_id),
    }


async def get_dashboard_data(tg_id: int) -> Optional[dict]:
    """Собирает все данные для личного кабинета одним вызовом в отдельном потоке."""
    return await asyncio.to_thread(_dashboard_data_sync, tg_id)

