"""
Триал по устройству: один пробный период на устройство, а не на Telegram-аккаунт.

Раз в REFERRAL_CHECK_INTERVAL проверяем свежие триалы (не старше CHECK_WINDOW, владелец ни разу
не платил): если триальщик подключился с устройства (HWID), на котором РАНЬШЕ был триал другого
Telegram-аккаунта, — триал завершается, человеку предлагаем оформить подписку, админам — оповещение.
Такой человек не получает подарок «триал закончился», а пригласивший его — награду за друга.
Не блокируем, если другой аккаунт — админ (тесты) или в игноре (семья).
"""
import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import abuse_db
from database import db
from services import whitelist

logger = logging.getLogger(__name__)

CHECK_WINDOW = timedelta(days=4)


def enabled() -> bool:
    return getattr(config, "TRIAL_BY_DEVICE", True)


async def _candidates(now: datetime) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT s.* FROM subscriptions s
            WHERE s.trial_used = 1 AND s.trial_device_checked IS NULL AND s.trial_blocked_at IS NULL
              AND s.created_at >= ?
              AND NOT EXISTS (SELECT 1 FROM payment_transactions p
                              WHERE p.telegram_id = s.telegram_id AND p.status = 'CONFIRMED')
        """, ((now - CHECK_WINDOW).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def _first_trial_at(telegram_ids) -> Dict[int, str]:
    """Когда каждый из аккаунтов взял триал (с учётом архива подписок)"""
    ids = list(telegram_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    result: Dict[int, str] = {}
    async with aiosqlite.connect(db.DB_PATH) as conn:
        for table in ("subscriptions", "subscriptions_archive"):
            async with conn.execute(
                    f"SELECT telegram_id, MIN(created_at) FROM {table} WHERE trial_used = 1 AND telegram_id IN ({marks}) "
                    f"GROUP BY telegram_id", ids) as cursor:
                for tid, created in await cursor.fetchall():
                    if created and (tid not in result or created < result[tid]):
                        result[tid] = created
    return result


async def _mark(subscription_id: int, blocked: bool):
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            f"UPDATE subscriptions SET trial_device_checked = 1"
            f"{', trial_blocked_at = CURRENT_TIMESTAMP' if blocked else ''} WHERE id = ?", (subscription_id,))
        await conn.commit()


async def _block(bot, panel_api, sub: Dict[str, Any], others: Set[int], now: datetime) -> bool:
    from services.abuse import _broadcast, _user_line
    from services.tariffs import iso_z

    expiry = iso_z(now)
    if not await panel_api.update_user(sub["username_in_panel"], expireAt=expiry):
        return False
    await db.update_subscription_expiry(sub["id"], expiry)
    await db.update_subscription_status(sub["id"], "EXPIRED")
    await _mark(sub["id"], blocked=True)
    # Все дни подписки сгорели — ГБ белых списков, начисленные за них, тоже
    await db.take_bs_bonus(sub["id"], whitelist.bonus_left(await db.get_subscription(sub["id"]) or sub))

    try:
        await bot.send_message(
            sub["telegram_id"],
            "ℹ️ <b>Пробный период на этом устройстве уже был использован</b>\n\n"
            "Бесплатный пробный период доступен один раз на устройство. Чтобы продолжить пользоваться "
            "VPN, оформи подписку",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Оформить подписку", callback_data="show_tariffs")]
            ])
        )
    except Exception as e:
        logger.warning(f"Не удалось сообщить о блокировке триала {sub['telegram_id']}: {e}")

    infos = await abuse_db.users_info({sub["telegram_id"], *others})
    lines = "\n".join(_user_line(t, infos.get(t, {})) for t in sorted(others))
    await _broadcast(bot, (
        f"🚫 <b>Повторный триал на том же устройстве — триал завершён</b>\n\n"
        f"<b>Новый аккаунт:</b>\n{_user_line(sub['telegram_id'], infos.get(sub['telegram_id'], {}))}\n"
        f"<b>Триал на этом устройстве раньше брал:</b>\n{lines}\n\n"
        f"Подписка <code>{html.escape(sub['username_in_panel'])}</code> завершена. Если это ошибка — "
        f"добавьте дни через /admin → ➕ Добавить дни подписки."
    ))
    logger.info(f"🚫 Триал {sub['telegram_id']} заблокирован: устройство уже было в триале у {sorted(others)}")
    return True


async def check(bot, panel_api, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or datetime.now(timezone.utc)
    result = {"blocked": 0, "checked": 0}
    if not enabled():
        return result
    candidates = await _candidates(now)
    if not candidates:
        return result

    from services.abuse import telegram_id_of
    panel_users = await panel_api.get_all_users()
    devices = await panel_api.get_all_hwid_devices()
    if panel_users is None or devices is None:
        return result
    tg_by_panel_id = {u["id"]: telegram_id_of(u) for u in panel_users}
    hwids_of: Dict[int, Set[str]] = defaultdict(set)
    owners_of: Dict[str, Set[int]] = defaultdict(set)
    for device in devices:
        tid = tg_by_panel_id.get(device.get("userId"))
        if tid:
            hwids_of[tid].add(device["hwid"])
            owners_of[device["hwid"]].add(tid)
    trusted = await abuse_db.admin_ids() | await abuse_db.ignored()

    for sub in candidates:
        tid = sub["telegram_id"]
        my_hwids = hwids_of.get(tid)
        if not my_hwids:
            expiry = sub.get("subscription_expiry")
            if expiry and datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= now:
                await _mark(sub["id"], blocked=False)    # триал закончился, так и не подключившись
            continue
        others = {o for h in my_hwids for o in owners_of[h] if o != tid}
        if others & trusted:
            await _mark(sub["id"], blocked=False)
            result["checked"] += 1
            continue
        trial_at = await _first_trial_at(others | {tid})
        mine = trial_at.get(tid) or str(sub.get("created_at"))
        earlier = {o for o in others if o in trial_at and trial_at[o] < mine}
        if earlier and await _block(bot, panel_api, sub, earlier, now):
            result["blocked"] += 1
        else:
            await _mark(sub["id"], blocked=False)
            result["checked"] += 1
    if result["blocked"]:
        logger.info(f"🚫 Триал по устройству: {result}")
    return result
