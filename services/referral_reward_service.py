"""
Реферальная программа с наградами.

Логика:
- Каждого приглашённого сохраняем в referral_invites (даже если отписался/удалил бота).
- Реферал засчитывается ТОЛЬКО если приглашённый подписан на обязательный канал.
- За каждые 10 засчитанных уникальных друзей → премиум на N дней.
- Один и тот же invited не засчитывается дважды (UNIQUE в таблице).
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

import config
import database as db

log = logging.getLogger(__name__)

FRIENDS_PER_REWARD = 10        # дефолт (переопределяется настройкой админа)
REWARD_PREMIUM_DAYS = 30       # дефолт (переопределяется настройкой админа)
ALMATY = timezone(timedelta(hours=5))


def _friends_needed() -> int:
    """Сколько друзей нужно (из настроек админа)."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='referral_friends'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    return FRIENDS_PER_REWARD


def _reward_days() -> int:
    """Сколько дней премиума за друзей (из настроек админа)."""
    try:
        row = db.fetchone("SELECT value FROM settings WHERE key='referral_reward_days'")
        if row and row.get('value'):
            return int(row['value'])
    except Exception:
        pass
    return REWARD_PREMIUM_DAYS


def record_invite(inviter_tg_id: int, invited_tg_id: int) -> bool:
    """
    Записать что inviter пригласил invited. Сохраняется навсегда.
    True если это новая запись (не было раньше).
    """
    if inviter_tg_id == invited_tg_id:
        return False
    existing = db.fetchone(
        "SELECT id FROM referral_invites WHERE invited_tg_id=?",
        (invited_tg_id,))
    if existing:
        return False  # этого человека уже кто-то приглашал
    try:
        db.execute(
            "INSERT INTO referral_invites (inviter_tg_id, invited_tg_id) "
            "VALUES (?,?)", (inviter_tg_id, invited_tg_id))
        return True
    except Exception as e:
        log.warning("record_invite: %s", e)
        return False


async def check_and_update_subscription(bot: Bot, invited_tg_id: int) -> bool:
    """
    Проверить подписан ли приглашённый на обязательные каналы.
    Если да — отметить subscribed+counted. Возвращает True если засчитан сейчас.
    """
    inv = db.fetchone(
        "SELECT * FROM referral_invites WHERE invited_tg_id=?", (invited_tg_id,))
    if not inv or inv.get('counted'):
        return False

    from services import subscription_service as ss
    not_sub = await ss.check_all_subscriptions(bot, invited_tg_id)
    if not_sub:
        return False  # не подписан на все каналы

    # Засчитываем
    db.execute(
        "UPDATE referral_invites SET subscribed=1, counted=1 WHERE id=?",
        (inv['id'],))
    return True


def count_referrals(inviter_tg_id: int) -> dict:
    """Статистика рефералов: всего приглашено, засчитано (подписаны)."""
    total = db.fetchone(
        "SELECT COUNT(*) AS c FROM referral_invites WHERE inviter_tg_id=?",
        (inviter_tg_id,))['c']
    counted = db.fetchone(
        "SELECT COUNT(*) AS c FROM referral_invites "
        "WHERE inviter_tg_id=? AND counted=1", (inviter_tg_id,))['c']
    return {'total': total, 'counted': counted}


async def maybe_give_reward(bot: Bot, inviter_tg_id: int) -> int:
    """
    Проверить достиг ли inviter кратного 10 засчитанных друзей.
    Если да и награда ещё не выдавалась — выдать премиум.
    Возвращает сколько наград выдано сейчас (0 или больше).
    """
    stats = count_referrals(inviter_tg_id)
    counted = stats['counted']
    deserved_rewards = counted // _friends_needed()

    user = db.fetchone("SELECT id, referral_rewards_given FROM users WHERE tg_id=?",
                        (inviter_tg_id,))
    if not user:
        return 0
    already = user.get('referral_rewards_given') or 0
    new_rewards = deserved_rewards - already
    if new_rewards <= 0:
        return 0

    # Выдаём премиум за каждую новую награду
    for _ in range(new_rewards):
        _grant_premium(user['id'], _reward_days())
    db.execute("UPDATE users SET referral_rewards_given=? WHERE tg_id=?",
                (deserved_rewards, inviter_tg_id))

    # Уведомляем пригласившего
    try:
        await bot.send_message(
            inviter_tg_id,
            f"🎉 <b>Поздравляем!</b>\n\n"
            f"Ты пригласил {counted} друзей и получаешь "
            f"<b>{_reward_days()} дней Премиума</b>! 🏆\n\n"
            f"Теперь у тебя доступ ко всем тестам и режимам.\n"
            f"Приглашай ещё друзей — за каждые 10 новых снова премиум!",
            parse_mode="HTML")
    except Exception:
        pass
    return new_rewards


def _grant_premium(user_id: int, days: int):
    """Выдать/продлить премиум пользователю."""
    now = datetime.now(ALMATY)
    existing = db.fetchone(
        "SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,))
    if existing and existing.get('expires_at'):
        try:
            cur_exp = datetime.fromisoformat(existing['expires_at'].replace('Z', '+00:00'))
            if cur_exp.tzinfo is None:
                cur_exp = cur_exp.replace(tzinfo=timezone.utc)
            base = max(now, cur_exp.astimezone(ALMATY))
        except Exception:
            base = now
    else:
        base = now
    new_exp = (base + timedelta(days=days)).astimezone(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO premium_users (user_id, expires_at, notified_expired)
           VALUES (?,?,0)
           ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,
                                              notified_expired=0""",
        (user_id, new_exp))


def get_referral_link(bot_username: str, inviter_tg_id: int) -> str:
    """Реферальная ссылка пользователя."""
    return f"https://t.me/{bot_username}?start=ref_{inviter_tg_id}"
