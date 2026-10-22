"""
Награда за приглашённых друзей: +REFERRAL_BONUS_DAYS дней к подписке пригласившего
и ГБ белых списков за эти дни (лимит тарифа / 30 × дни) — только после того, как друг подключился к VPN.

1. Друг активировал триал → награда в ожидании (referral_rewards, status = pending),
   пригласившему сообщаем, что бонус придёт после подключения друга.
2. Фоновая проверка (раз в REFERRAL_CHECK_INTERVAL): «подключился» = у подписки друга в панели
   появилось устройство (HWID — приложение передаёт его при первом получении подписки).
   Тогда начисляем награду. Если устройство друга совпало с устройством пригласившего —
   награду всё равно начисляем, но сообщаем админам (дни можно забрать: /admin → Уменьшить дни).
3. Не подключился за REFERRAL_WAIT_DAYS (3) дня — награда сгорает.
"""
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from config import config
from database import abuse_db, referral_db
from database import db as database
from services import gifts
from services import whitelist
from services import tariffs

logger = logging.getLogger(__name__)

REFERRAL_BONUS_DAYS = 7


def days_label_for(subscription: Optional[Dict[str, Any]], days: int) -> str:
    """«+7 дней» на Standart (или без подписки), «+5 дней тарифа Family» на другом тарифе"""
    if subscription and subscription["tariff_type_id"] != tariffs.BASE_TARIFF_ID:
        return f"+{tariffs.plural_days(days)} тарифа {html.escape(subscription['tariff_name'])}"
    return f"+{tariffs.plural_days(days)}"


