"""Тесты подарка дней с рассылкой: пересчёт по тарифу, ГБ за дни, истёкшие, без подписки, повтор, персональная строка."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from services import gifts, tariffs, whitelist  # noqa: E402
from test_tariffs import FakePanel as TariffPanel, fresh_db, sql, STD_SQUAD, BS  # noqa: E402

GB = whitelist.GB
MID = "mailing-1"


class FakePanel(TariffPanel):
    def __init__(self):
        super().__init__()
        self.created = []
        self.down = False

    async def extend_subscription_by_username(self, username, days, traffic_limit_bytes=None):
        if self.down:
            return None
        return await super().extend_subscription_by_username(username, days, traffic_limit_bytes)

    async def create_user(self, username, telegram_id, squad_id, days=30, hwid_limit=3, tag=None, extra_squad_ids=None):
        self.created.append((username, days, squad_id, extra_squad_ids))
        self.add(username, [squad_id] + list(extra_squad_ids or []))
        return {"shortUuid": f"su-{username}", "expireAt": tariffs.iso_z(datetime.now(timezone.utc) + timedelta(days=days)),
                "subscriptionUrl": "https://sub/x"}

    async def set_user_squads(self, username, squads):
        self.users[username]["activeInternalSquads"] = [{"uuid": s} for s in squads]
        return True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, "text", text))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        self.sent.append((chat_id, "photo", caption))

    async def send_video_note(self, chat_id, file_id, **kw):
        self.sent.append((chat_id, "video_note", None))


async def setup():
    path = await fresh_db()
    whitelist.config.BS_SQUAD_UUID = BS
    await whitelist.set_enabled(True)
    panel = FakePanel()
    now = datetime.now(timezone.utc)
    sql(path, "INSERT INTO users (telegram_id) VALUES (2), (3), (4)")
    sql(path, "INSERT INTO users (telegram_id, role) VALUES (5, 'banned')")
    subs = {}
    for tid, type_id, expiry in ((1, 1, now + timedelta(days=5)), (2, 2, now + timedelta(days=5)),
                                 (3, 3, now - timedelta(days=10)), (5, 1, now + timedelta(days=5))):
        username = f"tg{tid}_{type_id}"
        subs[tid] = await db.create_subscription(tid, type_id, 1, f"s{tid}", tariffs.iso_z(expiry), username)
        panel.add(username, [STD_SQUAD])
    sql(path, "UPDATE subscriptions SET status = 'EXPIRED' WHERE id = ?", (subs[3],))
    return path, panel, subs


async def test_grant():
    path, panel, subs = await setup()

    std = await gifts.grant(panel, MID, 1, 10)
    assert (std.days, std.bs_bytes, std.created) == (10, 10 * GB, False)          # 10 дней × 30 / 30
    fam = await gifts.grant(panel, MID, 2, 10)
    assert (fam.days, fam.bs_bytes) == (7, 14 * GB)                                # 10 × 200/300 = 6.67 → 7; 7 × 60/30
    corp = await gifts.grant(panel, MID, 3, 10)
    assert (corp.days, corp.bs_bytes) == (4, 16 * GB)                              # 10 × 200/500 = 4; 4 × 120/30
    assert panel.extended == [("tg1_1", 10), ("tg2_2", 7), ("tg3_3", 4)]
    s3 = await db.get_subscription(subs[3])
    assert s3["status"] == "ACTIVE" and s3["subscription_expiry"] == "2099-01-01T00:00:00.000Z"   # истёкшая ожила

    # Подписки не было — создаётся Standart на 10 дней со сквадом БС
    new = await gifts.grant(panel, MID, 4, 10)
    assert new.created and (new.days, new.bs_bytes) == (10, 10 * GB)
    assert panel.created[0][:2] == ("tg4_1", 10) and panel.created[0][3] == [BS]
    created = await db.get_user_main_subscription(4)
    assert created["tariff_type_id"] == 1 and created["bs_bonus_bytes"] == 10 * GB and not created["trial_used"]

    # Та же рассылка второй раз не начисляет
    assert await gifts.grant(panel, MID, 1, 10) is None and len(panel.extended) == 3
    assert (await db.get_subscription(subs[1]))["bs_bonus_bytes"] == 10 * GB
    # Другая рассылка — начисляет
    assert (await gifts.grant(panel, "mailing-2", 1, 3)).days == 3

    # Панель не ответила — не начислено, место освобождено, повтор в той же рассылке проходит
    panel.down = True
    assert await gifts.grant(panel, "mailing-3", 2, 10) is None
    assert sql(path, "SELECT COUNT(*) FROM mailing_gifts WHERE mailing_id = 'mailing-3'")[0][0] == 0
    panel.down = False
    assert (await gifts.grant(panel, "mailing-3", 2, 10)).days == 7
    assert sql(path, "SELECT days, bs_bytes, status FROM mailing_gifts WHERE mailing_id = 'mailing-3'")[0] == \
        (7, 14 * GB, "granted")

    # Персональная строка
    assert await gifts.gift_line(std) == "🎁 Тебе начислено <b>+10 дней</b> подписки и <b>+10 ГБ</b> белых списков"
    assert "<b>+7 дней тарифа Family</b>" in await gifts.gift_line(fam)
    assert "ждёт тебя в разделе «Подписка»" in await gifts.gift_line(new)
    await whitelist.set_enabled(False)
    assert "ГБ" not in await gifts.gift_line(std)                                   # БС выключены — без ГБ
    await whitelist.set_enabled(True)

    examples = await gifts.examples(10)
    assert examples["Standart"] == (10, 10 * GB) and examples["Family"] == (7, 14 * GB)
    assert examples["Corporate"] == (4, 16 * GB)
    print("OK  подарок с рассылкой: 10 дн. Standart → 10/7/4 дн. и 10/14/16 ГБ, истёкшая ожила, без подписки — Standart, "
          "повтор в той же рассылке не начисляет, сбой панели можно повторить")


async def test_mailing_flow():
    from handlers import admin

    path, panel, subs = await setup()
    admin.panel_api = panel
    sql(path, "INSERT INTO users (telegram_id, role) VALUES (999, 'admin')")
    bot = FakeBot()

    class State:
        def __init__(self, data):
            self.data = data

        async def get_data(self):
            return dict(self.data)

        async def clear(self):
            self.data = {}

    class Msg:
        async def edit_text(self, *a, **k):
            return self

    callback = SimpleNamespace(from_user=SimpleNamespace(id=999), message=Msg())

    async def answer(*a, **k):
        callback.answered = a
    callback.answer = answer

    content = {"content_type": "photo", "file_id": "F", "caption": "Новости <b>сервиса</b>"}
    state = State({"audience": "all", "content": content, "gift_days": 10, "mailing_id": "m-flow"})
    await admin.confirm_mailing(callback, state, bot)
    by_user = {}
    for chat_id, kind, text in bot.sent:
        by_user.setdefault(chat_id, []).append((kind, text))
    assert by_user[1] == [("photo", "Новости <b>сервиса</b>\n\n🎁 Тебе начислено <b>+10 дней</b> подписки "
                                    "и <b>+10 ГБ</b> белых списков")], by_user[1]
    assert "+7 дней тарифа Family" in by_user[2][0][1] and "+4 дня тарифа Corporate" in by_user[3][0][1]
    assert by_user[5] == [("photo", "Новости <b>сервиса</b>")]                       # забаненному — без подарка
    assert (await db.get_subscription(subs[5]))["bs_bonus_bytes"] == 0
    assert "ждёт тебя в разделе «Подписка»" in by_user[4][0][1]                    # без подписки — создана Standart
    assert (await db.get_user_main_subscription(4))["tariff_type_id"] == 1

    # Повторное нажатие «Отправить» — рассылка уже идёт/прошла, второй раз не отправляем и не начисляем
    sent_before, extended_before = len(bot.sent), len(panel.extended)
    await admin.confirm_mailing(callback, state, bot)
    assert len(bot.sent) == sent_before and len(panel.extended) == extended_before

    # Длинная подпись: строка подарка уходит отдельным сообщением; кружочек — тоже отдельным
    bot.sent.clear()
    await admin._send_mailing(bot, 1, {"content_type": "photo", "file_id": "F", "caption": "x" * 1020}, "🎁 подарок")
    assert [k for _, k, _ in bot.sent] == ["photo", "text"] and bot.sent[1][2] == "🎁 подарок"
    bot.sent.clear()
    await admin._send_mailing(bot, 1, {"content_type": "video_note", "file_id": "F"}, "🎁 подарок")
    assert [k for _, k, _ in bot.sent] == ["video_note", "text"]
    bot.sent.clear()
    await admin._send_mailing(bot, 1, {"content_type": "text", "text": "Привет"}, "🎁 подарок")
    assert bot.sent == [(1, "text", "Привет\n\n🎁 подарок")]
    print("OK  рассылка с подарком: строка у каждого своя (в подписи или отдельным сообщением), забаненным без подарка, "
          "повторное «Отправить» ничего не дублирует")


async def main():
    await test_grant()
    await test_mailing_flow()


if __name__ == "__main__":
    asyncio.run(main())
