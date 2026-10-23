"""Тесты эксплуатации: напоминания в базе (без дублей после перезапуска), ежедневный бэкап, оповещения о сбоях."""
import asyncio
import gzip
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from services import ops  # noqa: E402
from services.reminder import SubscriptionReminder  # noqa: E402

MSK = ops.MSK


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


class FakeBot:
    def __init__(self):
        self.sent, self.documents = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.documents.append((chat_id, document.path, caption))


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    return path


async def test_reminders_persist():
    path = await fresh_db()
    assert not await db.reminder_sent_within("1_5", "24h", 48 * 3600)
    await db.mark_reminder_sent("1_5", "24h")
    assert await db.reminder_sent_within("1_5", "24h", 48 * 3600)
    sql(path, "UPDATE reminder_notifications SET sent_at = datetime('now', '-49 hours')")
    assert not await db.reminder_sent_within("1_5", "24h", 48 * 3600)       # окно 48 ч прошло
    assert await db.reminder_sent_within("1_5", "24h", None)                 # «когда-либо» — да

    # Подписка заканчивается через 24 ч: напоминание одно, даже если бот перезапустился
    sql(path, "INSERT INTO users (telegram_id) VALUES (7)")
    expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sid = await db.create_subscription(7, 1, 1, "s7", expiry, "tg7_1")
    sub = {"id": sid, "telegram_id": 7, "subscription_expiry": expiry, "status": "ACTIVE", "tariff_name": "Standart"}
    bot = FakeBot()
    await SubscriptionReminder(bot)._check_subscription(sub)
    await SubscriptionReminder(bot)._check_subscription(sub)                # «после перезапуска» — новый объект
    assert len([s for s in bot.sent if s[0] == 7]) == 1, bot.sent

    # Очистка старых отметок (раньше падала каждую ночь)
    sql(path, "UPDATE reminder_notifications SET sent_at = datetime('now', '-40 days') WHERE notif_key = '1_5'")
    assert await db.cleanup_reminders(30) == 1
    assert sql(path, "SELECT COUNT(*) FROM reminder_notifications")[0][0] == 1
    print("OK  напоминания: хранятся в базе, после перезапуска не дублируются, старые чистятся без ошибок")


async def test_backup():
    path = await fresh_db()
    sql(path, "INSERT INTO users (telegram_id) VALUES (1), (2)")
    ops.config.BACKUP_DIR = tempfile.mkdtemp()
    ops.config.BACKUP_HOUR_MSK = 4
    ops.config.ADMIN_ID = 555
    bot = FakeBot()

    early = datetime(2026, 10, 24, 3, 30, tzinfo=MSK)
    assert await ops.daily_backup(bot, early) is None                       # до 4:00 МСК — рано
    morning = datetime(2026, 10, 24, 4, 5, tzinfo=MSK)
    made = await ops.daily_backup(bot, morning)
    assert made and os.path.basename(made) == "vpnbot_autobackup_20261024_040500.db.gz"
    assert await ops.daily_backup(bot, morning + timedelta(hours=5)) is None   # второй раз за день — нет

    # Копия — настоящая база
    restored = os.path.join(tempfile.mkdtemp(), "r.db")
    with gzip.open(made, "rb") as f_in, open(restored, "wb") as f_out:
        f_out.write(f_in.read())
    assert sql(restored, "SELECT COUNT(*) FROM users")[0][0] == 2

    # Файл ушёл владельцу с подписью
    assert bot.documents[0][0] == 555 and bot.documents[0][1] == made
    assert "пользователей 2" in bot.documents[0][2]

    # Ротация: автоматические старше 14 дней удаляются, ручные остаются
    d = ops.config.BACKUP_DIR
    for name in ("vpnbot_autobackup_20261001_040000.db.gz", "vpnbot_autobackup_20261015_040000.db.gz",
                 "vpnbot_backup_20261001_211650.db.gz"):
        open(os.path.join(d, name), "wb").close()
    assert ops.rotate_backups(14, morning) == 1
    left = sorted(os.listdir(d))
    assert "vpnbot_autobackup_20261001_040000.db.gz" not in left and "vpnbot_backup_20261001_211650.db.gz" in left
    assert "vpnbot_autobackup_20261015_040000.db.gz" in left

    # На следующий день — новый бэкап
    assert await ops.daily_backup(bot, morning + timedelta(days=1))
    print("OK  бэкап: раз в сутки после 4:00 МСК, согласованная копия базы, файл владельцу, ротация только автоматических")


class FakePanel:
    def __init__(self):
        self.nodes = []
        self.down = False

    async def _request(self, method, path, **kw):
        assert (method, path) == ("GET", "nodes")
        return None if self.down else {"response": self.nodes}


