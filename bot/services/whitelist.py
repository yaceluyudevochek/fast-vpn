"""
Белые списки (БС): отдельный Internal Squad с серверами белых списков и лимит трафика на них.

- Сквад БС задаётся в config.BS_SQUAD_UUID; бот работает только с ним (другие
  сквады, например админский, не трогает и их трафик не считает).
- Трафик считается панелью по всем нодам сквада (серверов БС может быть несколько).
- Лимит — у типа тарифа (tariff_types.bs_limit_gb, NULL = БС не входит в тариф).

Единое правило: каждый день подписки даёт лимит тарифа / 30 ГБ (Standart 50 ГБ → 1,67 ГБ в день).
- Оплаченные дни (subscriptions.bs_paid_until) — полный лимит тарифа на каждые 30 дней от начала
  оплаченного срока (subscriptions.bs_period_anchor): за 30 дней те же лимит / 30 × 30, но лимит
  обновляется по периодам, а не выдаётся сразу за весь срок. Досрочное продление только добавляет
  дни, лимит обновляется по расписанию.
- Любые другие добавленные дни (триал, админ, бонусы за друзей, подарки) сразу начисляют
  бонусные ГБ: дни × лимит тарифа / 30 (grant_days). Своей квоты у неоплаченных дней нет —
  на них работают только бонусные ГБ.

Бонусные ГБ — личный запас поверх квоты: тратятся, когда обычный лимит квоты израсходован,
и не сгорают. Учёт: bs_bonus_bytes —
начислено, bs_bonus_used — потрачено в закрытых квотах. Когда квота сменяется, трафик сверх
обычного лимита в закрывшейся квоте списывается из бонусов (bs_quota_* — отслеживаемая квота).

Лимит (квота + бонусы) исчерпан → сквад БС снимается до обновления квоты или новых бонусов.
Глобальный выключатель (bot_settings.bs_enabled): выключено — бот никому не выдаёт сквад БС.

bs_state у подписки (bs_state_period — ключ квоты, к которой относится состояние):
  NULL       — по умолчанию: сквад есть, пока не исчерпан лимит;
  exhausted  — лимит исчерпан, сквад снят до смены квоты или новых бонусов;
  admin_off  — админ отключил БС вручную, бот сам не вернёт;
  admin_on   — админ включил вручную: без лимита до смены квоты (бонусы не тратятся).
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


def bytes_for_days(limit_gb, days: float) -> int:
    """Сколько байт белых списков дают дни подписки: дни × лимит тарифа / 30"""
    if not limit_gb or days <= 0:
        return 0
    return int(limit_gb * GB * days / PERIOD_DAYS)


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


def _sql_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Quota:
    limit: Optional[int]           # обычный лимит квоты, байты; None — БС подписке не положены
    paid: bool                     # оплаченные дни (полный лимит) или неоплаченные (лимит 0, только бонусы)
    start: Optional[datetime]      # с какого момента считается трафик
    key: str                       # меняется, когда квота обновляется
    refresh_at: Optional[datetime] = None  # оплаченные дни: когда лимит обновится
    paid_until: Optional[datetime] = None
    bonus: int = 0                 # остаток бонусных байт (тратится сверх обычного лимита)
    upcoming_limit: Optional[int] = None   # больший лимит нового тарифа — со следующего периода

    @property
    def total(self) -> Optional[int]:
        """Всего доступно в этой квоте: обычный лимит + бонусы"""
        return None if self.limit is None else self.limit + self.bonus

    @property
    def refresh_is_full(self) -> bool:
        """Обновится ли лимит до полного (а не закончатся оплаченные дни раньше)"""
        return bool(self.paid and self.refresh_at and self.paid_until and self.refresh_at <= self.paid_until)


def bonus_left(subscription: Dict[str, Any]) -> int:
    return max(0, int(subscription.get("bs_bonus_bytes") or 0) - int(subscription.get("bs_bonus_used") or 0))


def get_quota(subscription: Dict[str, Any], now: Optional[datetime] = None) -> Quota:
    now = now or datetime.now(timezone.utc)
    limit_gb = subscription.get("bs_limit_gb")
    if not limit_gb:
        return Quota(limit=None, paid=False, start=None, key="none")
    full = int(limit_gb * GB)
    bonus = bonus_left(subscription)

    paid_until = _parse_utc(subscription.get("bs_paid_until"))
    if paid_until and now < paid_until:
        start, refresh_at = current_period(subscription.get("bs_period_anchor"), now)
        key = _key("paid", start)
        # Тариф повысили посреди периода: больший лимит — только со следующего периода, иначе можно
        # «съездить» на день на дорогой тариф ради его месячного лимита белых списков
        upcoming = None
        recorded = subscription.get("bs_quota_limit")
        if subscription.get("bs_quota_key") == key and recorded is not None and int(recorded) < full:
            upcoming, full = full, int(recorded)
        return Quota(full, True, start, key, refresh_at, paid_until, bonus, upcoming)

    # Неоплаченные дни: своего лимита нет, тратятся бонусные ГБ (начислены за эти дни).
    # Трафик считается с конца оплаченного срока (или с начала подписки)
    start = paid_until or _parse_utc(subscription.get("bs_period_anchor")) \
        or _parse_utc(subscription.get("created_at")) or now
    return Quota(0, False, start, _key("free", start), None, paid_until, bonus)


def format_gb(value: int) -> str:
    text = f"{value / GB:.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_moment(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def _day(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).date().isoformat()


async def _sync_quota(panel_api, subscription: Dict[str, Any], quota: Quota, panel_user_id: int) -> Optional[Quota]:
    """
    Квота сменилась с прошлой проверки: списать из бонусов трафик сверх обычного лимита
    в закрывшейся квоте и запомнить новую. Возвращает квоту с актуальным остатком бонусов,
    None — панель не ответила (повторим при следующей проверке).
    """
    if quota.limit is None or subscription.get("bs_quota_key") == quota.key:
        return quota

    spent = 0
    old_key, old_start, old_limit = (subscription.get("bs_quota_key"), _parse_utc(subscription.get("bs_quota_start")),
                                     subscription.get("bs_quota_limit"))
    unlimited = subscription.get("bs_state") == "admin_on" and subscription.get("bs_state_period") == old_key
    if old_key and old_start and old_limit is not None and not unlimited and bonus_left(subscription) > 0:
        # Трафик считается по дням: день начала новой квоты относится к ней
        last_day = quota.start - timedelta(days=1)
        if last_day.date() >= old_start.date():
            used = await panel_api.get_squad_user_usage_bytes(squad_uuid(), panel_user_id, _day(old_start), _day(last_day))
            if used is None:
                return None
            spent = min(bonus_left(subscription), max(0, used - int(old_limit)))

    await db.close_bs_quota(subscription["id"], spent, quota.key, _sql_ts(quota.start), quota.limit)
    subscription["bs_bonus_used"] = int(subscription.get("bs_bonus_used") or 0) + spent
    subscription["bs_quota_key"], subscription["bs_quota_limit"] = quota.key, quota.limit
    if spent:
        logger.info(f"🛡 БС: подписка {subscription['id']} потратила {format_gb(spent)} ГБ бонусов в закрытой квоте {old_key}")
    quota.bonus = bonus_left(subscription)
    return quota


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
        return None if self.used is None else max(0, self.quota.total - self.used)


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
        quota = await _sync_quota(panel_api, subscription, quota, user_id) or quota
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


def _bonus_hint() -> str:
    return " или раньше — с бонусными днями (например, за приглашённого друга)"


def status_text(status: BsStatus, emoji: str = "🛡") -> str:
    """Строки для карточки подписки"""
    quota = status.quota
    title = f"{emoji} Белые списки:"
    if status.state == "admin_off":
        return f"{title} <b>отключены</b>\n"
    if status.state == "admin_on":
        return f"{title} <b>без лимита</b>\n"
    if not quota.total and status.state != "exhausted":
        return (f"{title} <b>0 ГБ</b>\n"
                f"   ГБ начисляются за дни подписки: после оплаты — полный лимит каждые 30 дней\n")
    if status.state == "exhausted" or (status.used is not None and status.used >= quota.total):
        if quota.limit:
            what = f"лимит <b>{format_gb(quota.limit)} ГБ</b>" + (" и бонусные ГБ" if quota.bonus else "")
        else:
            what = "бонусные ГБ"
        return (
            f"{title} {what} исчерпаны\n"
            f"   Обычные серверы работают без ограничений. Доступ к белым спискам {_when_back(quota)}"
            f"{_bonus_hint()}\n"
        )

    if not quota.limit:
        left = quota.bonus if status.used is None else status.remaining
        line = f"{title} осталось <b>{format_gb(left)} из {format_gb(quota.bonus)} ГБ</b>" + (
            " (статистика временно недоступна)" if status.used is None else "") + "\n"
    elif status.used is None:
        line = f"{title} лимит <b>{format_gb(quota.limit)} ГБ</b> (статистика временно недоступна)\n"
        if quota.bonus:
            line += f"   + бонусные <b>{format_gb(quota.bonus)} ГБ</b> (не сгорают)\n"
    elif status.used < quota.limit:
        line = f"{title} осталось <b>{format_gb(quota.limit - status.used)} из {format_gb(quota.limit)} ГБ</b>\n"
        if quota.bonus:
            line += f"   + бонусные <b>{format_gb(quota.bonus)} ГБ</b> (не сгорают)\n"
    else:
        line = (
            f"{title} основной лимит {format_gb(quota.limit)} ГБ израсходован, идут бонусные: "
            f"осталось <b>{format_gb(status.remaining)} из {format_gb(quota.bonus)} ГБ</b>\n"
        )

    if quota.upcoming_limit:
        line += (f"   Лимит нового тарифа {format_gb(quota.upcoming_limit)} ГБ начнёт действовать "
                 f"{format_moment(quota.refresh_at)}\n")
    if quota.refresh_is_full:
        return line + f"   Лимит обновится {format_moment(quota.refresh_at)}\n"
    if quota.paid:
        return line + (
            f"   Полный лимит действует по оплаченным дням (до {format_moment(quota.paid_until)}), "
            f"дальше — только бонусные ГБ до продления\n"
        )
    return line + "   ГБ начислены за дни подписки и не сгорают. После оплаты — полный лимит каждые 30 дней\n"


# ======================================================
# ВЫДАЧА СКВАДА
# ======================================================

async def squads_for_new_user(tariff_type: Dict[str, Any]) -> List[str]:
    """Дополнительные сквады при создании пользователя в панели: [сквад БС], если БС входят в тариф"""
    if not await is_enabled() or not tariff_type or not tariff_type.get("bs_limit_gb"):
        return []
    return [squad_uuid()]


async def _add_squad(panel_api, subscription: Dict[str, Any]):
    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    if user:
        squads = [s["uuid"] for s in user.get("activeInternalSquads", [])]
        if squad_uuid() not in squads:
            await panel_api.set_user_squads(subscription["username_in_panel"], squads + [squad_uuid()])


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
    user_id = await _panel_user_id(panel_api, subscription)
    if user_id:
        await _sync_quota(panel_api, subscription, quota, user_id)
    if subscription.get("bs_state") == "exhausted":
        await db.set_bs_state(subscription_id, None)  # квота сменилась (первая оплата) — лимит снова есть
    await _add_squad(panel_api, subscription)


async def on_payment(panel_api, telegram_id: int, plan: Dict[str, Any]):
    """Оплата: дни плана становятся оплаченными (досрочное продление лимит не обновляет)"""
    subscription = await db.get_user_main_subscription(telegram_id)
    if not subscription:
        return
    await db.add_bs_paid_days(
        subscription["id"], plan["days"], datetime.now(timezone.utc),
        from_now=bool(plan.get("traffic_limit_bytes")),  # лимитный тариф: срок ставится от текущего момента
    )
    await _ensure_squad(panel_api, subscription["id"])


async def grant_days(panel_api, subscription_id: int, days: float) -> int:
    """
    К подписке добавлены неоплаченные дни (триал, админ, бонусы, подарки) — начислить за них
    бонусные ГБ по лимиту её тарифа. Возвращает начисленные байты.
    """
    subscription = await db.get_subscription(subscription_id)
    amount = bytes_for_days(subscription and subscription.get("bs_limit_gb"), days)
    if amount:
        await grant_bonus(panel_api, subscription_id, amount)
    return amount


async def on_admin_days(panel_api, subscription_id: int, days: int) -> int:
    """Админ добавил дни — ГБ за них по общему правилу"""
    return await grant_days(panel_api, subscription_id, days)


async def on_admin_reduce(subscription: Dict[str, Any], days: int, now: Optional[datetime] = None):
    """
    Админ уменьшил дни (subscription — данные до уменьшения). Дни снимаются с конца срока:
    сначала неоплаченные (забираем начисленные за них бонусные ГБ, сколько осталось),
    потом оплаченные (сокращаем оплаченный срок).
    """
    now = now or datetime.now(timezone.utc)
    expiry = _parse_utc(subscription.get("subscription_expiry")) or now
    paid_until = _parse_utc(subscription.get("bs_paid_until"))
    unpaid_tail = max(0.0, (expiry - max(now, paid_until or now)).total_seconds() / 86400)
    from_unpaid = min(float(days), unpaid_tail)
    amount = bytes_for_days(subscription.get("bs_limit_gb"), from_unpaid)
    if amount:
        await db.take_bs_bonus(subscription["id"], amount)
    from_paid = days - from_unpaid
    if from_paid > 0 and paid_until and paid_until > now:
        await db.reduce_bs_paid_days(subscription["id"], from_paid)


async def grant_bonus(panel_api, subscription_id: int, amount: int):
    """
    Начислить бонусные байты. Сквад БС выдаётся сразу (не дожидаясь фоновой проверки): тем, у кого
    его не было (неоплаченные дни без ГБ), и тем, у кого лимит был исчерпан, а с бонусом снова есть запас.
    """
    await db.add_bs_bonus(subscription_id, amount)
    if not await is_enabled():
        return
    try:
        await _apply_bonus(panel_api, subscription_id)
    except Exception as e:  # ГБ уже начислены — сквад выдаст фоновая проверка
        logger.error(f"🛡 БС: не удалось сразу выдать сквад подписке {subscription_id}: {e}")


async def _apply_bonus(panel_api, subscription_id: int):
    subscription = await db.get_subscription(subscription_id)
    quota = get_quota(subscription)
    state = _effective_state(subscription, quota)
    if quota.limit is None or state == "admin_off":
        return
    if state != "exhausted":
        await _ensure_squad(panel_api, subscription_id)
        return
    user_id = await _panel_user_id(panel_api, subscription)
    if not user_id:
        return
    quota = await _sync_quota(panel_api, subscription, quota, user_id)
    used = await panel_api.get_squad_user_usage_bytes(
        squad_uuid(), user_id, _day(quota.start), _day(datetime.now(timezone.utc))
    ) if quota else None
    if used is not None and used < quota.total:
        await db.set_bs_state(subscription_id, None)
        await _add_squad(panel_api, subscription)


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
    restored: List[Dict[str, Any]]    # подписки, которым вернули сквад (новая квота или бонусы)
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
    counted = [q for q in quotas.values() if q.total]
    if not subscriptions:
        return CheckResult([], [], [], [], [])

    # Кандидаты: трафик с самого раннего начала квоты не меньше 80% минимального доступного объёма
    # (лимит + бонусы). Окно покрывает квоту любого пользователя, поэтому остальные заведомо в лимите.
    # Подписки с нулевым объёмом (неоплаченные дни без бонусов) в порог не входят — им сквад не положен.
    candidates = {}
    if counted:
        window_start = min(q.start for q in counted)
        candidates = await panel_api.get_squad_usage(
            squad, _day(window_start), _day(now), int(min(q.total for q in counted) * WARN_SHARE)
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

        # Квота сменилась — списываем бонусы, потраченные в закрывшейся квоте
        if quota.limit is not None:
            synced = await _sync_quota(panel_api, sub, quota, user["id"])
            if synced is None:
                continue  # панель не отдала статистику — эту подписку проверим в следующий раз
            quota = synced

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
        if quota.limit is not None and not quota.total and state != "admin_on" and state != "exhausted":
            # Неоплаченные дни, за которые ГБ не начислялись: сквад не положен, но это не «исчерпал лимит»
            if in_squad:
                to_remove.append(user["id"])
            if sub in restored:
                restored.remove(sub)
            continue
        if quota.limit is None or not expire or expire <= now:
            # БС не входят в тариф — снимаем; истёкшую подписку не трогаем (ручное включение админа тоже)
            if in_squad and state != "admin_on" and quota.limit is None:
                to_remove.append(user["id"])
            if sub in restored:
                restored.remove(sub)
            continue

        used = 0 if user["id"] not in candidates else None
        if state != "admin_on" and user["id"] in candidates:
            used = await panel_api.get_squad_user_usage_bytes(squad, user["id"], _day(quota.start), _day(now))
            if used is None:
                if sub in restored:
                    restored.remove(sub)
                continue  # нет статистики — ничего не меняем
        over = state != "admin_on" and used is not None and used >= quota.total

        if (not over and used and used >= quota.total * WARN_SHARE and state not in ("exhausted", "admin_on")
                and sub.get("bs_warned_period") != quota.key):
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
            continue

        if state == "exhausted":
            # Лимит той же квоты увеличился (новые бонусы) — доступ возвращается
            await db.set_bs_state(sub["id"], None)
            restored.append(sub)
        if not in_squad:
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
    """Сообщить пользователям, что лимит БС исчерпан / скоро закончится / снова доступен"""
    for sub in result.exhausted:
        quota = get_quota(sub)
        await _send(bot, sub["telegram_id"], (
            f"🛡 <b>Лимит белых списков исчерпан</b>\n\n"
            f"Подписка <b>{sub['tariff_name']}</b>: использовано {format_gb(quota.total or 0)} ГБ "
            f"на серверах белых списков.\n"
            f"Обычные серверы продолжают работать без ограничений.\n\n"
            f"Доступ к белым спискам {_when_back(quota)}{_bonus_hint()}."
        ))
    for sub, used in result.warned:
        quota = get_quota(sub)
        left = format_gb(max(0, quota.total - used))
        if quota.refresh_is_full:
            tail = f"Лимит обновится <b>{format_moment(quota.refresh_at)}</b>."
        elif quota.paid:
            tail = (f"Лимит обновится <b>{format_moment(quota.refresh_at)}</b>, если продлить подписку "
                    f"до {format_moment(quota.paid_until)}.")
        else:
            full = sub.get("bs_limit_gb")
            tail = (f"Оплати подписку — и получишь <b>{full} ГБ</b> белых списков каждые 30 дней. "
                    f"Бонусные дни (например, за приглашённого друга) тоже добавляют ГБ.")
        await _send(bot, sub["telegram_id"], (
            f"🛡 <b>Белые списки: осталось {left} из {format_gb(quota.total)} ГБ</b>\n\n"
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
