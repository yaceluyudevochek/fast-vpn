"""
Белые списки (БС): отдельный Internal Squad с серверами белых списков и лимит трафика на них.

- Сквад БС задаётся в config.BS_SQUAD_UUID; бот работает только с ним (другие
  сквады, например админский, не трогает и их трафик не считает).
- Трафик считается панелью по всем нодам сквада (серверов БС может быть несколько).
- Лимит — у типа тарифа (tariff_types.bs_limit_gb, NULL = БС не входит в тариф).
  Полный лимит — только тем, кто хоть раз оплатил подписку; остальным (триал) — BS_TRIAL_LIMIT_GB.
- Период — 30 дней от покупки (subscriptions.bs_period_anchor), оплата начинает период заново.
- Лимит исчерпан → сквад БС у пользователя снимается до конца периода, потом возвращается.
- Глобальный выключатель (bot_settings.bs_enabled): выключено — бот никому не выдаёт сквад БС.

bs_state у подписки:
  NULL       — по умолчанию: сквад есть, пока не исчерпан лимит;
  exhausted  — лимит исчерпан в периоде bs_state_period, сквад снят;
  admin_off  — админ отключил БС вручную, бот сам не вернёт;
  admin_on   — админ включил вручную: без лимита до конца периода bs_state_period.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import config
from database import db

logger = logging.getLogger(__name__)

GB = 1024 ** 3
PERIOD_DAYS = 30
MSK = timezone(timedelta(hours=3))
SETTING_ENABLED = "bs_enabled"


def squad_uuid() -> Optional[str]:
    return getattr(config, "BS_SQUAD_UUID", None) or None


def trial_limit_gb() -> float:
    return getattr(config, "BS_TRIAL_LIMIT_GB", 3)


async def is_enabled() -> bool:
    return bool(squad_uuid()) and await db.get_setting(SETTING_ENABLED, "0") == "1"


async def set_enabled(enabled: bool):
    await db.set_setting(SETTING_ENABLED, "1" if enabled else "0")


# ======================================================
# ПЕРИОД И ЛИМИТ
# ======================================================

def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def current_period(anchor: Optional[str], now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Текущий 30-дневный период, отсчитанный от anchor: (начало, момент обновления лимита)"""
    now = now or datetime.now(timezone.utc)
    start = _parse_utc(anchor) or now
    if now > start:
        start += timedelta(days=PERIOD_DAYS * ((now - start) // timedelta(days=PERIOD_DAYS)))
    return start, start + timedelta(days=PERIOD_DAYS)


def period_key(start: datetime) -> str:
    return start.strftime("%Y-%m-%d %H:%M:%S")


def limit_bytes(subscription: Dict[str, Any], has_paid: bool) -> Optional[int]:
    """Лимит БС для подписки в байтах; None — БС подписке не положены"""
    if not has_paid:
        return int(trial_limit_gb() * GB)
    limit_gb = subscription.get("bs_limit_gb")
    return int(limit_gb * GB) if limit_gb else None


def format_gb(value: int) -> str:
    text = f"{value / GB:.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_moment(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


# ======================================================
# СТАТУС ДЛЯ ЭКРАНОВ
# ======================================================

@dataclass
class BsStatus:
    limit: int
    used: Optional[int]          # None — панель не отдала статистику
    reset_at: datetime
    state: Optional[str]

    @property
    def remaining(self) -> Optional[int]:
        return None if self.used is None else max(0, self.limit - self.used)


async def _panel_user_id(panel_api, subscription: Dict[str, Any]) -> Optional[int]:
    if subscription.get("panel_user_id"):
        return subscription["panel_user_id"]
    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    if not user or not user.get("id"):
        return None
    await db.set_panel_user_id(subscription["id"], user["id"])
    return user["id"]


async def get_status(panel_api, subscription: Dict[str, Any]) -> Optional[BsStatus]:
    """Статус БС подписки или None, если БС выключены глобально или не положены подписке"""
    if not await is_enabled():
        return None
    limit = limit_bytes(subscription, await db.user_has_paid(subscription["telegram_id"]))
    if limit is None:
        return None

    start, reset_at = current_period(subscription.get("bs_period_anchor"))
    state = subscription.get("bs_state")
    if state in ("exhausted", "admin_on") and subscription.get("bs_state_period") != period_key(start):
        state = None  # период сменился — фоновая проверка вернёт сквад

    used = None
    user_id = await _panel_user_id(panel_api, subscription)
    if user_id:
        used = await panel_api.get_squad_user_usage_bytes(
            squad_uuid(), user_id, start.date().isoformat(), datetime.now(timezone.utc).date().isoformat()
        )
    return BsStatus(limit=limit, used=used, reset_at=reset_at, state=state)


def status_text(status: BsStatus, emoji: str = "🛡") -> str:
    """Строки для карточки подписки"""
    title = f"{emoji} Белые списки:"
    if status.state == "admin_off":
        return f"{title} <b>отключены</b>\n"
    if status.state == "admin_on":
        return f"{title} <b>без лимита</b> до {format_moment(status.reset_at)}\n"
    if status.state == "exhausted" or (status.used is not None and status.used >= status.limit):
        return (
            f"{title} лимит <b>{format_gb(status.limit)} ГБ</b> исчерпан\n"
            f"   Обычные серверы работают без ограничений. "
            f"Доступ к белым спискам вернётся <b>{format_moment(status.reset_at)}</b>\n"
        )
    if status.used is None:
        return f"{title} лимит <b>{format_gb(status.limit)} ГБ</b> (статистика временно недоступна)\n"
    return (
        f"{title} осталось <b>{format_gb(status.remaining)} из {format_gb(status.limit)} ГБ</b>\n"
        f"   Лимит обновится {format_moment(status.reset_at)}\n"
    )


# ======================================================
# ВЫДАЧА СКВАДА
# ======================================================

async def squads_for_new_user(tariff_type: Dict[str, Any], telegram_id: int) -> List[str]:
    """Дополнительные сквады при создании пользователя в панели: [сквад БС], если положен"""
    if not await is_enabled():
        return []
    if limit_bytes(tariff_type, await db.user_has_paid(telegram_id)) is None:
        return []
    return [squad_uuid()]


async def on_payment(panel_api, telegram_id: int, tariff_type_id: int):
    """После оплаты: период БС начинается заново, сквад возвращается, если был снят за лимит"""
    subscription = await db.get_user_subscription_by_tariff_type(telegram_id, tariff_type_id)
    if not subscription:
        return
    await db.restart_bs_period(subscription["id"])
    if not await is_enabled():
        return
    subscription = await db.get_subscription(subscription["id"])
    if subscription.get("bs_state") == "admin_off" or limit_bytes(subscription, True) is None:
        return
    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    if user:
        squads = [s["uuid"] for s in user.get("activeInternalSquads", [])]
        if squad_uuid() not in squads:
            await panel_api.set_user_squads(subscription["username_in_panel"], squads + [squad_uuid()])


async def admin_set_user_squad(panel_api, subscription: Dict[str, Any], squad: str, enable: bool) -> bool:
    """Админ включает/выключает сквад пользователю. Для сквада БС запоминаем решение админа."""
    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    if not user:
        return False
    squads = [s["uuid"] for s in user.get("activeInternalSquads", [])]
    new_squads = (squads + [squad]) if enable else [s for s in squads if s != squad]
    if not await panel_api.set_user_squads(subscription["username_in_panel"], new_squads):
        return False
    if squad == squad_uuid():
        start, _ = current_period(subscription.get("bs_period_anchor"))
        if enable:
            await db.set_bs_state(subscription["id"], "admin_on", period_key(start))
        else:
            await db.set_bs_state(subscription["id"], "admin_off")
    return True


# ======================================================
# ФОНОВАЯ ПРОВЕРКА
# ======================================================

@dataclass
class CheckResult:
    added: List[int]
    removed: List[int]
    exhausted: List[Dict[str, Any]]   # подписки, у которых только что кончился лимит
    restored: List[Dict[str, Any]]    # подписки, которым вернули сквад после нового периода


_check_lock = asyncio.Lock()


async def check_all(panel_api, now: Optional[datetime] = None) -> Optional[CheckResult]:
    """
    Сверить сквад БС у всех пользователей бота с лимитами и периодами и применить изменения.
    None — проверка не выполнялась (выключено или панель не ответила).
    """
    async with _check_lock:  # фоновая проверка и кнопка «Проверить сейчас» не пересекаются
        return await _check_all(panel_api, now)


async def _check_all(panel_api, now: Optional[datetime] = None) -> Optional[CheckResult]:
    if not await is_enabled():
        return None
    squad = squad_uuid()
    now = now or datetime.now(timezone.utc)

    panel_users = await panel_api.get_all_users()
    if panel_users is None:
        return None
    by_username = {u["username"]: u for u in panel_users}

    subscriptions = await db.get_bs_subscriptions()
    limits = {s["id"]: limit_bytes(s, bool(s["has_paid"])) for s in subscriptions}
    known = [v for v in limits.values() if v]
    if not subscriptions:
        return CheckResult([], [], [], [])

    # Кандидаты на превышение: трафик за последние 31 день не меньше минимального лимита.
    # Текущий 30-дневный период целиком входит в это окно, остальные заведомо в лимите.
    candidates = {}
    if known:
        window_start = (now - timedelta(days=PERIOD_DAYS + 1)).date().isoformat()
        candidates = await panel_api.get_squad_usage(squad, window_start, now.date().isoformat(), min(known))
        if candidates is None:
            return None

    to_add, to_remove, exhausted, restored = [], [], [], []
    for sub in subscriptions:
        user = by_username.get(sub["username_in_panel"])
        if not user:
            continue
        if not sub.get("panel_user_id"):
            await db.set_panel_user_id(sub["id"], user["id"])

        in_squad = squad in {s["uuid"] for s in user.get("activeInternalSquads", [])}
        limit = limits[sub["id"]]
        start, _ = current_period(sub.get("bs_period_anchor"), now)
        key = period_key(start)
        state = sub.get("bs_state")

        # Новый период: исчерпанный лимит и ручное «без лимита» больше не действуют
        if state in ("exhausted", "admin_on") and sub.get("bs_state_period") != key:
            if state == "exhausted":
                restored.append(sub)
            state = None
            await db.set_bs_state(sub["id"], None)

        if state == "admin_off":
            continue

        expire = _parse_utc(user.get("expireAt"))
        if limit is None or not expire or expire <= now:
            # БС не положены или подписка истекла — сквад не выдаём (ручное включение админа не трогаем)
            if in_squad and state != "admin_on" and limit is None:
                to_remove.append(user["id"])
            continue

        over = False
        if state != "admin_on" and user["id"] in candidates:
            used = await panel_api.get_squad_user_usage_bytes(
                squad, user["id"], start.date().isoformat(), now.date().isoformat()
            )
            over = used is not None and used >= limit

        if over:
            if in_squad:
                to_remove.append(user["id"])
            if state != "exhausted":
                await db.set_bs_state(sub["id"], "exhausted", key)
                exhausted.append(sub)
            if sub in restored:
                restored.remove(sub)
        elif state == "exhausted":
            if in_squad:
                to_remove.append(user["id"])
        elif not in_squad:
            to_add.append(user["id"])

    to_add = sorted(set(to_add))
    to_remove = sorted(set(to_remove) - set(to_add))
    if to_add:
        await panel_api.add_users_to_squad(squad, to_add)
    if to_remove:
        await panel_api.remove_users_from_squad(squad, to_remove)
    if to_add or to_remove:
        logger.info(f"🛡 БС: выдан {len(to_add)}, снят {len(to_remove)}, исчерпали лимит {len(exhausted)}")
    return CheckResult(to_add, to_remove, exhausted, restored)


async def disable_for_all(panel_api) -> Optional[int]:
    """Выключить БС глобально и снять сквад у всех, у кого он есть. Возвращает число пользователей."""
    await set_enabled(False)
    squad = squad_uuid()
    panel_users = await panel_api.get_all_users()
    if panel_users is None:
        return None
    ids = [u["id"] for u in panel_users if squad in {s["uuid"] for s in u.get("activeInternalSquads", [])}]
    if ids and not await panel_api.remove_users_from_squad(squad, ids):
        return None
    return len(ids)


async def notify_users(bot, result: CheckResult):
    """Сообщить пользователям, что лимит БС исчерпан / снова доступен"""
    for sub in result.exhausted:
        _, reset_at = current_period(sub.get("bs_period_anchor"))
        limit = limit_bytes(sub, bool(sub["has_paid"])) or 0
        await _send(bot, sub["telegram_id"], (
            f"🛡 <b>Лимит белых списков исчерпан</b>\n\n"
            f"Подписка <b>{sub['tariff_name']}</b>: использовано {format_gb(limit)} ГБ на серверах белых списков.\n"
            f"Обычные серверы продолжают работать без ограничений.\n\n"
            f"Доступ к белым спискам вернётся <b>{format_moment(reset_at)}</b> или сразу после продления подписки."
        ))
    for sub in result.restored:
        await _send(bot, sub["telegram_id"], (
            f"🛡 <b>Белые списки снова доступны</b>\n\n"
            f"Подписка <b>{sub['tariff_name']}</b>: лимит трафика на серверах белых списков обновлён. "
            f"Обнови подписку в приложении, если серверы не появились."
        ))


async def _send(bot, telegram_id: int, text: str):
    try:
        await bot.send_message(telegram_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о БС {telegram_id}: {e}")
    await asyncio.sleep(0.05)
