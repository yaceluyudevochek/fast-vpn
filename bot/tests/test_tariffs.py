"""Тесты «одна подписка на аккаунт»: смена тарифа с пересчётом, покупка другого тарифа, бонусные дни, архив."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from services import payment_webhook as pw  # noqa: E402
from services import tariffs, whitelist  # noqa: E402

STD_SQUAD, FAM_SQUAD, CORP_SQUAD, BS, ADMIN_SQUAD = "std", "fam", "corp", "bs", "admin-extra"
DAY = timedelta(days=1)


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


class FakePanel:
    def __init__(self):
        self.users = {}
        self.updates = []
        self.resets = []
        self.extended = []

    def add(self, username, squads):
        self.users[username] = {"id": len(self.users) + 1, "username": username,
                                "activeInternalSquads": [{"uuid": s} for s in squads]}

    def squads(self, username):
        return [s["uuid"] for s in self.users[username]["activeInternalSquads"]]

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def update_user(self, username, **fields):
        self.updates.append((username, fields))
        if "activeInternalSquads" in fields:
            self.users[username]["activeInternalSquads"] = [{"uuid": s} for s in fields["activeInternalSquads"]]
        return True

    async def reset_user_hwid_devices_by_username(self, username):
        self.resets.append(username)
        return True

    async def extend_subscription_by_username(self, username, days, traffic_limit_bytes=None):
        self.extended.append((username, days))
        return "2099-01-01T00:00:00.000Z"

    async def update_user_status_by_username(self, *a, **k):
        return True


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    # Standart 200 ₽ / 3 устр. / 50 ГБ; Family 300 / 5 / 75; Corporate 500 / 10 / 150
    sql(path, "UPDATE tariff_types SET name='Standart', squad_id=?, hwid_limit=3, bs_limit_gb=50 WHERE id=1", (STD_SQUAD,))
    sql(path, "UPDATE tariff_plans SET price = 200 WHERE tariff_type_id = 1 AND days = 30")
    sql(path, "UPDATE tariff_types SET name='Family', squad_id=?, hwid_limit=5, bs_limit_gb=75, switch_coef=300 WHERE id=2",
        (FAM_SQUAD,))
    sql(path, "INSERT INTO tariff_types (id, name, squad_id, hwid_limit, bs_limit_gb, switch_coef) "
              "VALUES (3, 'Corporate', ?, 10, 150, 500)", (CORP_SQUAD,))
    sql(path, "INSERT INTO tariff_plans (tariff_type_id, days, price) VALUES (3, 30, 500)")
    sql(path, "INSERT INTO users (telegram_id) VALUES (1)")
    return path


async def std_sub(path, panel, remaining=15 * DAY, paid=15 * DAY, now=None):
    now = now or datetime.now(timezone.utc)
    sid = await db.create_subscription(1, 1, 1, "s1", tariffs.iso_z(now + remaining), "tg1_1")
    sql(path, "UPDATE subscriptions SET bs_paid_until = ? WHERE id = ?",
        (tariffs.sql_ts(now + paid) if paid else None, sid))
    panel.add("tg1_1", [STD_SQUAD, BS, ADMIN_SQUAD])
    return sid


def remaining(sub, now):
    return datetime.fromisoformat(sub["subscription_expiry"].replace("Z", "+00:00")) - now


async def test_formula_and_switch():
    path = await fresh_db()
    panel = FakePanel()
    now = datetime.now(timezone.utc)
    sid = await std_sub(path, panel, remaining=15 * DAY, paid=12 * DAY, now=now)
    sub = await db.get_subscription(sid)

    pv = await tariffs.preview(sub, 2, now)
    assert tariffs.days_text(pv.remaining_old) == "15 дней" and tariffs.days_text(pv.remaining_new) == "10 дней"
    assert tariffs.days_text((await tariffs.preview(sub, 3, now)).remaining_new) == "6 дней"   # 15 × 200 / 500
    assert not pv.devices_reset

    updated = await tariffs.switch(panel, sid, 2, now)
    assert updated["tariff_type_id"] == 2 and updated["tariff_name"] == "Family" and updated["tariff_hwid_limit"] == 5
    assert abs(remaining(updated, now) - 10 * DAY) < timedelta(seconds=2)
    paid_until = datetime.fromisoformat(updated["bs_paid_until"]).replace(tzinfo=timezone.utc)
    assert abs(paid_until - (now + 8 * DAY)) < timedelta(seconds=2)                          # оплаченные 12 → 8
    assert panel.squads("tg1_1") == [FAM_SQUAD, BS, ADMIN_SQUAD]                              # БС и админский — на месте
    fields = panel.updates[-1][1]
    assert fields["hwidDeviceLimit"] == 5 and "expireAt" in fields and panel.resets == []
    assert updated["tariff_changed_at"]
    assert whitelist.get_quota(updated).limit == 75 * whitelist.GB                            # лимит БС нового тарифа

    # Раз в сутки
    allowed, message = tariffs.can_switch(updated)
    assert not allowed and "через" in message
    assert tariffs.can_switch(updated, now + 25 * 3600 * timedelta(seconds=1))[0]
    print("OK  смена тарифа: 15 дн. Standart → 10 Family / 6 Corporate, оплаченные дни отдельно, сквад тарифа заменён, "
          "БС и админский сохранены, лимит устройств и БС нового тарифа, не чаще раза в сутки")


async def test_round_trip_and_downgrade():
    path = await fresh_db()
    panel = FakePanel()
    now = datetime.now(timezone.utc)
    sid = await std_sub(path, panel, remaining=2 * DAY, paid=2 * DAY, now=now)
    sub = await tariffs.switch(panel, sid, 3, now)                       # 2 дня Standart → 0.8 дня Corporate
    assert abs(remaining(sub, now) - 0.8 * DAY) < timedelta(seconds=2)
    sub = await tariffs.switch(panel, sid, 1, now)                       # обратно → снова 2 дня, без выигрыша
    assert abs(remaining(sub, now) - 2 * DAY) < timedelta(seconds=2)
    assert panel.resets == ["tg1_1"]                                     # Corporate → Standart: 10 → 3 устройства
    assert (await tariffs.preview(await db.get_subscription(sid), 3, now)).devices_reset is False

    # Истёкшая подписка: остаток 0 — меняется только тариф, срок не трогаем
    sql(path, "UPDATE subscriptions SET subscription_expiry = ? WHERE id = ?", (tariffs.iso_z(now - DAY), sid))
    sub = await tariffs.switch(panel, sid, 2, now)
    assert sub["tariff_type_id"] == 2 and "expireAt" not in panel.updates[-1][1]
    print("OK  туда-обратно без выигрыша дней (точность до секунды), понижение сбрасывает устройства, истёкшая — без срока")


async def test_payment_for_other_tariff():
    path = await fresh_db()
    panel = FakePanel()
    sid = await std_sub(path, panel, remaining=15 * DAY, paid=15 * DAY)
    pw.panel_api = panel

    async def noop(*a, **k):
        return None
    pw.notify_user_success = noop
    pw.notify_admin_error = noop

    family_plan = (await db.get_tariff_plans(2))[0]
    tx = {"transaction_id": "t1", "telegram_id": 1, "plan_id": family_plan["id"], "payload": f"1_{family_plan['id']}_1"}
    assert await pw.handle_successful_payment(tx) is True
    subs = await db.get_user_subscriptions(1)
    assert len(subs) == 1 and subs[0]["tariff_type_id"] == 2                   # та же подписка, новый тариф
    assert panel.extended == [("tg1_1", family_plan["days"])]                  # и продлена на купленный план
    print("OK  покупка плана другого тарифа: та же подписка переводится на него (остаток пересчитан) и продлевается")


async def test_bonus_days_and_archive():
    path = await fresh_db()
    panel = FakePanel()
    sid = await std_sub(path, panel)
    assert await tariffs.bonus_days(await db.get_subscription(sid), 7) == 7
    await tariffs.switch(panel, sid, 2)
    assert await tariffs.bonus_days(await db.get_subscription(sid), 7) == 5          # 4.67 → вверх до 5
    sql(path, "UPDATE tariff_types SET switch_coef = 350 WHERE id = 2")
    assert await tariffs.bonus_days(await db.get_subscription(sid), 7) == 4          # 7 × 200 / 350 = 4.0 ровно
    sql(path, "UPDATE tariff_types SET switch_coef = 300 WHERE id = 2")
    # На экране смены тарифа — вниз: 7.5 дня показываем как «7 дней» (реально начислится 7.5)
    assert tariffs.days_text(timedelta(days=7.5)) == "7 дней" and tariffs.days_text(timedelta(days=21)) == "21 день"
    assert tariffs.days_text(timedelta(hours=5)) == "меньше дня" and tariffs.plural_days(2) == "2 дня"
    assert await tariffs.bonus_days(None, 7) == 7

    # Миграция: у кого несколько подписок — оставляем на активном тарифе с самым поздним сроком, остальные в архив
    path2 = os.path.join(tempfile.mkdtemp(), "old.db")
    db.DB_PATH = path2
    await db.init_db()
    sql(path2, "INSERT INTO users (telegram_id) VALUES (5), (6)")
    keep = await db.create_subscription(5, 1, 1, "a", "2026-10-01T00:00:00.000Z", "tg5_1")
    sql(path2, "INSERT INTO tariff_types (id, name, squad_id, is_active) VALUES (4, 'Unblocker', 'wl', 0)")
    old = await db.create_subscription(5, 4, 1, "b", "2026-12-01T00:00:00.000Z", "tg5_4")   # позже, но тариф выключен
    single = await db.create_subscription(6, 1, 1, "c", "2026-10-01T00:00:00.000Z", "tg6_1")
    sql(path2, "PRAGMA user_version = 12")
    await db.init_db()
    assert [r[0] for r in sql(path2, "SELECT id FROM subscriptions ORDER BY id")] == [keep, single]
    assert [r[0] for r in sql(path2, "SELECT id FROM subscriptions_archive")] == [old]
    assert (await db.get_user_main_subscription(5))["id"] == keep
    print("OK  бонусные дни Standart → тариф подписки с округлением вверх, экран смены — вниз; лишние подписки уходят в архив")


async def test_referral_to_family():
    from services import referrals
    path = await fresh_db()
    panel = FakePanel()
    sid = await std_sub(path, panel)
    await tariffs.switch(panel, sid, 2)
    panel.extended.clear()

    class Bot:
        sent = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append(text)
    bot = Bot()
    assert await referrals.grant(bot, panel, 1, "Друг") == sid
    assert len(await db.get_user_subscriptions(1)) == 1                       # новая подписка не создана
    username, days = panel.extended[0]
    assert username == "tg1_1" and days == 5                                   # 7 дней Standart → 4.67 → 5 Family
    assert "+5 дней тарифа Family" in bot.sent[-1], bot.sent[-1]
    print("OK  награда за друга идёт на единственную подписку (Family), дни пересчитаны по коэффициенту")


async def test_bs_limit_upgrade_next_period():
    """«Съездить на день в Corporate» ради лимита белых списков нельзя: больший лимит — со следующего периода"""
    path = await fresh_db()
    panel = FakePanel()
    whitelist.config.BS_SQUAD_UUID = "bs"
    await whitelist.set_enabled(True)
    now = datetime.now(timezone.utc)
    sid = await std_sub(path, panel, remaining=100 * DAY, paid=100 * DAY, now=now)   # Corporate: 40 оплаченных дней
    sql(path, "UPDATE subscriptions SET bs_period_anchor = ? WHERE id = ?", (tariffs.sql_ts(now - 5 * DAY), sid))
    sub = await db.get_subscription(sid)
    assert await whitelist._sync_quota(panel, sub, whitelist.get_quota(sub, now), 1)        # период запомнен: 50 ГБ

    sub = await tariffs.switch(panel, sid, 3, now)                                           # Standart → Corporate
    q = whitelist.get_quota(sub, now)
    assert q.limit == 50 * whitelist.GB and q.upcoming_limit == 150 * whitelist.GB
    status = whitelist.BsStatus(quota=q, used=10 * whitelist.GB, state=None)
    assert "Лимит нового тарифа 150 ГБ начнёт действовать" in whitelist.status_text(status)

    later = now + 26 * DAY                                                                   # новый 30-дневный период
    q2 = whitelist.get_quota(sub, later)
    assert q2.limit == 150 * whitelist.GB and q2.upcoming_limit is None

    sub = await tariffs.switch(panel, sid, 1, now)                                           # обратно на Standart
    assert whitelist.get_quota(sub, now).limit == 50 * whitelist.GB                          # меньший — сразу
    from handlers.admin_promo import _scope_warning
    family_plans = [p["id"] for p in await db.get_tariff_plans(2)]
    all_plans = [p["id"] for t in (1, 2, 3) for p in await db.get_tariff_plans(t)]
    warning = await _scope_warning(family_plans)
    assert "Standart" in warning and "Corporate" in warning and "Family" not in warning.split("не действует на:")[1].split(".")[0]
    assert await _scope_warning(all_plans) == "" and await _scope_warning([]) == ""
    await whitelist.set_enabled(False)
    print("OK  повышение тарифа: больший лимит БС — со следующего периода, понижение — сразу; "
          "мастер промокодов предупреждает, если код не на все тарифы")


async def main():
    await test_formula_and_switch()
    await test_round_trip_and_downgrade()
    await test_payment_for_other_tariff()
    await test_bonus_days_and_archive()
    await test_referral_to_family()
    await test_bs_limit_upgrade_next_period()


if __name__ == "__main__":
    asyncio.run(main())
