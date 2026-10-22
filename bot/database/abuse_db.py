"""Данные оповещений о «плохишах» (таблицы создаёт миграция #10 в database/db.py)."""
from typing import Any, Dict, List, Optional, Set

import aiosqlite

from database import db as core


def _connect():
    return aiosqlite.connect(core.DB_PATH)


async def was_alerted(key: str, within_days: Optional[int] = None) -> bool:
    query, params = "SELECT 1 FROM abuse_alerts WHERE key = ?", [key]
    if within_days is not None:
        query += " AND created_at >= datetime('now', ?)"
        params.append(f"-{int(within_days)} days")
    async with _connect() as db:
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone() is not None


async def mark_alerted(key: str):
    async with _connect() as db:
        await db.execute("INSERT OR REPLACE INTO abuse_alerts (key, created_at) VALUES (?, CURRENT_TIMESTAMP)", (key,))
        await db.commit()


async def has_any_alert(prefix: str) -> bool:
    async with _connect() as db:
        async with db.execute("SELECT 1 FROM abuse_alerts WHERE key LIKE ? LIMIT 1", (prefix + "%",)) as cursor:
            return await cursor.fetchone() is not None


async def ignored() -> Set[int]:
    async with _connect() as db:
        async with db.execute("SELECT telegram_id FROM abuse_ignore") as cursor:
            return {row[0] for row in await cursor.fetchall()}


async def set_ignored(telegram_id: int, ignore: bool, by: Optional[int] = None):
    async with _connect() as db:
        if ignore:
            await db.execute("INSERT OR IGNORE INTO abuse_ignore (telegram_id, added_by) VALUES (?, ?)", (telegram_id, by))
        else:
            await db.execute("DELETE FROM abuse_ignore WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def admin_ids() -> Set[int]:
    from config.config import ADMIN_ID
    async with _connect() as db:
        async with db.execute("SELECT telegram_id FROM users WHERE role = 'admin'") as cursor:
            ids = {row[0] for row in await cursor.fetchall()}
    if ADMIN_ID:
        ids.add(ADMIN_ID)
    return ids


async def users_info(telegram_ids) -> Dict[int, Dict[str, Any]]:
    """username, дата регистрации, платил ли, брал ли триал — для текста оповещения"""
    ids = list({int(t) for t in telegram_ids})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"""
            SELECT u.telegram_id, u.username, u.created_at, u.role,
                   EXISTS (SELECT 1 FROM payment_transactions p
                           WHERE p.telegram_id = u.telegram_id AND p.status = 'CONFIRMED') AS has_paid,
                   EXISTS (SELECT 1 FROM subscriptions s
                           WHERE s.telegram_id = u.telegram_id AND s.trial_used = 1) AS had_trial
            FROM users u WHERE u.telegram_id IN ({marks})
        """, ids) as cursor:
            return {row["telegram_id"]: dict(row) for row in await cursor.fetchall()}


async def find_users(query: str) -> List[Dict[str, Any]]:
    """Поиск по Telegram username (без @, без учёта регистра) или по имени в панели tg<ID>_<тип>"""
    import re
    q = query.strip().lstrip("@")
    panel = re.fullmatch(r"tg(\d+)(?:_\d+)?", q, flags=re.IGNORECASE)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        if panel:
            sql, params = "SELECT * FROM users WHERE telegram_id = ?", (int(panel.group(1)),)
        elif q.isdigit():
            sql, params = "SELECT * FROM users WHERE telegram_id = ?", (int(q),)
        else:
            sql, params = "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (q,)
        async with db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