def node(uuid, name, connected, disabled=False, msg=""):
    return {"uuid": uuid, "name": name, "address": f"{name}.example", "isConnected": connected,
            "isDisabled": disabled, "lastStatusMessage": msg}


async def test_monitor():
    path = await fresh_db()
    ops.config.ADMIN_ID = 555
    ops.config.MONITOR_IGNORE_NODES = ["Old"]
    mon = ops.Monitor()
    panel, bot = FakePanel(), FakeBot()
    now = datetime(2026, 10, 24, 12, 0, tzinfo=MSK)

    # При запуске: лежащая нода — сразу; выключенная и игнорируемая — нет
    panel.nodes = [node("a", "Estonia", True), node("g", "Germany", False, msg="timeout of 15000ms exceeded"),
                   node("x", "Spare", False, disabled=True), node("o", "Old", False)]
    sent = await mon.check(bot, panel, now)
    assert len(sent) == 1 and "Germany" in sent[0] and "при запуске" in sent[0] and "timeout" in sent[0]
    assert "Spare" not in sent[0] and "Old" not in sent[0]
    assert bot.sent[-1][0] == 555
    assert await mon.check(bot, panel, now) == []                           # уже сообщили — молчим

    # Нода упала: первая неудачная проверка — тишина (мог быть сбой), вторая — сообщение
    panel.nodes[0] = node("a", "Estonia", False)
    assert await mon.check(bot, panel, now) == []
    sent = await mon.check(bot, panel, now)
    assert len(sent) == 1 and "Estonia" in sent[0] and "при запуске" not in sent[0]
    # Поднялись обе
    panel.nodes[0], panel.nodes[1] = node("a", "Estonia", True), node("g", "Germany", True)
    sent = await mon.check(bot, panel, now)
    assert len(sent) == 1 and "снова в строю" in sent[0] and "Estonia" in sent[0] and "Germany" in sent[0]

    # Панель не отвечает: сообщение на третьей проверке, одно; потом — что ответила
    panel.down = True
    assert await mon.check(bot, panel, now) == [] and await mon.check(bot, panel, now) == []
    sent = await mon.check(bot, panel, now)
    assert len(sent) == 1 and "Панель Remnawave не отвечает" in sent[0]
    assert await mon.check(bot, panel, now) == []
    panel.down = False
    assert any("снова отвечает" in s for s in await mon.check(bot, panel, now))
    print("OK  монитор: ноды (при запуске сразу, потом после 2 проверок, восстановление), панель (3 проверки), без спама")


async def test_error_alerts():
    await fresh_db()
    ops.config.ADMIN_ID = 555
    ops.config.ERROR_ALERT_THRESHOLD = 20
    ops.install_error_collector()
    ops.collector.take()
    mon = ops.Monitor()
    mon.first_check = False
    panel, bot = FakePanel(), FakeBot()
    now = datetime(2026, 10, 24, 12, 0, tzinfo=MSK)
    log = logging.getLogger("services.reminder")
    log.propagate = True

    # Шум и немного ошибок — тишина
    for _ in range(30):
        logging.getLogger("aiogram.dispatcher").error("Failed to fetch updates - TelegramNetworkError: timeout")
    for i in range(5):
        log.error(f"Error processing subscription {i}: boom")
    assert await mon.check(bot, panel, now) == []

    # Много одинаковых ошибок — одна сводка, номера подписок схлопнуты
    for i in range(25):
        log.error(f"Error processing subscription {i}: cannot access local variable")
    sent = await mon.check(bot, panel, now)
    assert len(sent) == 1 and "25" in sent[0] and "subscription N" in sent[0], sent
    for i in range(25):
        log.error(f"Error processing subscription {i}: again")
    assert await mon.check(bot, panel, now + timedelta(minutes=10)) == []   # сводка не чаще раза в час
    for i in range(25):
        log.error(f"Error processing subscription {i}: again")
    assert len(await mon.check(bot, panel, now + timedelta(hours=1, minutes=1))) == 1

    # Ошибка оплаты — сразу, даже одна
    logging.getLogger("services.payment_webhook").error("Активация транзакции t3 не удалась")
    sent = await mon.check(bot, panel, now + timedelta(hours=1, minutes=2))
    assert len(sent) == 1 and "Ошибки оплаты" in sent[0] and "t3" in sent[0]
    print("OK  ошибки в логах: шум пропускаем, сводка при всплеске (не чаще раза в час), ошибки оплаты — сразу")


async def main():
    await test_reminders_persist()
    await test_backup()
    await test_monitor()
    await test_error_alerts()


if __name__ == "__main__":
    asyncio.run(main())
