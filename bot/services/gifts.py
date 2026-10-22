"""
Подарок дней всем получателям рассылки.

Админ задаёт N дней Standart. Каждому начисляется по общим правилам бонусных дней:
- дни пересчитываются в тариф его подписки по коэффициенту и округляются вверх
  (10 дней Standart → 7 дней Family → 4 дня Corporate при 200 / 300 / 500);
- ГБ белых списков — за эти дни по лимиту тарифа (лимит / 30 × дни);
- истёкшая подписка продлевается от текущего момента и снова работает;
- у кого подписки не было — создаётся Standart на N дней.

Каждое начисление сначала записывается в mailing_gifts (рассылка + пользователь), поэтому
повторное нажатие «Отправить» или перезапуск одной и той же рассылки второй раз не начислит.
"""
import html
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiosqlite

from database import db
from services import tariffs, whitelist

logger = logging.getLogger(__name__)

MAX_DAYS = 365


@dataclass
class Gift:
    days: int                  # в днях тарифа подписки
    bs_bytes: int
    tariff_type_id: int
    tariff_name: str
    created: bool = False      # подписки не было — создана Standart


async def _claim(mailing_id: str, telegram_id: int) -> bool:
    """Занять начисление (False — этому пользователю в этой рассылке уже начисляли)"""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO mailing_gifts (mailing_id, telegram_id) VALUES (?, ?)", (mailing_id, telegram_id))
        await conn.commit()
        return cursor.rowcount == 1


async def _finish(mailing_id: str, telegram_id: int, gift: Optional["Gift"], subscription_id: Optional[int]):
    async with aiosqlite.connect(db.DB_PATH) as conn:
        if gift is None:  # не получилось — освобождаем, чтобы можно было повторить
            await conn.execute("DELETE FROM mailing_gifts WHERE mailing_id = ? AND telegram_id = ?", (mailing_id, telegram_id))
        else:
            await conn.execute(
                "UPDATE mailing_gifts SET status = 'granted', subscription_id = ?, days = ?, bs_bytes = ? "
                "WHERE mailing_id = ? AND telegram_id = ?",
                (subscription_id, gift.days, gift.bs_bytes, mailing_id, telegram_id))
        await conn.commit()


async def create_base_subscription(panel_api, telegram_id: int, days: int) -> Optional[int]:
    """Подписка Standart на days дней для того, у кого подписки нет. Возвращает её id (None — панель не ответила)."""
    tariff_type = await db.get_tariff_type(tariffs.BASE_TARIFF_ID)
    plans = await db.get_tariff_plans(tariffs.BASE_TARIFF_ID)
    if not tariff_type or not plans:
        return None
    username = f"tg{telegram_id}_{tariffs.BASE_TARIFF_ID}"
    new_user = await panel_api.create_user(
        username=username,
        telegram_id=telegram_id,
        squad_id=tariff_type['squad_id'],
        days=days,
        hwid_limit=tariff_type['hwid_limit'],
        tag=f"U{telegram_id}_T{tariffs.BASE_TARIFF_ID}"[:16],
        extra_squad_ids=await whitelist.squads_for_new_user(tariff_type)
    )
    if not new_user:
        return None
    return await db.create_subscription(
        telegram_id, tariffs.BASE_TARIFF_ID, plans[0]['id'], new_user.get("shortUuid"), new_user.get("expireAt"),
        username, trial=False
    )


async def grant(panel_api, mailing_id: str, telegram_id: int, base_days: int) -> Optional[Gift]:
    """Начислить подарок одному пользователю. None — уже начислено в этой рассылке или панель не ответила."""
    if not await _claim(mailing_id, telegram_id):
        return None
    gift, subscription_id = None, None
    try:
        subscription = await db.get_user_main_subscription(telegram_id)
        if subscription:
            subscription_id = subscription['id']
            days = await tariffs.bonus_days(subscription, base_days)
            new_expiry = await panel_api.extend_subscription_by_username(subscription['username_in_panel'], days)
            if new_expiry:
                await db.update_subscription_expiry(subscription_id, new_expiry)
                await db.update_subscription_status(subscription_id, "ACTIVE")
                gift = Gift(days, 0, subscription['tariff_type_id'], subscription['tariff_name'])
        else:
            subscription_id = await create_base_subscription(panel_api, telegram_id, base_days)
            if subscription_id:
                created = await db.get_subscription(subscription_id)
                gift = Gift(base_days, 0, tariffs.BASE_TARIFF_ID, created['tariff_name'] if created else "", created=True)
        if gift:
            try:
                gift.bs_bytes = await whitelist.grant_days(panel_api, subscription_id, gift.days)
            except Exception as e:
                logger.error(f"Подарок в рассылке: не удалось начислить ГБ БС {telegram_id}: {e}")
    except Exception as e:
        logger.error(f"Подарок в рассылке: ошибка для {telegram_id}: {e}", exc_info=True)
        gift = None
    await _finish(mailing_id, telegram_id, gift, subscription_id)
    if gift is None:
        logger.error(f"Подарок в рассылке {mailing_id}: не удалось начислить {telegram_id}")
    return gift


async def gift_line(gift: Gift) -> str:
    """«🎁 Тебе начислено +7 дней тарифа Family и +14 ГБ белых списков» — у каждого своя"""
    days = f"+{tariffs.plural_days(gift.days)}"
    if gift.tariff_type_id != tariffs.BASE_TARIFF_ID:
        days += f" тарифа {html.escape(gift.tariff_name)}"
    text = f"🎁 Тебе начислено <b>{days}</b> подписки"
    if gift.bs_bytes > 0 and await whitelist.is_enabled():
        text += f" и <b>+{whitelist.format_gb(gift.bs_bytes)} ГБ</b> белых списков"
    if gift.created:
        text += ". Подписка уже ждёт тебя в разделе «Подписка»"
    return text


async def examples(base_days: int) -> Dict[str, Any]:
    """Сколько получит подписка каждого активного тарифа — для предпросмотра админу"""
    result = {}
    for tariff_type in await db.get_all_tariff_types():
        fake = {"tariff_type_id": tariff_type['id']}
        days = await tariffs.bonus_days(fake, base_days)
        result[tariff_type['name']] = (days, whitelist.bytes_for_days(tariff_type.get('bs_limit_gb'), days))
    return result
