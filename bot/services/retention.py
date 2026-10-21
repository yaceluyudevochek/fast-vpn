"""
Возврат клиентов: автоматические сообщения и подарки тем, кто ушёл или так и не начал пользоваться.

Сегменты (у каждого сообщения своё окно — давно ушедшим ничего не шлём):
  winback        — платил, все подписки истекли: через 24 ч (до 7 дней) дарим RETENTION_GIFT_DAYS дней
                   + RETENTION_GIFT_GB ГБ белых списков (бонусом, как за друга);
  trial_expired  — не платил, подписка (триал) истекла: через 24 ч рассказываем о подписке,
                   ещё через 24 ч — тот же подарок;
  trial_idle     — триал идёт, а подключения (>= 50 МБ трафика) нет: помощь на 1-й и 2-й день;
                   после окончания триала такой пользователь попадает в trial_expired;
  start_only     — нажал /start, подписок не было: цепочка 1 / 3 / 7 дней с предложением триала.

Ограничения: подарок — не чаще раза в RETENTION_GIFT_COOLDOWN_DAYS (90) дней; не больше одного
сообщения в сутки на человека; только с 10:00 до 21:00 МСК; забаненным и заблокировавшим бота не пишем;
оплатил / подключился / вышел из окна — цепочка прекращается. Выключено по умолчанию (bot_settings).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db, retention_db
from services import retention_texts as texts
from services import whitelist

logger = logging.getLogger(__name__)

SETTING_ENABLED = "retention_enabled"
MSK = timezone(timedelta(hours=3))
SEND_HOURS_MSK = (10, 21)          # с 10:00 до 20:59
MIN_GAP = timedelta(hours=20)      # не чаще одного маркетингового сообщения в сутки
CONNECTED_BYTES = 50 * 1024 * 1024

CAMPAIGNS = {
    "winback": "Не продлили платную подписку",
    "trial_expired": "Триал закончился, не купили",
    "trial_idle": "Триал без подключения",
    "start_only": "Нажали /start, без триала",
}

# Окна, в которые отправляется шаг (от момента события)
WINBACK_WINDOW = (timedelta(hours=24), timedelta(days=7))
TRIAL_PROMO_WINDOW = (timedelta(hours=24), timedelta(days=7))
TRIAL_GIFT_WINDOW = (timedelta(hours=48), timedelta(days=8))
START_ONLY_STEPS = [(1, 3), (3, 7), (7, 14)]   # (день, до какого дня ещё актуально)


def gift_days() -> int:
    return getattr(config, "RETENTION_GIFT_DAYS", 3)


def gift_gb() -> float:
    return getattr(config, "RETENTION_GIFT_GB", 3)


def gift_cooldown() -> timedelta:
    return timedelta(days=getattr(config, "RETENTION_GIFT_COOLDOWN_DAYS", 90))


async def is_enabled() -> bool:
    return await db.get_setting(SETTING_ENABLED, "0") == "1"


async def set_enabled(enabled: bool):
    await db.set_setting(SETTING_ENABLED, "1" if enabled else "0")


def _utc(value) -> Optional[datetime]:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_send_time(now: datetime) -> bool:
    return SEND_HOURS_MSK[0] <= now.astimezone(MSK).hour < SEND_HOURS_MSK[1]


# ======================================================
# СООБЩЕНИЯ
# ======================================================

def _keyboard(kind: str) -> InlineKeyboardMarkup:
    from handlers.ui_emoji import kb_button

    support = getattr(config, "SUPPORT_URL", None)
    rows = {
        "winback": [[kb_button("subs", "Мои подписки", callback_data="my_subscriptions")],
                    [kb_button("buy", "Продлить подписку", callback_data="show_tariffs")]],
        "trial_promo": [[kb_button("buy", "Выбрать подписку", callback_data="show_tariffs")]],
        "trial_gift": [[kb_button("subs", "Мои подписки", callback_data="my_subscriptions")],
                       [kb_button("buy", "Оформить подписку", callback_data="show_tariffs")]],
        "trial_idle": [[kb_button("subs", "Мои подписки", callback_data="my_subscriptions")]]
                      + ([[kb_button("chat", "Поддержка", url=support)]] if support else []),
        "start_only": [[kb_button("trial", "Попробовать бесплатно", callback_data="activate_trial")],
                       [kb_button("info", "Подробнее", callback_data="info_menu")]],
    }[kind]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _context(tariff: str = "") -> Dict[str, Any]:
    gb = gift_gb()
    bs = f" и {whitelist.format_gb(int(gb * whitelist.GB))} ГБ белых списков" if gb > 0 and await whitelist.is_enabled() else ""
    return {"tariff": tariff, "gift_days": gift_days(), "gift_bs": bs,
            "min_price": await retention_db.min_month_price() or "—"}


_BLOCKED_MARKERS = ("blocked", "deactivated", "chat not found", "user not found", "kicked")


async def _send(bot, telegram_id: int, text: str, keyboard: InlineKeyboardMarkup) -> bool:
    try:
        await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=keyboard)
        return True
    except Exception as e:
        if any(marker in str(e).lower() for marker in _BLOCKED_MARKERS):
            await retention_db.set_bot_blocked(telegram_id, True)  # больше не пишем, пока не нажмёт /start
        else:
            logger.warning(f"Возврат клиентов: не удалось отправить {telegram_id}: {e}")
        return False
    finally:
        await asyncio.sleep(0.05)


# ======================================================
# ПОДАРОК
# ======================================================

async def _gift_allowed(telegram_id: int, now: datetime) -> bool:
    last = _utc(await retention_db.last_gift_time(telegram_id))
    return last is None or now - last >= gift_cooldown()


async def _gift(panel_api, sub: Dict[str, Any]) -> bool:
    """Продлить подписку на подарочные дни и начислить бонусные ГБ белых списков"""
    new_expiry = await panel_api.extend_subscription_by_username(sub["username_in_panel"], gift_days())
    if not new_expiry:
        logger.error(f"Возврат клиентов: не удалось продлить {sub['username_in_panel']} в панели")
        return False
    await db.update_subscription_expiry(sub["id"], new_expiry)
    await db.update_subscription_status(sub["id"], "ACTIVE")
    if gift_gb() > 0:
        try:
            await whitelist.grant_bonus(panel_api, sub["id"], int(gift_gb() * whitelist.GB))
        except Exception as e:
            logger.error(f"Возврат клиентов: не удалось начислить бонус БС подписке {sub['id']}: {e}")
    return True


# ======================================================
# ОСНОВНОЙ ПРОХОД
# ======================================================

async def _process_user(bot, panel_api, user: Dict[str, Any], now: datetime, tariffs: Dict[int, str]) -> Optional[str]:
    """Отправить пользователю не больше одного сообщения. Возвращает «кампания:шаг» или None."""
    tid = user["telegram_id"]
    last = _utc(user.get("last_marketing_at"))
    if last and now - last < MIN_GAP:
        return None

    subs = user["subscriptions"]
    # 4. Нажал /start, подписок не было
    if not subs:
        age = now - (_utc(user.get("created_at")) or now)
        for day, until in START_ONLY_STEPS:
            if timedelta(days=day) <= age < timedelta(days=until):
                step = f"d{day}"
                if await retention_db.has_event(tid, "start_only", step):
                    return None
                await retention_db.record_event(tid, "start_only", step)
                await _send(bot, tid, texts.START_ONLY[day], _keyboard("start_only"))
                return f"start_only:{step}"
        return None

    active = [s for s in subs if (_utc(s["subscription_expiry"]) or now) > now]

    # 3. Триал идёт, подключения нет — помощь на 1-й и 2-й день
    if active:
        if user["has_paid"]:
            return None
        trial = next((s for s in active if s.get("trial_used")), None)
        if not trial:
            return None
        day = int((now - (_utc(trial["created_at"]) or now)) // timedelta(days=1))
        if day not in texts.TRIAL_IDLE_HELP:
            return None
        ref = f"{trial['id']}:d{day}"
        if await retention_db.has_event(tid, "trial_idle", "help", ref):
            return None
        used = await panel_api.get_used_traffic_bytes(trial["username_in_panel"])
        if used is None or used >= CONNECTED_BYTES:
            return None  # нет данных или уже подключился
        await retention_db.record_event(tid, "trial_idle", "help", ref, trial["id"])
        await _send(bot, tid, texts.TRIAL_IDLE_HELP[day], _keyboard("trial_idle"))
        return "trial_idle:help"

    # Все подписки истекли
    latest = max(subs, key=lambda s: _utc(s["subscription_expiry"]) or datetime.min.replace(tzinfo=timezone.utc))
    expired_at = _utc(latest["subscription_expiry"])
    if not expired_at:
        return None
    since = now - expired_at
    ref = f"{latest['id']}:{latest['subscription_expiry']}"
    ctx = await _context(tariffs.get(latest["id"], ""))

    # 1. Платил и не продлил — подарок через 24 часа
    if user["has_paid"]:
        if not (WINBACK_WINDOW[0] <= since < WINBACK_WINDOW[1]):
            return None
        if await retention_db.has_event(tid, "winback", "gift", ref) or not await _gift_allowed(tid, now):
            return None
        if not await _gift(panel_api, latest):
            return None
        await retention_db.record_event(tid, "winback", "gift", ref, latest["id"])
        await _send(bot, tid, texts.WINBACK_GIFT.format(**ctx), _keyboard("winback"))
        return "winback:gift"

    # 2. Триал закончился, не купил: сначала рассказ о подписке, через сутки — подарок
    if TRIAL_PROMO_WINDOW[0] <= since < TRIAL_PROMO_WINDOW[1] \
            and not await retention_db.has_event(tid, "trial_expired", "promo", ref):
        await retention_db.record_event(tid, "trial_expired", "promo", ref, latest["id"])
        await _send(bot, tid, texts.TRIAL_EXPIRED_PROMO.format(**ctx), _keyboard("trial_promo"))
        return "trial_expired:promo"

    promo_at = _utc(await retention_db.event_time(tid, "trial_expired", "promo", ref))
    if (promo_at and TRIAL_GIFT_WINDOW[0] <= since < TRIAL_GIFT_WINDOW[1]
            and now - promo_at >= MIN_GAP
            and not await retention_db.has_event(tid, "trial_expired", "gift", ref)
            and await _gift_allowed(tid, now)):
        if not await _gift(panel_api, latest):
            return None
        await retention_db.record_event(tid, "trial_expired", "gift", ref, latest["id"])
        await _send(bot, tid, texts.TRIAL_EXPIRED_GIFT.format(**ctx), _keyboard("trial_gift"))
        return "trial_expired:gift"
    return None


_lock = asyncio.Lock()


async def run(bot, panel_api, now: Optional[datetime] = None, ignore_hours: bool = False) -> Optional[Dict[str, int]]:
    """Один проход по всем пользователям. None — выключено или сейчас не время рассылки."""
    now = now or datetime.now(timezone.utc)
    if not await is_enabled() or (not ignore_hours and not is_send_time(now)):
        return None
    async with _lock:
        users = await retention_db.get_audience()
        tariffs = await retention_db.subscription_tariff_names()
        sent: Dict[str, int] = {}
        for user in users:
            try:
                result = await _process_user(bot, panel_api, user, now, tariffs)
            except Exception as e:
                logger.error(f"Возврат клиентов: ошибка для {user['telegram_id']}: {e}", exc_info=True)
                continue
            if result:
                sent[result] = sent.get(result, 0) + 1
        if sent:
            logger.info(f"📣 Возврат клиентов: {sent}")
        return sent


async def send_previews(bot, chat_id: int):
    """Прислать админу все черновики сообщений с примерными подстановками"""
    ctx = await _context("Default")
    previews: List[tuple] = [
        ("1. Не продлил платную подписку (через 24 ч, подарок)", texts.WINBACK_GIFT.format(**ctx), "winback"),
        ("2a. Триал закончился (через 24 ч)", texts.TRIAL_EXPIRED_PROMO.format(**ctx), "trial_promo"),
        ("2b. Триал закончился (ещё через 24 ч, подарок)", texts.TRIAL_EXPIRED_GIFT.format(**ctx), "trial_gift"),
    ]
    previews += [(f"3. Триал без подключения, день {d}", t, "trial_idle") for d, t in texts.TRIAL_IDLE_HELP.items()]
    previews += [(f"4. Только /start, день {d}", t, "start_only") for d, t in texts.START_ONLY.items()]
    for title, text, kind in previews:
        await bot.send_message(chat_id, f"<i>Пример — {title}</i>\n\n{text}", parse_mode="HTML",
                               reply_markup=_keyboard(kind))
        await asyncio.sleep(0.1)
