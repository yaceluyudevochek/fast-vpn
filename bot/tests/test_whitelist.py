"""Тесты белых списков: лимиты, периоды по 30 дней, выдача/снятие сквада, решения админа, миграция."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from services import whitelist  # noqa: E402

GB = whitelist.GB
BS = "bs-squad-uuid"
STD = "standard-squad-uuid"
ADMIN_SQUAD = "admin-squad-uuid"   # админский сквад (например, для своих) — бот его не трогает
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2020-01-01T00:00:00.000Z"


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


class FakePanel:
    def __init__(self):
        self.users = {}       # username -> {id, username, expireAt, activeInternalSquads}
        self.usage = {}       # panel id -> байты на нодах сквада БС
        self.exact_calls = []

    def add(self, username, uid, squads=(STD,), expire=FUTURE):
        self.users[username] = {
            "id": uid, "username": username, "expireAt": expire,
            "activeInternalSquads": [{"uuid": s, "name": s} for s in squads],
        }

    def squads(self, username):
        return [s["uuid"] for s in self.users[username]["activeInternalSquads"]]

    def _by_id(self, uid):
        return next(u for u in self.users.values() if u["id"] == uid)

    async def get_all_users(self):
        return list(self.users.values())

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def get_squad_usage(self, squad, start, end, min_total_bytes=0):
        assert squad == BS
        return {uid: b for uid, b in self.usage.items() if b >= min_total_bytes}

    async def get_squad_user_usage_bytes(self, squad, uid, start, end):
        assert squad == BS
        self.exact_calls.append(uid)
        return self.usage.get(uid, 0)

    async def add_users_to_squad(self, squad, ids):
        for uid in ids:
            user = self._by_id(uid)
            if squad not in [s["uuid"] for s in user["activeInternalSquads"]]:
                user["activeInternalSquads"].append({"uuid": squad, "name": squad})
        return True

    async def remove_users_from_squad(self, squad, ids):
        for uid in ids:
            user = self._by_id(uid)
            user["activeInternalSquads"] = [s for s in user["activeInternalSquads"] if s["uuid"] != squad]
        return True

    async def set_user_squads(self, username, squads):
        self.users[username]["activeInternalSquads"] = [{"uuid": s, "name": s} for s in dict.fromkeys(squads)]
        return True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "UPDATE tariff_types SET bs_limit_gb = 50 WHERE id = 1")   # Default: 50 ГБ
    sql(path, "UPDATE tariff_types SET bs_limit_gb = NULL WHERE id = 2")  # Premium: БС не входят
    return path


async def make_sub(path, tid, username, type_id=1, paid=False, anchor_days_ago=1, state=None, state_period=None):
    sql(path, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (tid,))
    sid = await db.create_subscription(tid, type_id, 1 if type_id == 1 else 4, f"s-{username}", FUTURE, username)
    anchor = (NOW - timedelta(days=anchor_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    sql(path, "UPDATE subscriptions SET bs_period_anchor = ?, bs_state = ?, bs_state_period = ? WHERE id = ?",
        (anchor, state, state_period, sid))
    if paid:
        sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status) "
                  "VALUES (?, ?, 1, 150, 'CONFIRMED')", (f"pay-{username}", tid))
    return sid


def state(path, sid):
    return sql(path, "SELECT bs_state, bs_state_period FROM subscriptions WHERE id = ?", (sid,))[0]


async def test_period_math():
    anchor = (NOW - timedelta(days=65)).strftime("%Y-%m-%d %H:%M:%S")
    start, reset = whitelist.current_period(anchor, NOW)
    assert start == NOW - timedelta(days=5) and reset == NOW + timedelta(days=25), (start, reset)
    start, reset = whitelist.current_period((NOW - timedelta(hours=1)).isoformat(), NOW)
    assert start == NOW - timedelta(hours=1)
    assert whitelist.current_period(None, NOW)[0] == NOW
    assert whitelist.format_gb(50 * GB) == "50" and whitelist.format_gb(int(32.6 * GB)) == "32.6"
    print("OK  период БС: 30 дней от покупки, следующие периоды отсчитываются от той же даты")


async def test_disabled():
    await fresh_db()
    whitelist.config.BS_SQUAD_UUID = BS
    panel = FakePanel()
    assert not await whitelist.is_enabled()
    assert await whitelist.check_all(panel, NOW) is None
    assert await whitelist.squads_for_new_user({"bs_limit_gb": 50}, 1) == []

    whitelist.config.BS_SQUAD_UUID = ""   # сквад не задан — выключено даже при флаге
    await whitelist.set_enabled(True)
    assert not await whitelist.is_enabled()
    whitelist.config.BS_SQUAD_UUID = BS
    print("OK  выключено: проверки нет, новым пользователям сквад не выдаётся, без BS_SQUAD_UUID не включается")


async def test_check_all():
    path = await fresh_db()
    whitelist.config.BS_SQUAD_UUID = BS
    await whitelist.set_enabled(True)
    panel = FakePanel()

    a = await make_sub(path, 1, "a", paid=True)                      # 10 ГБ из 50 -> выдать
    panel.add("a", 101); panel.usage[101] = 10 * GB
    b = await make_sub(path, 2, "b", paid=False)                     # триал: 4 ГБ из 3 -> исчерпан
    panel.add("b", 102); panel.usage[102] = 4 * GB
    c = await make_sub(path, 3, "c", paid=True)                      # в скваде, 60 ГБ из 50 -> снять
    panel.add("c", 103, squads=(STD, BS)); panel.usage[103] = 60 * GB
    d = await make_sub(path, 4, "d", type_id=2, paid=True)           # тариф без БС, но в скваде -> снять
    panel.add("d", 104, squads=(STD, BS))
    e = await make_sub(path, 5, "e", paid=True, state="admin_off")   # отключён админом -> не трогать
    panel.add("e", 105)
    await make_sub(path, 6, "f", paid=True)                          # истекла -> не выдавать
    panel.add("f", 106, expire=PAST)
    await make_sub(path, 7, "g", paid=True)                          # 2 ГБ < мин. лимита -> выдать без точного запроса
    panel.add("g", 107, squads=(STD, ADMIN_SQUAD)); panel.usage[107] = 2 * GB
    panel.add("friend", 999, squads=(STD, ADMIN_SQUAD, BS))          # не пользователь бота -> не трогать

    result = await whitelist.check_all(panel, NOW)
    assert result.added == [101, 107], result.added
    assert result.removed == [103, 104], result.removed
    assert sorted(s["telegram_id"] for s in result.exhausted) == [2, 3]
    assert BS in panel.squads("a") and BS in panel.squads("g") and ADMIN_SQUAD in panel.squads("g")
    assert BS not in panel.squads("b") and BS not in panel.squads("c") and BS not in panel.squads("d")
    assert BS not in panel.squads("e") and BS not in panel.squads("f")
    assert panel.squads("friend") == [STD, ADMIN_SQUAD, BS]
    assert 107 not in panel.exact_calls and 101 in panel.exact_calls
    assert state(path, b)[0] == "exhausted" and state(path, c)[0] == "exhausted"
    assert state(path, e)[0] == "admin_off" and state(path, a)[0] is None
    assert sql(path, "SELECT panel_user_id FROM subscriptions WHERE id = ?", (a,))[0][0] == 101

    bot = FakeBot()
    await whitelist.notify_users(bot, result)
    assert sorted(chat for chat, _ in bot.sent) == [2, 3] and "исчерпан" in bot.sent[0][1]

    # Повторная проверка ничего не меняет и не дублирует уведомления
    again = await whitelist.check_all(panel, NOW)
    assert again.added == [] and again.removed == [] and again.exhausted == [] and again.restored == []

    # Через 30 дней: новый период, трафик в нём маленький -> сквад вернулся, уведомление «снова доступны»
    panel.usage[103] = 1 * GB
    panel.usage[102] = 0
    later = NOW + timedelta(days=30)
    result = await whitelist.check_all(panel, later)
    assert 103 in result.added and 102 in result.added, result.added
    assert sorted(s["telegram_id"] for s in result.restored) == [2, 3]
    assert state(path, c)[0] is None and BS in panel.squads("c")
    print("OK  проверка БС: выдача по тарифу, лимит триала 3 ГБ, снятие за лимит/тариф, admin_off, истёкшие, чужие сквады и не-боты не тронуты, возврат в новом периоде")


async def test_payment_and_admin():
    path = await fresh_db()
    whitelist.config.BS_SQUAD_UUID = BS
    await whitelist.set_enabled(True)
    panel = FakePanel()

    c = await make_sub(path, 3, "c", paid=True, anchor_days_ago=10)
    panel.add("c", 103); panel.usage[103] = 60 * GB
    await whitelist.check_all(panel, NOW)
    assert state(path, c)[0] == "exhausted"

    # Продление: новый период, сквад сразу возвращается
    await whitelist.on_payment(panel, 3, 1)
    assert state(path, c) == (None, None) and BS in panel.squads("c")
    anchor = sql(path, "SELECT bs_period_anchor FROM subscriptions WHERE id = ?", (c,))[0][0]
    assert anchor and datetime.fromisoformat(anchor).replace(tzinfo=timezone.utc) > NOW - timedelta(days=1)

    # Админ отключил БС — проверка не возвращает, оплата тоже не отменяет решение админа
    sub = await db.get_subscription(c)
    assert await whitelist.admin_set_user_squad(panel, sub, BS, False)
    assert state(path, c)[0] == "admin_off" and BS not in panel.squads("c")
    panel.usage[103] = 0
    await whitelist.check_all(panel, NOW)
    assert BS not in panel.squads("c")
    await whitelist.on_payment(panel, 3, 1)
    assert state(path, c)[0] == "admin_off" and BS not in panel.squads("c")

    # Админ включил вручную — без лимита до конца периода
    sub = await db.get_subscription(c)
    assert await whitelist.admin_set_user_squad(panel, sub, BS, True)
    assert state(path, c)[0] == "admin_on"
    panel.usage[103] = 500 * GB
    result = await whitelist.check_all(panel, datetime.now(timezone.utc))
    assert BS in panel.squads("c") and result.removed == []

    # Другие сквады админ переключает без изменения состояния БС
    assert await whitelist.admin_set_user_squad(panel, sub, ADMIN_SQUAD, True)
    assert ADMIN_SQUAD in panel.squads("c") and state(path, c)[0] == "admin_on"

    # Новый пользователь: сквад БС по тарифу (триал — по лимиту 3 ГБ), тариф без БС — без сквада
    assert await whitelist.squads_for_new_user({"bs_limit_gb": 50}, 3) == [BS]
    assert await whitelist.squads_for_new_user({"bs_limit_gb": None}, 3) == []
    assert await whitelist.squads_for_new_user({"bs_limit_gb": None}, 777) == [BS]   # не платил: 3 ГБ

    # Отключить у всех: снимается у всех, у кого есть (и у не-ботов), другие сквады остаются
    panel.add("friend", 999, squads=(ADMIN_SQUAD, BS))
    removed = await whitelist.disable_for_all(panel)
    assert removed == 2 and BS not in panel.squads("c") and panel.squads("friend") == [ADMIN_SQUAD]
    assert not await whitelist.is_enabled()
    print("OK  БС: оплата начинает новый период, admin_off/admin_on, переключение других сквадов, выдача новым, «отключить у всех»")


async def test_status_text():
    path = await fresh_db()
    whitelist.config.BS_SQUAD_UUID = BS
    await whitelist.set_enabled(True)
    panel = FakePanel()
    sid = await make_sub(path, 1, "a", paid=True)
    panel.add("a", 101); panel.usage[101] = int(17.4 * GB)

    status = await whitelist.get_status(panel, await db.get_subscription(sid))
    assert status.limit == 50 * GB and status.remaining == int(50 * GB) - int(17.4 * GB)
    assert "осталось <b>32.6 из 50 ГБ</b>" in whitelist.status_text(status), whitelist.status_text(status)

    panel.usage[101] = 51 * GB
    status = await whitelist.get_status(panel, await db.get_subscription(sid))
    assert "исчерпан" in whitelist.status_text(status)

    sql(path, "UPDATE tariff_types SET bs_limit_gb = NULL WHERE id = 1")
    assert await whitelist.get_status(panel, await db.get_subscription(sid)) is None
    await whitelist.set_enabled(False)
    sql(path, "UPDATE tariff_types SET bs_limit_gb = 50 WHERE id = 1")
    assert await whitelist.get_status(panel, await db.get_subscription(sid)) is None
    print("OK  карточка подписки: «осталось X из Y ГБ», исчерпан, не показывается без БС в тарифе или при выключенных БС")


async def test_migration_anchor():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "INSERT INTO users (telegram_id) VALUES (1), (2)")
    paid = await db.create_subscription(1, 1, 1, "s1", FUTURE, "u1")
    unpaid = await db.create_subscription(2, 1, 1, "s2", FUTURE, "u2")
    sql(path, "UPDATE subscriptions SET created_at = '2026-09-01 00:00:00'")
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, activated_at) "
              "VALUES ('p1', 1, 1, 150, 'CONFIRMED', '2026-10-05 10:00:00'), "
              "('p0', 1, 1, 150, 'CONFIRMED', '2026-09-05 10:00:00'), "
              "('px', 1, 1, 150, 'CANCELED', '2026-10-10 10:00:00')")
    # «Боевая» БД v2.3.0: колонок БС ещё нет, user_version = 5
    conn = sqlite3.connect(path)
    for column in ("bs_period_anchor", "bs_state", "bs_state_period", "panel_user_id"):
        conn.execute(f"ALTER TABLE subscriptions DROP COLUMN {column}")
    conn.execute("ALTER TABLE tariff_types DROP COLUMN bs_limit_gb")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    await db.init_db()
    anchors = dict(sql(path, "SELECT id, bs_period_anchor FROM subscriptions"))
    assert anchors[paid] == "2026-10-05 10:00:00" and anchors[unpaid] == "2026-09-01 00:00:00", anchors
    assert sql(path, "PRAGMA user_version")[0][0] == len(db._MIGRATIONS)
    print("OK  миграция БС: период от последней оплаты подписки этого типа, без оплат — от создания")


async def main():
    await test_period_math()
    await test_disabled()
    await test_check_all()
    await test_payment_and_admin()
    await test_status_text()
    await test_migration_anchor()


if __name__ == "__main__":
    asyncio.run(main())
