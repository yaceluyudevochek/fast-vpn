"""Отложенные награды за приглашённых друзей (таблицу создаёт миграция #11 в database/db.py)."""
from typing import Any, Dict, List, Optional

import aiosqlite

from database import db as core


def _connect():
    return aiosqlite.connect(core.DB_PATH)


async def queue(referrer_id: int, friend_id: int, friend_name: Optional[str]) -> bool:
    """Поставить награду в ожидание. False — за этого друга награда уже была."""
    async with _connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO referral_rewards (referrer_id, friend_id, friend_name) VALUES (?, ?, ?)",
            (referrer_id, friend_id, friend_name))
        await db.commit()
        return cursor.rowcount == 1


async def pending() -> List[Dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM referral_rewards WHERE status = 'pending' ORDER BY id") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def set_status(reward_id: int, status: str, note: Optional[str] = None):
    async with _connect() as db:
        await db.execute(
            "UPDATE referral_rewards SET status = ?, note = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, note, reward_id))
        await db.commit()


async def pending_count(referrer_id: int) -> int:
    async with _connect() as db:
        async with db.execute(
                "SELECT COUNT(*) FROM referral_rewards WHERE referrer_id = ? AND status = 'pending'",
                (referrer_id,)) as cursor:
            return (await cursor.fetchone())[0]
