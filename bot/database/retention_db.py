"""Доступ к данным системы возврата клиентов (таблицы создаёт миграция #9 в database/db.py)."""
from collections import defaultdict
from typing import Any, Dict, List, Optional

import aiosqlite

from database import db as core


def _connect():
    return aiosqlite.connect(core.DB_PATH)


async def get_audience() -> List[Dict[str, Any]]:
    """
    Пользователи, которым можно писать (не забанены, не заблокировали бота), с подписками
    (кроме отключённых админом) и признаком хотя бы одной оплаты.
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.telegram_id, u.created_at, u.last_marketing_at,
                   EXISTS (SELECT 1 FROM payment_transactions p
                           WHERE p.telegram_id = u.telegram_id AND p.status = 'CONFIRMED') AS has_paid
            FROM users u
            WHERE COALESCE(u.role, 'user') != 'banned' AND u.bot_blocked_at IS NULL
        """) as cursor:
            users = [dict(row) for row in await cursor.fetchall()]
        async with db.execute("""
            SELECT id, telegram_id, username_in_panel, subscription_expiry, trial_used, created_at, status,
                   bs_paid_until, trial_blocked_at
            FROM subscriptions WHERE status != 'DISABLED'
        """) as cursor:
            subs = defaultdict(list)
            for row in await cursor.fetchall():
                subs[row["telegram_id"]].append(dict(row))
    for user in users:
        user["subscriptions"] = subs.get(user["telegram_id"], [])
    return users


async def has_event(telegram_id: int, campaign: str, step: str, ref: Optional[str] = None) -> bool:
    query = "SELECT 1 FROM retention_events WHERE telegram_id = ? AND campaign = ? AND step = ?"
    params = [telegram_id, campaign, step]
    if ref is not None:
        query += " AND ref = ?"
        params.append(ref)
    async with _connect() as db:
        async with db.execute(query + " LIMIT 1", params) as cursor:
            return await cursor.fetchone() is not None


async def event_time(telegram_id: int, campaign: str, step: str, ref: str) -> Optional[str]:
    async with _connect() as db:
        async with db.execute("""
            SELECT created_at FROM retention_events
            WHERE telegram_id = ? AND campaign = ? AND step = ? AND ref = ?
            ORDER BY id DESC LIMIT 1
        """, (telegram_id, campaign, step, ref)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def last_gift_time(subscription_id: int) -> Optional[str]:
    """Когда последний раз дарили дни к этой подписке"""
    async with _connect() as db:
        async with db.execute(
                "SELECT MAX(created_at) FROM retention_events WHERE subscription_id = ? AND step = 'gift'",
                (subscription_id,)) as cursor:
            return (await cursor.fetchone())[0]


async def record_event(telegram_id: int, campaign: str, step: str, ref: Optional[str] = None,
                       subscription_id: Optional[int] = None):
    async with _connect() as db:
        await db.execute("""
            INSERT INTO retention_events (telegram_id, campaign, step, ref, subscription_id)
            VALUES (?, ?, ?, ?, ?)
        """, (telegram_id, campaign, step, ref, subscription_id))
        await db.execute("UPDATE users SET last_marketing_at = CURRENT_TIMESTAMP WHERE telegram_id = ?",
                         (telegram_id,))
        await db.commit()


async def set_bot_blocked(telegram_id: int, blocked: bool):
    async with _connect() as db:
        await db.execute(
            f"UPDATE users SET bot_blocked_at = {'CURRENT_TIMESTAMP' if blocked else 'NULL'} WHERE telegram_id = ?",
            (telegram_id,))
        await db.commit()


async def stats(days: int = 30, convert_days: int = 14) -> Dict[str, Dict[str, int]]:
    """
    По кампаниям за последние days дней: сколько сообщений, подарков, и сколько получивших
    потом оплатили в течение convert_days дней после первого сообщения кампании.
    """
    async with _connect() as db:
        async with db.execute("""
            SELECT campaign,
                   COUNT(*) AS messages,
                   SUM(step = 'gift') AS gifts,
                   COUNT(DISTINCT telegram_id) AS users
            FROM retention_events
            WHERE created_at >= datetime('now', ?)
            GROUP BY campaign
        """, (f"-{int(days)} days",)) as cursor:
            result = {row[0]: {"messages": row[1], "gifts": row[2] or 0, "users": row[3], "paid": 0}
                      for row in await cursor.fetchall()}
        async with db.execute("""
            SELECT f.campaign, COUNT(DISTINCT f.telegram_id)
            FROM (SELECT telegram_id, campaign, MIN(created_at) AS first_at FROM retention_events
                  WHERE created_at >= datetime('now', ?) GROUP BY telegram_id, campaign) f
            JOIN payment_transactions p ON p.telegram_id = f.telegram_id AND p.status = 'CONFIRMED'
             AND p.created_at >= f.first_at AND p.created_at <= datetime(f.first_at, ?)
            GROUP BY f.campaign
        """, (f"-{int(days)} days", f"+{int(convert_days)} days")) as cursor:
            for campaign, paid in await cursor.fetchall():
                result.setdefault(campaign, {"messages": 0, "gifts": 0, "users": 0, "paid": 0})["paid"] = paid
    return result


async def min_month_price() -> Optional[int]:
    """Самая низкая цена активного плана примерно на месяц (для текстов «от N ₽ в месяц»)"""
    async with _connect() as db:
        async with db.execute("""
            SELECT MIN(tp.price) FROM tariff_plans tp
            JOIN tariff_types tt ON tt.id = tp.tariff_type_id
            WHERE tp.is_active = 1 AND tt.is_active = 1 AND tp.days BETWEEN 28 AND 31
        """) as cursor:
            return (await cursor.fetchone())[0]


async def subscription_tariff_names() -> Dict[int, str]:
    async with _connect() as db:
        async with db.execute("""
            SELECT s.id, tt.name FROM subscriptions s JOIN tariff_types tt ON tt.id = s.tariff_type_id
        """) as cursor:
            return {row[0]: row[1] for row in await cursor.fetchall()}