async def reward_for(subscription: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """Награда за друга: (дни тарифа подписки, байты белых списков за эти дни). Без подписки — Standart."""
    days = await tariffs.bonus_days(subscription, REFERRAL_BONUS_DAYS)
    if subscription:
        limit = subscription.get("bs_limit_gb")
    else:
        limit = (await database.get_tariff_type(tariffs.BASE_TARIFF_ID) or {}).get("bs_limit_gb")
    return days, whitelist.bytes_for_days(limit, days)


async def _gb_text(amount: int, prefix: str = " и ") -> str:
    if amount <= 0 or not await whitelist.is_enabled():
        return ""
    return f"{prefix}<b>+{whitelist.format_gb(amount)} ГБ</b> к белым спискам"


async def bonus_text(telegram_id: int) -> str:
    """«<b>+7 дней</b> к подписке и <b>+7 ГБ</b> к белым спискам» — в днях тарифа человека, как и будет начислено"""
    subscription = await database.get_user_main_subscription(telegram_id)
    days, amount = await reward_for(subscription)
    return f"<b>{days_label_for(subscription, days)}</b> к подписке" + await _gb_text(amount)


def wait_days() -> int:
    return getattr(config, "REFERRAL_WAIT_DAYS", 3)


async def _send(bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение о реферале {chat_id}: {e}")


# ======================================================
# 1. ДРУГ АКТИВИРОВАЛ ТРИАЛ — НАГРАДА В ОЖИДАНИИ
# ======================================================

async def queue_reward(bot, friend_id: int, friend_name: str):
    user = await database.get_user(friend_id)
    if not user or not user["referred_by"]:
        return
    referrer_id = user["referred_by"]
    if not await referral_db.queue(referrer_id, friend_id, friend_name):
        return
    await _send(bot, referrer_id, (
        f"🎉 <b>У вас новый реферал!</b>\n\n"
        f"Пользователь {html.escape(friend_name or str(friend_id))} активировал пробный период.\n"
        f"🎁 Бонус {await bonus_text(referrer_id)} начислится, как только он подключится к VPN."
    ))


# ======================================================
# 2. НАЧИСЛЕНИЕ
# ======================================================

async def grant(bot, panel_api, referrer_id: int, friend_name: str) -> Optional[int]:
    """
    +7 дней (в днях Standart, пересчитанных по коэффициенту в тариф подписки) к подписке пригласившего
    (создаём Standart, если подписки нет) и ГБ белых списков за эти дни.
    Возвращает id подписки (None — панель не ответила).
    """
    referrer_sub = await database.get_user_main_subscription(referrer_id)
    base_id = None
    days = REFERRAL_BONUS_DAYS
    days_label = days_label_for(None, days)
    if referrer_sub:
        base_id = referrer_sub['id']
        days = await tariffs.bonus_days(referrer_sub, REFERRAL_BONUS_DAYS)
        days_label = days_label_for(referrer_sub, days)
        new_expiry = await panel_api.extend_subscription_by_username(referrer_sub['username_in_panel'], days)
        if new_expiry:
            await database.update_subscription_expiry(base_id, new_expiry)
    else:
        logger.info(f"Creating base subscription for referrer {referrer_id} with {REFERRAL_BONUS_DAYS} days")
        base_id = await gifts.create_base_subscription(panel_api, referrer_id, REFERRAL_BONUS_DAYS)
        if base_id is None:
            return None  # панель не ответила — повторим при следующей проверке
    await database.increment_referral_activated(referrer_id)

    bonus = ""
    if base_id:
        try:
            bonus = await _gb_text(await whitelist.grant_days(panel_api, base_id, days))
        except Exception as e:
            logger.error(f"Failed to grant whitelist bonus to referrer {referrer_id}: {e}")

    await _send(bot, referrer_id, (
        f"🎁 <b>Ваш друг подключился к VPN!</b>\n\n"
        f"{html.escape(friend_name or 'Друг')} начал пользоваться VPN.\n"
        f"Вам начислено <b>{days_label}</b> подписки{bonus}!"
    ))
    return base_id


# ======================================================
# 3. ФОНОВАЯ ПРОВЕРКА
# ======================================================

async def _devices_of(panel_api, telegram_id: int) -> Optional[List[Dict[str, Any]]]:
    """Все HWID-устройства всех подписок пользователя в панели. None — панель не ответила."""
    devices = []
    for sub in await database.get_user_subscriptions(telegram_id):
        user = await panel_api.get_user_by_username(sub["username_in_panel"])
        if not user:
            continue
        data = await panel_api.get_user_hwid_devices(user["id"])
        if data is None:
            return None
        devices.extend(data.get("devices", []))
    return devices


def _utc(value) -> Optional[datetime]:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _alert_shared_device(bot, reward: Dict[str, Any], devices: List[Dict[str, Any]]):
    from services.abuse import _broadcast, _device_label, _user_line

    referrer_id, friend_id = reward["referrer_id"], reward["friend_id"]
    infos = await abuse_db.users_info([referrer_id, friend_id])
    labels = ", ".join(sorted({_device_label(d) for d in devices}))
    await _broadcast(bot, (
        f"🧩 <b>Друг подключился с устройства пригласившего</b>\n"
        f"Устройство: {labels}\n\n"
        f"<b>Пригласил:</b>\n{_user_line(referrer_id, infos.get(referrer_id, {}))}\n"
        f"<b>Приглашённый:</b>\n{_user_line(friend_id, infos.get(friend_id, {}))}\n\n"
        f"Бонус начислен: +{REFERRAL_BONUS_DAYS} дней (в днях Standart) к подписке пригласившего "
        f"и ГБ белых списков за эти дни. "
        f"Если это накрутка — заберите дни (ГБ за них заберутся тоже): /admin → ➖ Уменьшить дни подписки."
    ))


async def process_pending(bot, panel_api, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or datetime.now(timezone.utc)
    result = {"granted": 0, "shared_device": 0, "expired": 0}
    skip = await abuse_db.admin_ids() | await abuse_db.ignored()
    for reward in await referral_db.pending():
        friend_sub = await database.get_user_main_subscription(reward["friend_id"])
        if friend_sub and friend_sub.get("trial_blocked_at"):
            await referral_db.set_status(reward["id"], "expired", "повторный триал на том же устройстве")
            result["expired"] += 1
            continue
        friend_devices = await _devices_of(panel_api, reward["friend_id"])
        if friend_devices is None:
            continue  # панель не ответила — проверим в следующий раз
        if not friend_devices:
            created = _utc(reward["created_at"])
            if created and now - created > timedelta(days=wait_days()):
                await referral_db.set_status(reward["id"], "expired", "друг не подключился")
                result["expired"] += 1
            continue

        referrer_devices = await _devices_of(panel_api, reward["referrer_id"]) or []
        shared: Set[str] = {d["hwid"] for d in friend_devices} & {d["hwid"] for d in referrer_devices}

        # Сначала отмечаем, потом начисляем: дубль награды хуже, чем пропуск при сбое панели
        note = "общее устройство с пригласившим" if shared else None
        await referral_db.set_status(reward["id"], "granted", note)
        if await grant(bot, panel_api, reward["referrer_id"], reward["friend_name"]) is None:
            await referral_db.set_status(reward["id"], "pending")  # панель не ответила — повторим
            continue
        result["granted"] += 1
        if shared and reward["referrer_id"] not in skip and reward["friend_id"] not in skip:
            await _alert_shared_device(bot, reward, [d for d in friend_devices if d["hwid"] in shared])
            result["shared_device"] += 1
    if any(result.values()):
        logger.info(f"🎁 Награды за друзей: {result}")
    return result
