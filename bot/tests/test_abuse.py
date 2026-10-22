"""Тесты оповещений о «плохишах»: трафик, общие HWID/IP, игнор, поиск по username."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import abuse_db, db  # noqa: E402
from services import abuse  # noqa: E402

GB = abuse.GB
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
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
        self.users = []
        self.daily = {}       # (username, date) -> байты
        self.devices = []

    def add_user(self, tid, panel_id, suffix=1):
        self.users.append({"id": panel_id, "username": f"tg{tid}_{suffix}", "telegramId": tid})

    async def get_all_users(self):
        return self.users

    async def get_users_traffic(self, start, end, limit=100):
        totals = {}
        for (username, day), value in self.daily.items():
            if start <= day <= end:
                totals[username] = totals.get(username, 0) + value
        return [{"username": u, "total": t} for u, t in sorted(totals.items(), key=lambda x: -x[1])]

    async def get_all_hwid_devices(self):
        return self.devices


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "INSERT INTO users (telegram_id, username, role) VALUES (?, 'boss', 'admin')", (ADMIN,))
    return path


def user(path, tid, username=None, paid=False, trial=False):
    sql(path, "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)", (tid, username))
    if paid:
        sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status) "
                  "VALUES (?, ?, 1, 150, 'CONFIRMED')", (f"p{tid}", tid))
    if trial:
        sql(path, "INSERT INTO subscriptions (telegram_id, tariff_type_id, tariff_plan_id, username_in_panel, trial_used) "
                  "VALUES (?, 1, 1, ?, 1)", (tid, f"tg{tid}_1"))


async def test_traffic():
    path = await fresh_db()
    panel = FakePanel()
    today, yesterday = NOW.date().isoformat(), (NOW.date() - timedelta(days=1)).isoformat()
    for tid, pid in ((101, 1), (102, 2), (103, 3), (104, 4), (ADMIN, 5)):
        user(path, tid, username=f"user{tid}")
        panel.add_user(tid, pid)
    panel.daily[("tg101_1", today)] = 60 * GB                         # сутки — сегодня
    panel.daily[("tg104_1", yesterday)] = 55 * GB                     # сутки — вчера
    for i in range(7):                                                # 35 ГБ/день = 245 за неделю
        panel.daily[("tg102_1", (NOW.date() - timedelta(days=i)).isoformat())] = 35 * GB
    panel.daily[("tg103_1", today)] = 20 * GB                         # норма
    panel.daily[(f"tg{ADMIN}_1", today)] = 90 * GB                    # админ — не сообщаем
    bot = FakeBot()

    assert await abuse.check_traffic(bot, panel, NOW) == 3
    texts = {t for _, t, _ in bot.sent}
    assert all(chat == ADMIN for chat, _, _ in bot.sent)
    assert any("101" in t and "60.0 ГБ" in t for t in texts)
    assert any("104" in t and "55.0 ГБ" in t for t in texts)
    assert any("102" in t and "245.0 ГБ</b> за 7 дней" in t for t in texts)
    assert not any("103" in t for t in texts) and not any(f"<code>{ADMIN}</code>" in t for t in texts)
    assert bot.sent[0][2].inline_keyboard[0][0].callback_data.startswith("abuse_ignore_")

    assert await abuse.check_traffic(bot, panel, NOW) == 0            # повторно не сообщаем
    panel.daily[("tg101_1", (NOW.date() + timedelta(days=1)).isoformat())] = 70 * GB
    await abuse_db.set_ignored(104, True)
    assert await abuse.check_traffic(bot, panel, NOW + timedelta(days=1)) == 1   # новый день — снова; 104 в игноре
    print("OK  трафик: 50 ГБ/сутки (сегодня и вчера), 200 ГБ/неделя, все админам, без повторов, админы и игнор не попадают")


async def test_devices():
    path = await fresh_db()
    panel = FakePanel()
    for tid, pid, paid, trial in ((201, 1, False, True), (202, 2, False, True), (203, 3, True, False),
                                  (204, 4, False, True), (205, 5, False, True), (206, 6, False, True)):
        user(path, tid, username=f"u{tid}", paid=paid, trial=trial)
        panel.add_user(tid, pid)
    panel.add_user(201, 11, suffix=2)          # у 201 вторая подписка в панели — тот же человек
    panel.add_user(ADMIN, 12)
    panel.devices = [
        {"hwid": "H1", "userId": 1, "deviceModel": "iPhone 15", "platform": "iOS", "requestIp": "10.0.0.1"},
        {"hwid": "H1", "userId": 2, "requestIp": "10.0.0.1"},               # H1: 201 + 202
        {"hwid": "H2", "userId": 1, "requestIp": "10.0.0.2"},
        {"hwid": "H2", "userId": 11, "requestIp": "10.0.0.2"},              # H2: только 201 (две подписки) — не группа
        {"hwid": "H3", "userId": 3, "requestIp": "10.0.0.3"},
        {"hwid": "H3", "userId": 12, "requestIp": "10.0.0.3"},              # H3: 203 + админ — не группа
        {"hwid": "H4", "userId": 4, "requestIp": "77.0.0.1"},
        {"hwid": "H5", "userId": 5, "requestIp": "77.0.0.1"},               # IP 77.0.0.1: 204 + 205 не платили
    ]
    bot = FakeBot()

    assert await abuse.check_devices(bot, panel) == 2
    assert len(bot.sent) == 1 and "первая проверка" in bot.sent[0][1]      # первый запуск — одна сводка
    assert "iPhone 15" in bot.sent[0][1] and "77.0.0.1" in bot.sent[0][1]
    assert await abuse.check_devices(bot, panel) == 0

    # В группу H1 добавился аккаунт — отдельное оповещение с кнопками игнора
    panel.devices.append({"hwid": "H1", "userId": 6, "requestIp": "10.9.9.9"})
    assert await abuse.check_devices(bot, panel) == 1
    text, kb = bot.sent[-1][1], bot.sent[-1][2]
    assert "Одно устройство" in text and "206" in text and len(kb.inline_keyboard) == 3

    # Админ игнорирует одного из группы — остальным повторно не сообщаем
    await abuse_db.set_ignored(206, True)
    assert await abuse.check_devices(bot, panel) == 0

    # IP: большая группа (оператор) и группа, где не платил только один, — не сообщаем
    for i in range(7):
        panel.devices.append({"hwid": f"M{i}", "userId": [1, 2, 4, 5, 6, 3, 11][i], "requestIp": "100.64.0.1"})
    panel.devices.append({"hwid": "P1", "userId": 3, "requestIp": "88.0.0.1"})
    panel.devices.append({"hwid": "P2", "userId": 4, "requestIp": "88.0.0.1"})   # 203 платил + 204 не платил
    assert await abuse.check_devices(bot, panel) == 0
    print("OK  устройства: общий HWID у разных аккаунтов, IP только для 2–5 неплативших, первая проверка — сводкой, "
          "новые — с кнопками, игнор без повторов")


async def test_lookup():
    path = await fresh_db()
    await db.add_user(301, "OldName")
    await db.add_user(301, "NewName")                                    # сменил username — обновили при /start
    assert [u["telegram_id"] for u in await abuse_db.find_users("@newname")] == [301]
    assert await abuse_db.find_users("OldName") == []
    assert [u["telegram_id"] for u in await abuse_db.find_users("tg301_2")] == [301]
    assert [u["telegram_id"] for u in await abuse_db.find_users("301")] == [301]
    assert await abuse_db.find_users("nobody") == []
    print("OK  поиск: по @username без учёта регистра, по имени из панели tg<ID>_N и по ID; username обновляется при /start")


async def test_referral_links():
    """В оповещении видно, кто кого привёл; приглашение внутри группы помечено"""
    path = await fresh_db()
    panel = FakePanel()
    user(path, 401, username="farmer", trial=True)
    user(path, 402, username="alt1", trial=True)
    user(path, 403, username="stranger", trial=True)
    user(path, 499, username="outsider", paid=True)
    sql(path, "UPDATE users SET referred_by = 401 WHERE telegram_id = 402")      # 401 привёл 402 (оба в группе)
    sql(path, "UPDATE users SET referred_by = 499 WHERE telegram_id = 401")      # 401 пришёл от 499 (вне группы)
    sql(path, "UPDATE users SET referred_by = 401 WHERE telegram_id = 403")      # 401 привёл и 403 (вне группы)
    for tid, pid in ((401, 1), (402, 2), (403, 3), (499, 9)):
        panel.add_user(tid, pid)
    panel.devices = [{"hwid": "X", "userId": 1, "requestIp": "1.1.1.1"},
                     {"hwid": "X", "userId": 2, "requestIp": "1.1.1.1"}]
    bot = FakeBot()

    assert await abuse.send_current_groups(bot, panel, ADMIN) == 1
    text = bot.sent[-1][1]
    assert "пришёл по ссылке <code>401</code> ⚠️ участник этой группы" in text, text
    assert "пришёл по ссылке <code>499</code>\n" in text and "499</code> ⚠️" not in text
    assert "привёл по своей ссылке: 2, из них в этой группе: <code>402</code>" in text
    assert await abuse_db.was_alerted("hwid:x") is False                        # сводка ничего не отмечает
    assert await abuse.check_devices(bot, panel) == 1                           # обычная проверка всё равно сообщит
    assert sorted(await abuse_db.referrals_of(401)) == [402, 403]
    print("OK  связи: «пришёл по ссылке» (⚠️ если пригласивший в той же группе), «привёл N, из них в группе»; "
          "сводка всех текущих групп ничего не отмечает")


async def main():
    await test_traffic()
    await test_devices()
    await test_lookup()
    await test_referral_links()


if __name__ == "__main__":
    asyncio.run(main())
