"""Тесты награды за друга: ожидание до подключения, начисление, общее устройство, сгорание."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db, referral_db  # noqa: E402
from services import referrals, whitelist  # noqa: E402

GB = whitelist.GB
BS = "bs-squad-uuid"
FUTURE = "2099-01-01T00:00:00.000Z"
ADMIN = 1


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


class FakePanel:
    def __init__(self):
        self.users = {}       # username -> {id, ...}
        self.devices = {}     # panel id -> [devices]
        self.extended = []
        self.down = False

    def add(self, username, uid, squads=("std",)):
        self.users[username] = {"id": uid, "username": username,
                                "activeInternalSquads": [{"uuid": s} for s in squads]}

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def get_user_hwid_devices(self, uid):
        return None if self.down else {"devices": self.devices.get(uid, [])}

    async def extend_subscription_by_username(self, username, days, traffic_limit_bytes=None):
        self.extended.append((username, days))
        return FUTURE

    async def create_user(self, username, telegram_id, squad_id, days=30, hwid_limit=3, tag=None,
                          traffic_limit_bytes=0, extra_squad_ids=None):
        self.add(username, 500 + len(self.users), squads=[squad_id] + list(extra_squad_ids or []))
        return {"shortUuid": f"su-{username}", "expireAt": FUTURE}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "UPDATE tariff_types SET bs_limit_gb = 50 WHERE id = 1")
    whitelist.config.BS_SQUAD_UUID = BS
    await whitelist.set_enabled(True)
    sql(path, "INSERT INTO users (telegram_id, username, role) VALUES (?, 'boss', 'admin')", (ADMIN,))
    return path


async def friend_with_trial(path, panel, friend_id, referrer_id, panel_id):
    sql(path, "INSERT INTO users (telegram_id, referred_by) VALUES (?, ?)", (friend_id, referrer_id))
    await db.create_subscription(friend_id, 1, 1, f"s{friend_id}", FUTURE, f"tg{friend_id}_1", trial=True)
    panel.add(f"tg{friend_id}_1", panel_id)


async def test_flow():
    path = await fresh_db()
    panel, bot = FakePanel(), FakeBot()
    sql(path, "INSERT INTO users (telegram_id, referrals_activated) VALUES (10, 0)")
    base = await db.create_subscription(10, 1, 1, "s10", FUTURE, "tg10_1")
    panel.add("tg10_1", 110)
    await friend_with_trial(path, panel, 20, 10, 120)

    # Друг активировал триал — награда в ожидании, пригласившему сообщаем
    await referrals.queue_reward(bot, 20, "Вася")
    assert bot.sent[-1][0] == 10 and "начислится, как только он подключится" in bot.sent[-1][1]
    await referrals.queue_reward(bot, 20, "Вася")                      # повторно — не дублируется
    assert len(await referral_db.pending()) == 1 and await referral_db.pending_count(10) == 1

    # Ещё не подключился — ничего не начисляем
    assert (await referrals.process_pending(bot, panel))["granted"] == 0
    assert panel.extended == [] and (await db.get_subscription(base))["bs_bonus_bytes"] == 0

    # Панель недоступна — ждём
    panel.devices[120] = [{"hwid": "F1", "platform": "Android"}]
    panel.down = True
    assert (await referrals.process_pending(bot, panel))["granted"] == 0
    panel.down = False

    # Подключился (появилось устройство) — +7 дней и +7 ГБ, счётчик, сообщение
    result = await referrals.process_pending(bot, panel)
    assert result == {"granted": 1, "shared_device": 0, "expired": 0}, result
    assert panel.extended == [("tg10_1", 7)] and (await db.get_subscription(base))["bs_bonus_bytes"] == 7 * GB
    assert sql(path, "SELECT referrals_activated FROM users WHERE telegram_id = 10")[0][0] == 1
    assert "подключился к VPN" in bot.sent[-1][1] and "+7 ГБ" in bot.sent[-1][1]
    assert (await referrals.process_pending(bot, panel))["granted"] == 0     # второй раз не начисляем
    assert sql(path, "SELECT status, note FROM referral_rewards")[0] == ("granted", None)
    print("OK  награда за друга: ждёт подключения, начисляется один раз (+7 дней и +7 ГБ), сбой панели не теряет")


async def test_shared_device_and_expiry():
    path = await fresh_db()
    panel, bot = FakePanel(), FakeBot()
    sql(path, "INSERT INTO users (telegram_id, username) VALUES (30, 'farmer')")
    await db.create_subscription(30, 1, 1, "s30", FUTURE, "tg30_1")
    panel.add("tg30_1", 130)
    panel.devices[130] = [{"hwid": "SAME", "deviceModel": "iPhone 15", "platform": "iOS"}]
    await friend_with_trial(path, panel, 40, 30, 140)
    await friend_with_trial(path, panel, 50, 30, 150)
    await referrals.queue_reward(bot, 40, "Альт")
    await referrals.queue_reward(bot, 50, "Пропавший")

    # Друг подключился с того же устройства — награда начислена, админам оповещение
    panel.devices[140] = [{"hwid": "SAME", "deviceModel": "iPhone 15", "platform": "iOS"}]
    result = await referrals.process_pending(bot, panel)
    assert result["granted"] == 1 and result["shared_device"] == 1, result
    alert = next(t for c, t in bot.sent if c == ADMIN)
    assert "с устройства пригласившего" in alert and "iPhone 15" in alert and "<code>30</code>" in alert
    assert "<code>40</code>" in alert and "Уменьшить дни" in alert
    assert ("tg30_1", 7) in panel.extended
    assert sql(path, "SELECT note FROM referral_rewards WHERE friend_id = 40")[0][0] == "общее устройство с пригласившим"

    # Второй друг так и не подключился — через 2 дня ещё ждём, через 3 дня награда сгорает
    assert (await referrals.process_pending(bot, panel, now=datetime.now(timezone.utc) + timedelta(days=2)))["expired"] == 0
    later = datetime.now(timezone.utc) + timedelta(days=3, hours=1)
    assert (await referrals.process_pending(bot, panel, now=later))["expired"] == 1
    assert sql(path, "SELECT status FROM referral_rewards WHERE friend_id = 50")[0][0] == "expired"
    assert len([e for e in panel.extended if e[0] == "tg30_1"]) == 1

    # Пригласившего без подписки — создаём базовую со сквадом БС и сразу начисляем бонус
    sql(path, "INSERT INTO users (telegram_id) VALUES (60)")
    await friend_with_trial(path, panel, 70, 60, 170)
    await referrals.queue_reward(bot, 70, "Друг")
    panel.devices[170] = [{"hwid": "NEW"}]
    await referrals.process_pending(bot, panel)
    created = (await db.get_user_subscriptions(60))[0]
    assert created["username_in_panel"] == "tg60_1" and created["bs_bonus_bytes"] == 7 * GB
    assert BS in [s["uuid"] for s in panel.users["tg60_1"]["activeInternalSquads"]]
    print("OK  общее устройство с пригласившим — бонус начислен, админам оповещение; не подключился за 3 дня — сгорает; "
          "базовая подписка создаётся при необходимости")


async def main():
    await test_flow()
    await test_shared_device_and_expiry()


if __name__ == "__main__":
    asyncio.run(main())
