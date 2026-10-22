"""
Одна подписка на аккаунт и смена тарифа без потери оставшихся дней.

Формула: новый срок = остаток × коэффициент текущего тарифа / коэффициент нового.
Коэффициент — поле тарифа в админке (switch_coef), по умолчанию цена 30-дневного плана.
Остаток считается с точностью до секунды (а не до дней), чтобы сменой тарифа туда-обратно
нельзя было накрутить дни на округлении; человеку показываем округлённые дни.

- Оплаченные и бесплатные (бонусные) дни пересчитываются по отдельности, оплаченный срок
  белых списков (bs_paid_until) сдвигается соответственно; период и потраченный трафик белых
  списков не обнуляются — меняется только лимит (берётся у нового тарифа).
- В панели меняется сквад тарифа и лимит устройств; остальные сквады (белые списки, админские)
  сохраняются. При уменьшении лимита устройств привязанные устройства сбрасываются.
- Бесплатная смена тарифа — не чаще раза в SWITCH_COOLDOWN; при покупке плана другого тарифа
  смена происходит сама и без ограничения.
- Бонусные дни (за друга, подарки) заданы в днях базового тарифа (Standart) и пересчитываются
  по коэффициенту в тариф подписки.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from database import db

logger = logging.getLogger(__name__)

BASE_TARIFF_ID = 1
SWITCH_COOLDOWN = timedelta(hours=24)
DAY = timedelta(days=1)


def _utc(value) -> Optional[datetime]:
    if not value:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sql_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def days_text(delta: timedelta) -> str:
    days = delta.total_seconds() / 86400
    if days <= 0:
        return "0 дней"
    if days < 1:
        return "меньше дня"
    n = int(days + 0.5)
    word = "день" if n % 10 == 1 and n % 100 != 11 else ("дня" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "дней")
    return f"{n} {word}"


async def ratio(old_type_id: int, new_type_id: int) -> Optional[float]:
    c_old = await db.get_tariff_coefficient(old_type_id)
    c_new = await db.get_tariff_coefficient(new_type_id)
    if not c_old or not c_new:
        return None
    return c_old / c_new


@dataclass
class SwitchPreview:
    old_type: Dict[str, Any]
    new_type: Dict[str, Any]
    remaining_old: timedelta
    remaining_new: timedelta
    paid_new: timedelta          # из них оплаченных (с полным лимитом белых списков)

    @property
    def devices_reset(self) -> bool:
        return int(self.new_type["hwid_limit"]) < int(self.old_type["hwid_limit"])


async def preview(subscription: Dict[str, Any], new_type_id: int, now: Optional[datetime] = None) -> Optional[SwitchPreview]:
    """Что будет при смене тарифа. None — тариф не найден или у тарифов нет коэффициента."""
    now = now or datetime.now(timezone.utc)
    old_type = await db.get_tariff_type(subscription["tariff_type_id"])
    new_type = await db.get_tariff_type(new_type_id)
    r = await ratio(subscription["tariff_type_id"], new_type_id) if old_type and new_type else None
    if r is None:
        return None
    expiry = _utc(subscription.get("subscription_expiry")) or now
    remaining = max(timedelta(0), expiry - now)
    paid_until = _utc(subscription.get("bs_paid_until"))
    paid = min(remaining, max(timedelta(0), paid_until - now)) if paid_until else timedelta(0)
    return SwitchPreview(old_type, new_type, remaining, remaining * r, paid * r)


def can_switch(subscription: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Бесплатная смена тарифа — не чаще раза в сутки"""
    now = now or datetime.now(timezone.utc)
    changed = _utc(subscription.get("tariff_changed_at"))
    if changed and now - changed < SWITCH_COOLDOWN:
        left = SWITCH_COOLDOWN - (now - changed)
        hours, minutes = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
        return False, f"⏳ Сменить тариф снова можно через {hours} ч. {minutes} мин."
    return True, ""


async def switch(panel_api, subscription_id: int, new_type_id: int, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Перевести подписку на другой тариф с пересчётом остатка. Возвращает обновлённую подписку
    (None — панель не приняла изменение или тариф недоступен). Ограничение «раз в сутки» не проверяет.
    """
    now = now or datetime.now(timezone.utc)
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return None
    if subscription["tariff_type_id"] == new_type_id:
        return subscription
    pv = await preview(subscription, new_type_id, now)
    plans = await db.get_tariff_plans(new_type_id)
    if pv is None or not plans:
        logger.error(f"Смена тарифа {subscription_id} → {new_type_id}: нет коэффициента или планов")
        return None

    old_expiry = _utc(subscription["subscription_expiry"]) or now
    new_expiry = now + pv.remaining_new if pv.remaining_old > timedelta(0) else old_expiry
    paid_until = _utc(subscription.get("bs_paid_until"))
    if paid_until and paid_until > now:
        new_paid_until = sql_ts(now + pv.paid_new)
    else:
        new_paid_until = subscription.get("bs_paid_until")   # оплаченных дней нет — оставляем как было

    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    if not user:
        return None
    old_squad, new_squad = pv.old_type["squad_id"], pv.new_type["squad_id"]
    squads = [s["uuid"] for s in user.get("activeInternalSquads", []) if s["uuid"] != old_squad]
    if new_squad not in squads:
        squads.insert(0, new_squad)
    fields = {"activeInternalSquads": squads, "hwidDeviceLimit": int(pv.new_type["hwid_limit"]),
              "trafficLimitBytes": int(plans[0]["traffic_limit_bytes"] or 0)}
    if pv.remaining_old > timedelta(0):
        fields["expireAt"] = iso_z(new_expiry)
    if not await panel_api.update_user(subscription["username_in_panel"], **fields):
        return None
    if pv.devices_reset:
        await panel_api.reset_user_hwid_devices_by_username(subscription["username_in_panel"])

    await db.change_subscription_tariff(subscription_id, new_type_id, plans[0]["id"], iso_z(new_expiry), new_paid_until)
    logger.info(f"🔄 Подписка {subscription_id}: тариф {pv.old_type['name']} → {pv.new_type['name']}, "
                f"остаток {pv.remaining_old} → {pv.remaining_new}")
    return await db.get_subscription(subscription_id)


async def bonus_days(subscription: Optional[Dict[str, Any]], days: float) -> float:
    """Бонусные дни (заданы в днях базового тарифа) в днях тарифа подписки"""
    if not subscription or subscription["tariff_type_id"] == BASE_TARIFF_ID:
        return days
    r = await ratio(BASE_TARIFF_ID, subscription["tariff_type_id"])
    return days * r if r else days
