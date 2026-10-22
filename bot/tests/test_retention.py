"""Тесты возврата клиентов: сегменты, окна, подарки раз в 3 месяца, лимит сообщений, тихие часы, блокировка."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db, retention_db  # noqa: E402
from services import retention, whitelist  # noqa: E402

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


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakePanel:
    def __init__(self):
        self.used = {}
        self.extended = []

    async def extend_subscription_by_username(self, username, days, traffic_limit_bytes=None):
        self.extended.append((username, days))
        return iso(NOW + timedelta(days=days))

    async def get_used_traffic_bytes(self, username):
        return self.used.get(username, 0)

    async def get_user_by_username(self, username):
        return {"id": 1, "username": username, "activeInternalSquads": []}


class FakeBot:
    def __init__(self, blocked=()):
        self.sent = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise Exception("Telegram server says - Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "UPDATE tariff_types SET bs_limit_gb = 30 WHERE id = 1")   # Standart: 1 ГБ в день
    sql(path, "UPDATE tariff_types SET bs_limit_gb = 60 WHERE id = 2")   # Family: 2 ГБ в день
    await retention.set_enabled(True)
    return path


def user(path, tid, created_ago=timedelta(days=30)):
    sql(path, "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)", (tid, ts(NOW - created_ago)))


async def sub(path, tid, expiry, trial=False, created_ago=timedelta(days=10), paid=False, type_id=1):
    """paid — у подписки были оплаченные дни (bs_paid_until), т. е. это платная подписка"""
    sid = await db.create_subscription(tid, type_id, 1 if type_id == 1 else 4, f"s{tid}-{type_id}",
                                       iso(expiry), f"tg{tid}_{type_id}", trial=trial)
    sql(path, "UPDATE subscriptions SET created_at = ?, bs_paid_until = ? WHERE id = ?",
        (ts(NOW - created_ago), ts(expiry) if paid else None, sid))
    return sid


def pay(path, tid, when=None):
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, created_at) "
              "VALUES (?, ?, 1, 150, 'CONFIRMED', ?)", (f"p{tid}-{when}", tid, ts(when or NOW - timedelta(days=60))))


async def run(bot, panel, at):
    return await retention.run(bot, panel, now=at, ignore_hours=True)


async def test_switch_and_hours():
    path = await fresh_db()
    user(path, 1, created_ago=timedelta(days=1, hours=2))
    bot, panel = FakeBot(), FakePanel()
    await retention.set_enabled(False)
    assert await run(bot, panel, NOW) is None and bot.sent == []
    await retention.set_enabled(True)
    night = datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc)      # 03:30 МСК
    day = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)         # 12:00 МСК
    assert not retention.is_send_time(night) and retention.is_send_time(day)
    assert await retention.run(bot, panel, now=night) is None and bot.sent == []
    print("OK  выключатель и тихие часы: выключено/ночью ничего не отправляется")


async def test_start_only_chain():
    path = await fresh_db()
    user(path, 1, created_ago=timedelta(days=1, hours=2))
    user(path, 2, created_ago=timedelta(days=40))                   # давно — не трогаем
    bot, panel = FakeBot(), FakePanel()

    assert await run(bot, panel, NOW) == {"start_only:d1": 1}
    assert [c for c, _ in bot.sent] == [1] and "3 дня бесплатно" in bot.sent[0][1]
    assert await run(bot, panel, NOW + timedelta(hours=2)) == {}    # не чаще раза в сутки и не повторяется
    assert await run(bot, panel, NOW + timedelta(days=1)) == {}     # до 3-го дня — тишина
    assert await run(bot, panel, NOW + timedelta(days=2)) == {"start_only:d3": 1}
    assert await run(bot, panel, NOW + timedelta(days=6)) == {"start_only:d7": 1}
    assert await run(bot, panel, NOW + timedelta(days=13)) == {}    # цепочка закончилась
    assert len(bot.sent) == 3
    print("OK  только /start: 1 / 3 / 7 день, по разу, давно зарегистрированных не трогаем")


async def test_trial_idle():
    path = await fresh_db()
    for tid in (1, 2, 3):
        user(path, tid)
    await sub(path, 1, NOW + timedelta(days=2), trial=True, created_ago=timedelta(days=1, hours=1))
    await sub(path, 2, NOW + timedelta(days=2), trial=True, created_ago=timedelta(days=1, hours=1))
    await sub(path, 3, NOW + timedelta(days=2), trial=True, created_ago=timedelta(days=1, hours=1))
    pay(path, 3)
    bot, panel = FakeBot(), FakePanel()
    panel.used["tg2_1"] = 200 * 1024 * 1024                        # подключился

    assert await run(bot, panel, NOW) == {"trial_idle:help": 1}
    assert [c for c, _ in bot.sent] == [1] and "Happ" in bot.sent[0][1]
    assert await run(bot, panel, NOW + timedelta(hours=24)) == {"trial_idle:help": 1}   # 2-й день триала
    assert "последний день" in bot.sent[-1][1]
    print("OK  триал без подключения: помощь на 1-й и 2-й день; подключившимся и платившим не пишем")


async def test_trial_expired_promo_then_gift():
    path = await fresh_db()
    user(path, 1)
    sid = await sub(path, 1, NOW - timedelta(hours=30), trial=True, created_ago=timedelta(days=4))
    bot, panel = FakeBot(), FakePanel()

    assert await run(bot, panel, NOW) == {"trial_expired:promo": 1}
    assert "Пробный период закончился" in bot.sent[-1][1] and "150 ₽" in bot.sent[-1][1]
    assert await run(bot, panel, NOW + timedelta(hours=10)) == {}   # сутки не прошли
    assert await run(bot, panel, NOW + timedelta(hours=21)) == {"trial_expired:gift": 1}
    assert panel.extended == [("tg1_1", 3)]
    s = await db.get_subscription(sid)
    assert s["status"] == "ACTIVE" and s["subscription_expiry"] == iso(NOW + timedelta(days=3))
    assert s["bs_bonus_bytes"] == 3 * whitelist.GB
    assert "Дарим ещё 3 дня" in bot.sent[-1][1]

    # Подарок кончился, снова не купил: рассказ о подписке ещё раз, но подарка нет — не прошло 3 месяца
    later = NOW + timedelta(days=3, hours=30)
    sql(path, "UPDATE subscriptions SET subscription_expiry = ? WHERE id = ?", (iso(later - timedelta(hours=30)), sid))
    assert await run(bot, panel, later) == {"trial_expired:promo": 1}
    assert await run(bot, panel, later + timedelta(hours=21)) == {}
    assert len(panel.extended) == 1

    # Через 3 месяца подарок снова возможен
    sql(path, "UPDATE retention_events SET created_at = datetime(created_at, '-91 days') WHERE step = 'gift'")
    assert await run(bot, panel, later + timedelta(hours=22)) == {"trial_expired:gift": 1}
    print("OK  триал истёк: через 24 ч рассказ, ещё через 24 ч +3 дня и +3 ГБ, подарок не чаще раза в 3 месяца")


async def test_winback():
    path = await fresh_db()
    user(path, 1)
    user(path, 2)
    pay(path, 1)
    pay(path, 2)
    sid = await sub(path, 1, NOW - timedelta(hours=26), paid=True)
    await sub(path, 2, NOW - timedelta(days=10), paid=True)         # ушёл давно — не трогаем
    bot, panel = FakeBot(), FakePanel()

    assert await run(bot, panel, NOW) == {"winback:gift": 1}
    assert panel.extended == [("tg1_1", 3)] and "Мы подарили тебе 3 дня" in bot.sent[-1][1]
    assert (await db.get_subscription(sid))["status"] == "ACTIVE"

    # Подарок кончился, через сутки — подарка нет (3 месяца не прошло)
    later = NOW + timedelta(days=4, hours=2)
    sql(path, "UPDATE subscriptions SET subscription_expiry = ? WHERE id = ?", (iso(later - timedelta(hours=26)), sid))
    assert await run(bot, panel, later) == {}
    assert len(panel.extended) == 1
    print("OK  не продлил платную: через 24 ч +3 дня и +3 ГБ, не чаще раза в 3 месяца, давно ушедших не трогаем")


async def test_gift_on_other_tariff():
    """Подписка на Family (не базовый тариф): подарочные дни Standart пересчитываются по коэффициенту"""
    path = await fresh_db()
    user(path, 1)
    pay(path, 1)
    family = await sub(path, 1, NOW - timedelta(hours=26), paid=True, type_id=2)           # истекла сутки назад
    bot, panel = FakeBot(), FakePanel()

    assert await run(bot, panel, NOW) == {"winback:gift": 1}
    username, days = panel.extended[-1]
    assert username == "tg1_2" and days == 2                                                 # 3 дня Standart → 1.5 → 2 Family
    assert "Premium" in bot.sent[-1][1] and "Мы подарили тебе 2 дня" in bot.sent[-1][1]
    assert (await db.get_subscription(family))["bs_bonus_bytes"] == 4 * whitelist.GB         # 2 дня × 60 / 30

    # Подарок по подписке — не чаще раза в 3 месяца
    later = NOW + timedelta(days=4, hours=2)
    sql(path, "UPDATE subscriptions SET subscription_expiry = ? WHERE id = ?", (iso(later - timedelta(hours=26)), family))
    assert await run(bot, panel, later) == {}
    print("OK  подарок на тарифе Family: дни пересчитаны по коэффициенту и округлены вверх (3 → 1.5 → 2), ГБ за эти дни (2 × 2 = 4), раз в 3 месяца")


async def test_blocked_and_stats():
    path = await fresh_db()
    user(path, 1, created_ago=timedelta(days=1, hours=2))
    user(path, 2, created_ago=timedelta(days=1, hours=2))
    bot, panel = FakeBot(blocked={2}), FakePanel()

    await run(bot, panel, NOW)
    assert sql(path, "SELECT bot_blocked_at IS NOT NULL FROM users WHERE telegram_id = 2")[0][0] == 1
    audience = {u["telegram_id"] for u in await retention_db.get_audience()}
    assert 2 not in audience and 1 in audience
    await retention_db.set_bot_blocked(2, False)                     # снова нажал /start
    assert 2 in {u["telegram_id"] for u in await retention_db.get_audience()}

    pay(path, 1, when=NOW + timedelta(days=1))
    stats = await retention_db.stats()
    assert stats["start_only"]["users"] == 2 and stats["start_only"]["paid"] == 1, stats

    preview_bot = FakeBot()
    await retention.send_previews(preview_bot, 999)
    assert len(preview_bot.sent) == 8 and all("{" not in t for _, t in preview_bot.sent)
    print("OK  заблокировавшим бота не пишем (до нового /start), статистика с оплатами, примеры всех 8 сообщений")


async def main():
    await test_switch_and_hours()
    await test_start_only_chain()
    await test_trial_idle()
    await test_trial_expired_promo_then_gift()
    await test_winback()
    await test_gift_on_other_tariff()
    await test_blocked_and_stats()


if __name__ == "__main__":
    asyncio.run(main())
