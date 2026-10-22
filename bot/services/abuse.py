"""
Оповещения админам о «плохишах».

1. Аномальный трафик (все ноды — обычные и белые списки):
   >= ABUSE_DAY_GB за сутки (сегодня или вчера) или >= ABUSE_WEEK_GB за 7 дней.
   Об одном пользователе — раз в сутки (по дню) и раз в неделю (по неделе).
2. Абуз триала:
   - одно устройство (HWID) у 2+ разных Telegram-аккаунтов;
   - один IP (с которого устройство пришло за подпиской) у 2..ABUSE_IP_GROUP_MAX аккаунтов,
     из которых минимум двое ни разу не платили. Большие группы — это мобильные операторы
     и общий Wi-Fi, их пропускаем.
   Сообщаем о новой группе или когда в группу добавился аккаунт. При первом запуске уже
   существующие группы приходят одной сводкой.
Админы и пользователи из списка «Игнорировать» не попадают в оповещения.
"""
import hashlib
import html
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import abuse_db, db

logger = logging.getLogger(__name__)

GB = 1024 ** 3
SETTING_DEVICES_READY = "abuse_devices_ready"
MAX_SINGLE_ALERTS = 10      # если новых групп больше — одна сводка вместо пачки сообщений


def day_gb() -> float:
    return getattr(config, "ABUSE_DAY_GB", 50)


def week_gb() -> float:
    return getattr(config, "ABUSE_WEEK_GB", 200)


def ip_group_max() -> int:
    return getattr(config, "ABUSE_IP_GROUP_MAX", 5)


def telegram_id_of(panel_user: Dict[str, Any]) -> Optional[int]:
    if panel_user.get("telegramId"):
        return int(panel_user["telegramId"])
    match = re.match(r"tg(\d+)_", panel_user.get("username") or "")
    return int(match.group(1)) if match else None


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:12]


def _ignore_keyboard(telegram_ids: Iterable[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔇 Игнорировать {tid}", callback_data=f"abuse_ignore_{tid}")]
        for tid in sorted(telegram_ids)
    ])


