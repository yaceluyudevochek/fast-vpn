"""Тесты «триала по устройству»: повторный триал с того же устройства завершается."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db, referral_db  # noqa: E402
from services import referrals, trial_guard  # noqa: E402

ADMIN = 1
NOW = datetime.now(timezone.utc)
FUTURE = "2099-01-01T00:00:00.000Z"


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


def ts(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class FakePanel:
    def __init__(self):
        self.users, self.devices, self.updates = [], [], []

    async def get_all_users(self):
        return self.users

    async def get_all_hwid_devices(self):
        return self.devices

    async def update_user(self, username, **fields):
        self.updates.append((username, fields))
        return True

    async def get_user_by_username(self, username):
        return next((u for u in self.users if u["username"] == username), None)

    async def get_user_hwid_devices(self, uid):
        return {"devices": [d for d in self.devices if d["userId"] == uid]}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


async def trial_user(path, panel, tid, created_ago, hwid=None, paid=False, expiry=FUTURE):
    sql(path, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (tid,))
    sid = await db.create_subscription(tid, 1, 1, f"s{tid}", expiry, f"tg{tid}_1", trial=True)
    sql(path, "UPDATE subscriptions SET created_at = ? WHERE id = ?", (ts(NOW - created_ago), sid))
    panel.users.append({"id": tid, "username": f"tg{tid}_1", "telegramId": tid})
    if hwid:
        panel.devices.append({"hwid": hwid, "userId": tid, "platform": "iOS"})
    if paid:
        sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status) "
                  "VALUES (?, ?, 1, 150, 'CONFIRMED')", (f"p{tid}", tid))
    return sid


async def main():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "INSERT INTO users (telegram_id, role) VALUES (?, 'admin')", (ADMIN,))
    panel, bot = FakePanel(), FakeBot()

    await trial_user(path, panel, 10, timedelta(days=30), hwid="PHONE")         # первый триал на телефоне (давно)
    second = await trial_user(path, panel, 11, timedelta(hours=5), hwid="PHONE")  # второй аккаунт, тот же телефон
    own = await trial_user(path, panel, 12, timedelta(hours=5), hwid="OWN")       # свой телефон
    await trial_user(path, panel, ADMIN, timedelta(days=60), hwid="TEST")         # админ тестировал на устройстве
    tester = await trial_user(path, panel, 13, timedelta(hours=5), hwid="TEST")
    idle = await trial_user(path, panel, 14, timedelta(hours=5))                   # ещё не подключился
    await trial_user(path, panel, 15, timedelta(hours=5), hwid="PHONE", paid=True)  # платил — не трогаем
    sql(path, "UPDATE users SET referred_by = 10 WHERE telegram_id = 11")
    await referral_db.queue(10, 11, "Альт")

    result = await trial_guard.check(bot, panel)
    assert result == {"blocked": 1, "checked": 2}, result
    blocked = await db.get_subscription(second)
    assert blocked["trial_blocked_at"] and blocked["status"] == "EXPIRED"
    assert panel.updates[0][0] == "tg11_1" and "expireAt" in panel.updates[0][1]
    assert any(c == 11 and "уже был использован" in t for c, t in bot.sent)
    alert = next(t for c, t in bot.sent if c == ADMIN)
    assert "Повторный триал" in alert and "<code>11</code>" in alert and "<code>10</code>" in alert
    for sid in (own, tester):
        s = await db.get_subscription(sid)
        assert s["trial_device_checked"] == 1 and not s["trial_blocked_at"]
    assert (await db.get_subscription(idle))["trial_device_checked"] is None

    # Повторно не трогаем; не подключившийся до конца триала — отмечается проверенным
    assert await trial_guard.check(bot, panel) == {"blocked": 0, "checked": 0}
    sql(path, "UPDATE subscriptions SET subscription_expiry = '2020-01-01T00:00:00.000Z' WHERE id = ?", (idle,))
    await trial_guard.check(bot, panel)
    assert (await db.get_subscription(idle))["trial_device_checked"] == 1

    # Награда за «друга» с заблокированным триалом сгорает
    result = await referrals.process_pending(bot, panel)
    assert result["expired"] == 1 and result["granted"] == 0
    assert sql(path, "SELECT status, note FROM referral_rewards")[0] == ("expired", "повторный триал на том же устройстве")
    print("OK  триал по устройству: повторный триал с того же устройства завершён (сообщение и оповещение админам), "
          "свой телефон/админский/платящие не трогаем, награда за такого «друга» сгорает")


if __name__ == "__main__":
    asyncio.run(main())
