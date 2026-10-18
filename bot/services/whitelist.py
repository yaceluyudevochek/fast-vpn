"""
Белые списки (БС): отдельный Internal Squad с серверами белых списков и лимит трафика на них.

- Сквад БС задаётся в config.BS_SQUAD_UUID; бот работает только с ним (другие
  сквады, например админский, не трогает и их трафик не считает).
- Трафик считается панелью по всем нодам сквада (серверов БС может быть несколько).
- Лимит — у типа тарифа (tariff_types.bs_limit_gb, NULL = БС не входит в тариф).

Квота зависит от того, оплачены ли текущие дни подписки (subscriptions.bs_paid_until):
- оплаченные дни (оплата или дни от админа) — полный лимит тарифа, обновляется
  каждые 30 дней от начала оплаченного срока (subscriptions.bs_period_anchor);
  досрочное продление только добавляет дни, лимит обновляется по расписанию;
- неоплаченные дни (триал, бонусы за рефералов, дни после окончания оплаченного
  срока) — разовый пул BS_TRIAL_LIMIT_GB на всё время, пополняется только оплатой.

Лимит исчерпан → сквад БС у пользователя снимается до обновления квоты, потом возвращается.
Глобальный выключатель (bot_settings.bs_enabled): выключено — бот никому не выдаёт сквад БС.

bs_state у подписки (bs_state_period — ключ квоты, к которой относится состояние):
  NULL       — по умолчанию: сквад есть, пока не исчерпан лимит;
  exhausted  — лимит исчерпан, сквад снят до смены квоты;
  admin_off  — админ отключил БС вручную, бот сам не вернёт;
  admin_on   — админ включил вручную: без лимита до смены квоты.
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
WARN_SHARE = 0.8   # предупреждаем, когда потрачено 80% лимита (осталось 20%)
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
# КВОТА
# ======================================================

def _parse_utc(value) -> Optional[datetime]:
    if not value:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def current_period(anchor, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Текущий 30-дневный период, отсчитанный от anchor: (начало, момент обновления лимита)"""
    now = now or datetime.now(timezone.utc)
    start = _parse_utc(anchor) or now
    if now > start:
        start += timedelta(days=PERIOD_DAYS * ((now - start) // timedelta(days=PERIOD_DAYS)))
    return start, start + timedelta(days=PERIOD_DAYS)


def _key(prefix: str, moment: datetime) -> str:
    return f"{prefix}:{moment.strftime('%Y-%m-%d %H:%M:%S')}"


@dataclass
class Quota:
    limit: Optional[int]           # байты; None — БС подписке не положены
    paid: bool                     # оплаченные дни (полный лимит) или разовый пул
    start: Optional[datetime]      # с какого момента считается трафик
    key: str                       # меняется, когда квота обновляется
    refresh_at: Optional[datetime] = None  # оплаченные дни: когда лимит обновится
    paid_until: Optional[datetime] = None

    @property
    def refresh_is_full(self) -> bool:
        """Обновится ли лимит до полного (а не закончатся оплаченные дни раньше)"""
        return bool(self.paid and self.refresh_at and self.paid_until and self.refresh_at <= self.paid_until)


def get_quota(subscription: Dict[str, Any], now: Optional[datetime] = None) -> Quota:
    now = now or datetime.now(timezone.utc)
    limit_gb = subscription.get("bs_limit_gb")
    if not limit_gb:
        return Quota(limit=None, paid=False, start=None, key="none")
    full = int(limit_gb * GB)

    paid_until = _parse_utc(subscription.get("bs_paid_until"))
    if paid_until and now < paid_until:
        start, refresh_at = current_period(subscription.get("bs_period_anchor"), now)
        return Quota(full, True, start, _key("paid", start), refresh_at, paid_until)

    # Неоплаченные дни: разовый пул с конца оплаченного срока (или с начала триала)
    start = paid_until or _parse_utc(subscription.get("bs_period_anchor")) \
        or _parse_utc(subscription.get("created_at")) or now
    return Quota(min(full, int(trial_limit_gb() * GB)), False, start, _key("free", start), None, paid_until)


def format_gb(value: int) -> str:
    text = f"{value / GB:.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_moment(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def _day(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).date().isoformat()


# ======================================================
# СТАТУС ДЛЯ ЭКРАНОВ
# ======================================================

@dataclass
class BsStatus:
    quota: Quota
    used: Optional[int]          # None — панель не отдала статистику
    state: Optional[str]

    @property
    def limit(self) -> int:
        return self.quota.limit

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


def _effective_state(subscription: Dict[str, Any], quota: Quota) -> Optional[str]:
    state = subscription.get("bs_state")
    if state in ("exhausted", "admin_on") and subscription.get("bs_state_period") != quota.key:
        return None  # квота обновилась — состояние больше не действует
    return state


async def get_status(panel_api, subscription: Dict[str, Any]) -> Optional[BsStatus]:
    """Статус БС подписки или None, если БС выключены глобально или не положены подписке"""
    if not await is_enabled():
        return None
    quota = get_quota(subscription)
    if quota.limit is None:
        return None

    used = None
    user_id = await _panel_user_id(panel_api, subscription)
    if user_id:
        used = await panel_api.get_squad_user_usage_bytes(
            squad_uuid(), user_id, _day(quota.start), _day(datetime.now(timezone.utc))
        )
    return BsStatus(quota=quota, used=used, state=_effective_state(subscription, quota))


def _when_back(quota: Quota) -> str:
    if quota.refresh_is_full:
        return f"вернётся <b>{format_moment(quota.refresh_at)}</b>"
    if quota.paid:
        # Оплаченные дни кончатся раньше, чем обновится лимит
        return (f"вернётся <b>{format_moment(quota.refresh_at)}</b>, если продлить подписку "
                f"до {format_moment(quota.paid_until)}")
    return "вернётся после оплаты подписки"


def status_text(status: BsStatus, emoji: str = "🛡") -> str:
    """Строки для карточки подписки"""
    quota = status.quota
    title = f"{emoji} Белые списки:"
    if status.state == "admin_off":
        return f"{title} <b>отключены</b>\n"
    if status.state == "admin_on":
        return f"{title} <b>без лимита</b>\n"
    if status.state == "exhausted" or (status.used is not None and status.used >= status.limit):
        return (
            f"{title} лимит <b>{format_gb(status.limit)} ГБ</b> исчерпан\n"
            f"   Обычные серверы работают без ограничений. Доступ к белым спискам {_when_back(quota)}\n"
        )

    if status.used is None:
        line = f"{title} лимит <b>{format_gb(status.limit)} ГБ</b> (статистика временно недоступна)\n"
    else:
        line = f"{title} осталось <b>{format_gb(status.remaining)} из {format_gb(status.limit)} ГБ</b>\n"

    if quota.refresh_is_full:
        return line + f"   Лимит обновится {format_moment(quota.refresh_at)}\n"
    if quota.paid:
        return line + (
            f"   Полный лимит действует по оплаченным дням (до {format_moment(quota.paid_until)}), "
            f"дальше — {format_gb(int(trial_limit_gb() * GB))} ГБ до продления\n"
        )
    return line + "   Без оплаты лимит не пополняется — полный лимит каждые 30 дней после оплаты подписки\n"


# ======================================================
# ВЫДАЧА СКВАДА
# ======================================================

async def squads_for_new_user(tariff_type: Dict[str, Any]) -> List[str]:
    """Дополнительные сквады при создании пользователя в панели: [сквад БС], если БС входят в тариф"""
    if not await is_enabled() or not tariff_type or not tariff_type.get("bs_limit_gb"):
        return []
    return [squad_uuid()]


async def _ensure_squad(panel_api, subscription_id: int):
    """Сразу вернуть сквад БС, если он положен (не дожидаясь фоновой проверки)"""
    if not await is_enabled():
        return
    subscription = await db.get_subscription(subscription_id)
    if not subscription:
        return
    quota = get_quota(subscription)
    state = _effective_state(subscription, quota)
    if quota.limit is None or state in ("admin_off", "exhausted"):
        return  # лимит в текущей квоте исчерпан — досрочная оплата его не обновляет
    if subscription.get("bs_state") == "exhausted":
        await db.set_bs_state(subscription_id, None)  # квота сменилась (первая оплата) — лимит снова есть
    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    if user:
        squads = [s["uuid"] for s in user.get("activeInternalSquads", [])]
        if squad_uuid() not in squads:
            await panel_api.set_user_squads(subscription["username_in_panel"], squads + [squad_uuid()])


async def on_payment(panel_api, telegram_id: int, plan: Dict[str, Any]):
    """Оплата: дни плана становятся оплаченными (досрочное продление лимит не обновляет)"""
    subscription = await db.get_user_subscription_by_tariff_type(telegram_id, plan["tariff_type_id"])
    if not subscription:
        return
    await db.add_bs_paid_days(
        subscription["id"], plan["days"], datetime.now(timezone.utc),
        from_now=bool(plan.get("traffic_limit_bytes")),  # лимитный тариф: срок ставится от текущего момента
    )
    await _ensure_squad(panel_api, subscription["id"])


async def on_admin_days(panel_api, subscription_id: int, days: int):
    """Админ добавил дни — это дни полной подписки (с полным лимитом БС)"""
    await db.add_bs_paid_days(subscription_id, days, datetime.now(timezone.utc))
    await _ensure_squad(panel_api, subscription_id)


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
        if enable:
            await db.set_bs_state(subscription["id"], "admin_on", get_quota(subscription).key)
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
    restored: List[Dict[str, Any]]    # подписки, которым вернули сквад после обновления квоты
    warned: List[Tuple[Dict[str, Any], int]] = None  # (подписка, потрачено байт): осталось меньше 20%

    def __post_init__(self):
        self.warned = self.warned or []


_check_lock = asyncio.Lock()


async def check_all(panel_api, now: Optional[datetime] = None) -> Optional[CheckResult]:
    """
    Сверить сквад БС у всех пользователей бота с квотами и применить изменения.
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
    quotas = {s["id"]: get_quota(s, now) for s in subscriptions}
    counted = [q for q in quotas.values() if q.limit]
    if not subscriptions:
        return CheckResult([], [], [], [], [])

    # Кандидаты: трафик с самого раннего начала квоты не меньше 80% минимального лимита.
    # Окно покрывает квоту любого пользователя, поэтому остальные заведомо в лимите.
    candidates = {}
    if counted:
        window_start = min(q.start for q in counted)
        candidates = await panel_api.get_squad_usage(
            squad, _day(window_start), _day(now), int(min(q.limit for q in counted) * WARN_SHARE)
        )
        if candidates is None:
            return None

    to_add, to_remove, exhausted, restored, warned = [], [], [], [], []
    for sub in subscriptions:
        user = by_username.get(sub["username_in_panel"])
        if not user:
            continue
        if not sub.get("panel_user_id"):
            await db.set_panel_user_id(sub["id"], user["id"])

        in_squad = squad in {s["uuid"] for s in user.get("activeInternalSquads", [])}
        quota = quotas[sub["id"]]
        state = sub.get("bs_state")

        # Квота обновилась (новые 30 дней, оплата, конец оплаченных дней) — прежнее состояние не действует
        if state in ("exhausted", "admin_on") and sub.get("bs_state_period") != quota.key:
            if state == "exhausted":
                restored.append(sub)
            state = None
            await db.set_bs_state(sub["id"], None)

        if state == "admin_off":
            continue

        expire = _parse_utc(user.get("expireAt"))
        if quota.limit is None or not expire or expire <= now:
            # БС не входят в тариф — снимаем; истёкшую подписку не трогаем (ручное включение админа тоже)
            if in_squad and state != "admin_on" and quota.limit is None:
                to_remove.append(user["id"])
            if sub in restored:
                restored.remove(sub)
            continue

        over = False
        if state != "admin_on" and user["id"] in candidates:
            used = await panel_api.get_squad_user_usage_bytes(squad, user["id"], _day(quota.start), _day(now))
            over = used is not None and used >= quota.limit
            if (not over and used is not None and used >= quota.limit * WARN_SHARE
                    and sub.get("bs_warned_period") != quota.key and state != "exhausted"):
                await db.set_bs_warned(sub["id"], quota.key)
                warned.append((sub, used))

        if over:
            if in_squad:
                to_remove.append(user["id"])
            if state != "exhausted":
                await db.set_bs_state(sub["id"], "exhausted", quota.key)
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
    return CheckResult(to_add, to_remove, exhausted, restored, warned)


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
        quota = get_quota(sub)
        await _send(bot, sub["telegram_id"], (
            f"🛡 <b>Лимит белых списков исчерпан</b>\n\n"
            f"Подписка <b>{sub['tariff_name']}</b>: использовано {format_gb(quota.limit or 0)} ГБ "
            f"на серверах белых списков.\n"
            f"Обычные серверы продолжают работать без ограничений.\n\n"
            f"Доступ к белым спискам {_when_back(quota)}."
        ))
    for sub, used in result.warned:
        quota = get_quota(sub)
        left = format_gb(max(0, quota.limit - used))
        if quota.refresh_is_full:
            tail = f"Лимит обновится <b>{format_moment(quota.refresh_at)}</b>."
        elif quota.paid:
            tail = (f"Лимит обновится <b>{format_moment(quota.refresh_at)}</b>, если продлить подписку "
                    f"до {format_moment(quota.paid_until)}.")
        else:
            full = sub.get("bs_limit_gb")
            tail = f"Оплати подписку — и получишь <b>{full} ГБ</b> белых списков каждые 30 дней."
        await _send(bot, sub["telegram_id"], (
            f"🛡 <b>Белые списки: осталось {left} из {format_gb(quota.limit)} ГБ</b>\n\n"
            f"Подписка <b>{sub['tariff_name']}</b>. Когда лимит закончится, серверы белых списков "
            f"отключатся, обычные серверы продолжат работать без ограничений.\n\n{tail}"
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