async def _broadcast(bot, text: str, keyboard: Optional[InlineKeyboardMarkup] = None):
    for admin in await abuse_db.admin_ids():
        try:
            await bot.send_message(admin, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Не удалось отправить оповещение админу {admin}: {e}")


def _user_line(tid: int, info: Dict[str, Any]) -> str:
    username = f" (@{html.escape(info['username'])})" if info.get("username") else ""
    if info.get("has_paid"):
        status = "платил"
    elif info.get("had_trial"):
        status = "триал, не платил"
    else:
        status = "не платил"
    since = f", с {str(info['created_at'])[:10]}" if info.get("created_at") else ""
    return f"• <code>{tid}</code>{username} — {status}{since}"


# ======================================================
# 1. АНОМАЛЬНЫЙ ТРАФИК
# ======================================================

async def check_traffic(bot, panel_api, now: Optional[datetime] = None) -> Optional[int]:
    """Возвращает число отправленных оповещений (None — панель не ответила)"""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    panel_users = await panel_api.get_all_users()
    if panel_users is None:
        return None
    tg_by_username = {u["username"]: telegram_id_of(u) for u in panel_users}
    skip = await abuse_db.admin_ids() | await abuse_db.ignored()

    found: Dict[int, List[str]] = defaultdict(list)
    keys: Dict[int, List[str]] = defaultdict(list)

    for day in (today - timedelta(days=1), today):
        top = await panel_api.get_users_traffic(day.isoformat(), day.isoformat())
        if top is None:
            return None
        for row in top:
            tid = tg_by_username.get(row["username"])
            if not tid or tid in skip or row["total"] < day_gb() * GB:
                continue
            key = f"traffic_day:{tid}:{day.isoformat()}"
            if not await abuse_db.was_alerted(key):
                found[tid].append(f"<b>{row['total'] / GB:.1f} ГБ</b> за {day:%d.%m} (сутки, UTC)"
                                  f" — {html.escape(row['username'])}")
                keys[tid].append(key)

    week = await panel_api.get_users_traffic((today - timedelta(days=6)).isoformat(), today.isoformat())
    if week is None:
        return None
    for row in week:
        tid = tg_by_username.get(row["username"])
        if not tid or tid in skip or row["total"] < week_gb() * GB:
            continue
        key = f"traffic_week:{tid}"
        if not await abuse_db.was_alerted(key, within_days=7):
            found[tid].append(f"<b>{row['total'] / GB:.1f} ГБ</b> за 7 дней — {html.escape(row['username'])}")
            keys[tid].append(key)

    if not found:
        return 0
    infos = await abuse_db.users_info(found)
    for tid, lines in found.items():
        text = (
            f"📈 <b>Аномальный трафик</b>\n\n"
            f"{_user_line(tid, infos.get(tid, {}))}\n" + "\n".join(f"   {line}" for line in lines) +
            f"\n\nПороги: {day_gb():g} ГБ в сутки, {week_gb():g} ГБ за неделю (все ноды)."
        )
        await _broadcast(bot, text, _ignore_keyboard([tid]))
        for key in keys[tid]:
            await abuse_db.mark_alerted(key)
    return len(found)


# ======================================================
# 2. АБУЗ ТРИАЛА: ОБЩИЕ УСТРОЙСТВА И IP
# ======================================================

def _device_label(device: Dict[str, Any]) -> str:
    parts = [device.get("deviceModel"), device.get("platform"), device.get("osVersion")]
    return html.escape(" · ".join(str(p) for p in parts if p)) or "неизвестно"


async def find_device_groups(panel_api) -> Optional[List[Dict[str, Any]]]:
    """Группы аккаунтов с общим HWID или IP (без учёта админов и игнорируемых)"""
    panel_users = await panel_api.get_all_users()
    devices = await panel_api.get_all_hwid_devices()
    if panel_users is None or devices is None:
        return None
    tg_by_panel_id = {u["id"]: telegram_id_of(u) for u in panel_users}
    skip = await abuse_db.admin_ids() | await abuse_db.ignored()

    by_hwid: Dict[str, Set[int]] = defaultdict(set)
    hwid_device: Dict[str, Dict[str, Any]] = {}
    by_ip: Dict[str, Set[int]] = defaultdict(set)
    for device in devices:
        tid = tg_by_panel_id.get(device.get("userId"))
        if not tid:
            continue
        by_hwid[device["hwid"]].add(tid)
        hwid_device.setdefault(device["hwid"], device)
        if device.get("requestIp"):
            by_ip[device["requestIp"]].add(tid)

    infos = await abuse_db.users_info({t for s in list(by_hwid.values()) + list(by_ip.values()) for t in s})
    groups = []
    for hwid, members in by_hwid.items():
        visible = members - skip
        if len(members) >= 2 and len(visible) >= 2:
            groups.append({
                "kind": "hwid", "members": visible,
                "key": f"hwid:{_hash(hwid)}:{','.join(map(str, sorted(members)))}",
                "label": _device_label(hwid_device[hwid]),
            })
    hwid_sets = [members for members in by_hwid.values() if len(members) >= 2]
    for ip, members in by_ip.items():
        visible = members - skip
        unpaid = [t for t in visible if not infos.get(t, {}).get("has_paid")]
        if any(members <= hwid_members for hwid_members in hwid_sets):
            continue  # те же аккаунты уже пойманы по общему устройству — второе оповещение не нужно
        if 2 <= len(members) <= ip_group_max() and len(visible) >= 2 and len(unpaid) >= 2:
            groups.append({
                "kind": "ip", "members": visible,
                "key": f"ip:{_hash(ip)}:{','.join(map(str, sorted(members)))}",
                "label": html.escape(ip),
            })
    for group in groups:
        group["infos"] = {t: infos.get(t, {}) for t in group["members"]}
    return groups


def _group_text(group: Dict[str, Any]) -> str:
    title = ("📱 <b>Одно устройство у нескольких аккаунтов</b>\nУстройство: " if group["kind"] == "hwid"
             else "🌐 <b>Несколько неплативших аккаунтов с одного IP</b>\nIP: ")
    lines = [_user_line(t, group["infos"][t]) for t in sorted(group["members"])]
    return f"{title}{group['label']}\n\n" + "\n".join(lines)


async def check_devices(bot, panel_api) -> Optional[int]:
    """Возвращает число новых групп (None — панель не ответила)"""
    groups = await find_device_groups(panel_api)
    if groups is None:
        return None
    new = [g for g in groups if not await abuse_db.was_alerted(g["key"])]
    if not new:
        return 0

    first_run = await db.get_setting(SETTING_DEVICES_READY) != "1"
    if first_run or len(new) > MAX_SINGLE_ALERTS:
        header = ("🕵️ <b>Совпадения устройств и IP — первая проверка</b>\n"
                  "Это все группы, которые уже есть в панели. Дальше будут приходить только новые.\n"
                  if first_run else f"🕵️ <b>Новые совпадения устройств и IP: {len(new)}</b>\n")
        footer = "\nЧтобы не получать оповещения о человеке, добавьте его в игнор: /admin → 🚨 Плохиши."
        chunk = header
        for group in new:
            part = "\n" + _group_text(group) + "\n"
            if len(chunk) + len(part) + len(footer) > 3900:
                await _broadcast(bot, chunk)
                chunk = ""
            chunk += part
        await _broadcast(bot, chunk + footer)
    else:
        for group in new:
            await _broadcast(bot, _group_text(group), _ignore_keyboard(group["members"]))

    for group in new:
        await abuse_db.mark_alerted(group["key"])
    await db.set_setting(SETTING_DEVICES_READY, "1")
    return len(new)


async def run(bot, panel_api) -> Dict[str, Optional[int]]:
    result = {}
    for name, check in (("traffic", check_traffic), ("devices", check_devices)):
        try:
            result[name] = await check(bot, panel_api)
        except Exception as e:
            logger.error(f"Проверка «плохишей» ({name}) упала: {e}", exc_info=True)
            result[name] = None
    if any(result.values()):
        logger.info(f"🚨 Оповещения о плохишах: {result}")
    return result
