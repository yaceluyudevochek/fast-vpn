"""
Награда за приглашённых друзей: +REFERRAL_BONUS_DAYS дней к базовой подписке пригласившего
и бонусные ГБ белых списков — только после того, как друг подключился к VPN.

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
from typing import Any, Dict, List, Optional, Set

from config import config
from database import abuse_db, referral_db
from database import db as database
from services import whitelist
from services import tariffs

logger = logging.getLogger(__name__)

REFERRAL_BONUS_DAYS = 7


def days_label_for(subscription: Optional[Dict[str, Any]], days: int) -> str:
    """«+7 дней» на Standart (или без подписки), «+5 дней тарифа Family» на другом тарифе"""
    if subscription and subscription["tariff_type_id"] != tariffs.BASE_TARIFF_ID:
        return f"+{tariffs.plural_days(days)} тарифа {html.escape(subscription['tariff_name'])}"
    return f"+{tariffs.plural_days(days)}"


async def bonus_label(telegram_id: int) -> str:
    """Сколько дней за друга получит этот человек — в днях его тарифа (как и будет начислено)"""
    subscription = await database.get_user_main_subscription(telegram_id)
    return days_label_for(subscription, await tariffs.bonus_days(subscription, REFERRAL_BONUS_DAYS))


def wait_days() -> int:
    return getattr(config, "REFERRAL_WAIT_DAYS", 3)


def _bonus_text(prefix: str = " и ") -> str:
    gb = whitelist.referral_bonus_gb()
    return f"{prefix}<b>+{whitelist.format_gb(int(gb * whitelist.GB))} ГБ</b> к белым спискам" if gb > 0 else ""


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
    bonus = _bonus_text() if await whitelist.is_enabled() else ""
    await _send(bot, referrer_id, (
        f"🎉 <b>У вас новый реферал!</b>\n\n"
        f"Пользователь {html.escape(friend_name or str(friend_id))} активировал пробный период.\n"
        f"🎁 Бонус <b>{await bonus_label(referrer_id)}</b>{bonus} начислится, как только он подключится к VPN."
    ))


# ======================================================
# 2. НАЧИСЛЕНИЕ
# ======================================================

async def grant(bot, panel_api, referrer_id: int, friend_name: str) -> Optional[int]:
    """
    +7 дней (в днях Standart, пересчитанных по коэффициенту в тариф подписки) к подписке пригласившего
    (создаём Standart, если подписки нет) и бонусные ГБ белых списков (фиксированные).
    Возвращает id подписки (None — панель не ответила).
    """
    await database.increment_referral_activated(referrer_id)
    base_username = f"tg{referrer_id}_1"
    referrer_sub = await database.get_user_main_subscription(referrer_id)
    base_id = None
    days_label = days_label_for(None, REFERRAL_BONUS_DAYS)
    if referrer_sub:
        base_id = referrer_sub['id']
        days = await tariffs.bonus_days(referrer_sub, REFERRAL_BONUS_DAYS)
        days_label = days_label_for(referrer_sub, days)
        new_expiry = await panel_api.extend_subscription_by_username(referrer_sub['username_in_panel'], days)
        if new_expiry:
            await database.update_subscription_expiry(base_id, new_expiry)
    else:
        tariff_type = await database.get_tariff_type(1)
        plans = await database.get_tariff_plans(1)
        if not tariff_type or not plans:
            return None
        logger.info(f"Creating base subscription for referrer {referrer_id} with {REFERRAL_BONUS_DAYS} days")
        new_user = await panel_api.create_user(
            username=base_username,
            telegram_id=referrer_id,
            squad_id=tariff_type['squad_id'],
            days=REFERRAL_BONUS_DAYS,
            hwid_limit=tariff_type['hwid_limit'],
            tag=f"U{referrer_id}_T1"[:16],
            extra_squad_ids=await whitelist.squads_for_new_user(tariff_type)
        )
        if new_user:
            base_id = await database.create_subscription(
                referrer_id, 1, plans[0]['id'], new_user.get("shortUuid"), new_user.get("expireAt"),
                base_username, trial=False
            )

    bonus = ""
    if base_id:
        try:
            if await whitelist.grant_referral_bonus(panel_api, base_id) and await whitelist.is_enabled():
                bonus = _bonus_text()
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
    bonus = f" и +{whitelist.format_gb(int(whitelist.referral_bonus_gb() * whitelist.GB))} ГБ" \
        if whitelist.referral_bonus_gb() > 0 else ""
    await _broadcast(bot, (
        f"🧩 <b>Друг подключился с устройства пригласившего</b>\n"
        f"Устройство: {labels}\n\n"
        f"<b>Пригласил:</b>\n{_user_line(referrer_id, infos.get(referrer_id, {}))}\n"
        f"<b>Приглашённый:</b>\n{_user_line(friend_id, infos.get(friend_id, {}))}\n\n"
        f"Бонус начислен: +{REFERRAL_BONUS_DAYS} дней (в днях Standart) к подписке пригласившего{bonus}. "
        f"Если это накрутка — заберите дни: /admin → ➖ Уменьшить дни подписки."
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
