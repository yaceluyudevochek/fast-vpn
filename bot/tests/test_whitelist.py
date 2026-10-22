"""Тесты белых списков: квота на оплаченные/неоплаченные дни, выдача/снятие сквада, решения админа, миграция."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone

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


def utcnow() -> datetime:
    """Текущий момент в UTC без зоны — так время хранится в БД"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class FakePanel:
    """Панель с посуточной статистикой трафика на нодах сквада БС"""

    def __init__(self):
        self.users = {}       # username -> {id, username, expireAt, activeInternalSquads}
        self.daily = {}       # panel id -> {date: байты}
        self.exact_calls = []

    def add(self, username, uid, squads=(STD,), expire=FUTURE):
        self.users[username] = {
            "id": uid, "username": username, "expireAt": expire,
            "activeInternalSquads": [{"uuid": s, "name": s} for s in squads],
        }

    def use(self, uid, moment: datetime, gb: float):
        days = self.daily.setdefault(uid, {})
        day = moment.date()
        days[day] = days.get(day, 0) + int(gb * GB)

    def _sum(self, uid, start, end):
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        return sum(b for d, b in self.daily.get(uid, {}).items() if start <= d <= end)

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
        totals = {uid: self._sum(uid, start, end) for uid in self.daily}
        return {uid: b for uid, b in totals.items() if b >= min_total_bytes}

    async def get_squad_user_usage_bytes(self, squad, uid, start, end):
        assert squad == BS
        self.exact_calls.append(uid)
        return self._sum(uid, start, end)

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
    whitelist.config.BS_SQUAD_UUID = BS
    await whitelist.set_enabled(True)
    return path


async def make_sub(path, tid, username, type_id=1, anchor=None, paid_until=None, state=None, state_period=None):
    """Подписка; anchor — начало периода/пула, paid_until — до какого момента дни оплачены (None — не платил)"""
    sql(path, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (tid,))
    sid = await db.create_subscription(tid, type_id, 1 if type_id == 1 else 4, f"s-{username}", FUTURE, username)
    sql(path, "UPDATE subscriptions SET bs_period_anchor = ?, bs_paid_until = ?, bs_state = ?, bs_state_period = ? "
              "WHERE id = ?",
        (ts(anchor or NOW - timedelta(days=1)), ts(paid_until) if paid_until else None, state, state_period, sid))
    return sid


def state(path, sid):
    return sql(path, "SELECT bs_state FROM subscriptions WHERE id = ?", (sid,))[0][0]


async def sub(sid):
    return await db.get_subscription(sid)


async def test_period_math():
    start, reset = whitelist.current_period(ts(NOW - timedelta(days=65)), NOW)
    assert start == NOW - timedelta(days=5) and reset == NOW + timedelta(days=25), (start, reset)
    assert whitelist.current_period(None, NOW)[0] == NOW
    assert whitelist.format_gb(50 * GB) == "50" and whitelist.format_gb(int(32.6 * GB)) == "32.6"

    paid = {"bs_limit_gb": 50, "bs_period_anchor": ts(NOW - timedelta(days=40)), "bs_paid_until": ts(NOW + timedelta(days=60))}
    q = whitelist.get_quota(paid, NOW)
    assert q.paid and q.limit == 50 * GB and q.start == NOW - timedelta(days=10) and q.refresh_is_full

    unpaid = {"bs_limit_gb": 50, "bs_period_anchor": ts(NOW - timedelta(days=90)), "bs_paid_until": None}
    q = whitelist.get_quota(unpaid, NOW)
    assert not q.paid and q.limit == 3 * GB and q.start == NOW - timedelta(days=90) and not q.refresh_is_full

    after_paid = dict(paid, bs_paid_until=ts(NOW - timedelta(days=5)))
    q = whitelist.get_quota(after_paid, NOW)
    assert not q.paid and q.limit == 3 * GB and q.start == NOW - timedelta(days=5)

    assert whitelist.get_quota({"bs_limit_gb": None}, NOW).limit is None
    print("OK  квота: оплаченные дни — полный лимит каждые 30 дней, неоплаченные — разовый пул 3 ГБ с конца оплаты/начала триала")


async def test_disabled():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    whitelist.config.BS_SQUAD_UUID = BS
    panel = FakePanel()
    assert not await whitelist.is_enabled()
    assert await whitelist.check_all(panel, NOW) is None
    assert await whitelist.squads_for_new_user({"bs_limit_gb": 50}) == []

    whitelist.config.BS_SQUAD_UUID = ""   # сквад не задан — выключено даже при флаге
    await whitelist.set_enabled(True)
    assert not await whitelist.is_enabled()
    whitelist.config.BS_SQUAD_UUID = BS
    assert await whitelist.squads_for_new_user({"bs_limit_gb": 50}) == [BS]
    assert await whitelist.squads_for_new_user({"bs_limit_gb": None}) == []
    print("OK  выключено: проверки нет, сквад новым не выдаётся; включено — выдаётся, если БС входят в тариф")


async def test_check_all():
    path = await fresh_db()
    panel = FakePanel()
    paid = NOW + timedelta(days=90)

    a = await make_sub(path, 1, "a", paid_until=paid)                # 10 ГБ из 50 -> выдать
    panel.add("a", 101); panel.use(101, NOW, 10)
    b = await make_sub(path, 2, "b")                                 # не платил: 4 ГБ из 3 -> исчерпан
    panel.add("b", 102); panel.use(102, NOW, 4)
    c = await make_sub(path, 3, "c", paid_until=paid)                # в скваде, 60 ГБ из 50 -> снять
    panel.add("c", 103, squads=(STD, BS)); panel.use(103, NOW, 60)
    await make_sub(path, 4, "d", type_id=2, paid_until=paid)         # тариф без БС, но в скваде -> снять
    panel.add("d", 104, squads=(STD, BS))
    e = await make_sub(path, 5, "e", paid_until=paid, state="admin_off")  # отключён админом -> не трогать
    panel.add("e", 105)
    await make_sub(path, 6, "f", paid_until=paid)                    # истекла -> не выдавать
    panel.add("f", 106, expire=PAST)
    await make_sub(path, 7, "g", paid_until=paid)                    # 2 ГБ < мин. лимита -> выдать без точного запроса
    panel.add("g", 107, squads=(STD, ADMIN_SQUAD)); panel.use(107, NOW, 2)
    panel.add("friend", 999, squads=(STD, ADMIN_SQUAD, BS))          # не пользователь бота -> не трогать

    result = await whitelist.check_all(panel, NOW)
    assert result.added == [101, 107], result.added
    assert result.removed == [103, 104], result.removed
    assert sorted(s["telegram_id"] for s in result.exhausted) == [2, 3]
    assert BS in panel.squads("a") and BS in panel.squads("g") and ADMIN_SQUAD in panel.squads("g")
    for name in ("b", "c", "d", "e", "f"):
        assert BS not in panel.squads(name), name
    assert panel.squads("friend") == [STD, ADMIN_SQUAD, BS]
    assert 107 not in panel.exact_calls and 101 in panel.exact_calls
    assert state(path, b) == "exhausted" and state(path, c) == "exhausted"
    assert state(path, e) == "admin_off" and state(path, a) is None
    assert sql(path, "SELECT panel_user_id FROM subscriptions WHERE id = ?", (a,))[0][0] == 101

    bot = FakeBot()
    await whitelist.notify_users(bot, result)
    texts = dict(bot.sent)
    assert sorted(texts) == [2, 3]
    assert "после оплаты подписки" in texts[2]         # неоплаченные дни: вернётся только после оплаты
    assert "вернётся <b>" in texts[3] and "продлен" not in texts[3]   # оплаченные: обновится по расписанию

    again = await whitelist.check_all(panel, NOW)
    assert again.added == [] and again.removed == [] and again.exhausted == [] and again.restored == []

    # Через 30 дней: у оплаченного новый период — сквад вернулся; у неоплаченного пул не пополняется
    later = NOW + timedelta(days=30)
    result = await whitelist.check_all(panel, later)
    assert result.added == [103], result.added
    assert [s["telegram_id"] for s in result.restored] == [3]
    assert state(path, c) is None and BS in panel.squads("c")
    assert state(path, b) == "exhausted" and BS not in panel.squads("b")
    print("OK  проверка БС: выдача по тарифу, снятие за лимит/тариф, admin_off, истёкшие, чужие сквады и не-боты не тронуты, "
          "оплаченным лимит обновляется, неоплаченным — нет")


async def test_question_referral_days_then_payment():
    """100 бонусных дней без оплаты, потом оплата на 30 дней: полный лимит только на оплаченные 30 дней"""
    path = await fresh_db()
    panel = FakePanel()
    joined = NOW - timedelta(days=20)
    sid = await make_sub(path, 1, "u", anchor=joined)
    panel.add("u", 101)
    panel.use(101, joined + timedelta(days=1), 2)               # 2 ГБ за бонусные дни (из пула 3 ГБ)

    q = whitelist.get_quota(await sub(sid), NOW)
    assert not q.paid and q.limit == 3 * GB

    await db.add_bs_paid_days(sid, 30, NOW)   # оплатил 30 дней: оплаченных дней не было — период с момента оплаты
    q = whitelist.get_quota(await sub(sid), NOW + timedelta(days=1))
    assert q.paid and q.limit == 50 * GB and q.start == NOW and q.refresh_at == NOW + timedelta(days=30)
    panel.use(101, NOW + timedelta(days=1), 45)                  # 45 ГБ в оплаченные дни
    await whitelist.check_all(panel, NOW + timedelta(days=2))
    assert BS in panel.squads("u") and state(path, sid) is None

    # Оплаченные дни кончились, дальше — снова бонусные: 3 ГБ разово, с конца оплаты
    after = NOW + timedelta(days=31)
    q = whitelist.get_quota(await sub(sid), after)
    assert not q.paid and q.limit == 3 * GB and q.start == NOW + timedelta(days=30)
    panel.use(101, after, 2)
    await whitelist.check_all(panel, after)
    assert BS in panel.squads("u")                               # 2 из 3 ГБ — ещё можно
    panel.use(101, after + timedelta(days=10), 1.5)
    await whitelist.check_all(panel, after + timedelta(days=10))
    assert state(path, sid) == "exhausted" and BS not in panel.squads("u")
    # ...и не возвращается сам ни через 30, ни через 60 дней
    await whitelist.check_all(panel, after + timedelta(days=70))
    assert state(path, sid) == "exhausted" and BS not in panel.squads("u")
    status = await whitelist.get_status(panel, await sub(sid))
    print("OK  вопрос 1: бонусные дни → 3 ГБ, оплата 30 дней → 50 ГБ на 30 дней, потом снова разовые 3 ГБ без пополнения")
    return status


async def test_question_referral_farmer():
    """Не платит и приглашает людей: 3 ГБ на всё время, даже если трафик растянут на месяцы"""
    path = await fresh_db()
    panel = FakePanel()
    sid = await make_sub(path, 1, "farmer", anchor=NOW - timedelta(days=120))
    panel.add("farmer", 101)
    panel.use(101, NOW - timedelta(days=100), 1)
    panel.use(101, NOW - timedelta(days=60), 1)
    panel.use(101, NOW - timedelta(days=20), 1.5)               # за последние 31 день всего 1.5 ГБ

    result = await whitelist.check_all(panel, NOW)
    assert [s["telegram_id"] for s in result.exhausted] == [1]
    assert BS not in panel.squads("farmer")
    print("OK  вопрос 2: неплатящий «фармер» рефералов — 3 ГБ разово на всё время, растянутый трафик тоже учитывается")


async def test_payment_and_admin():
    path = await fresh_db()
    panel = FakePanel()

    c = await make_sub(path, 3, "c", paid_until=NOW + timedelta(days=20), anchor=NOW - timedelta(days=10))
    panel.add("c", 103); panel.use(103, datetime.now(timezone.utc), 60)
    await whitelist.check_all(panel)
    assert state(path, c) == "exhausted"

    # Досрочное продление безлимитного плана: дни складываются, но период и потраченный трафик не сбрасываются
    before = await sub(c)
    await whitelist.on_payment(panel, 3, {"tariff_type_id": 1, "days": 30, "traffic_limit_bytes": 0})
    s = await sub(c)
    assert datetime.fromisoformat(s["bs_paid_until"]) == datetime.fromisoformat(before["bs_paid_until"]) + timedelta(days=30)
    assert s["bs_period_anchor"] == before["bs_period_anchor"]
    assert state(path, c) == "exhausted" and BS not in panel.squads("c")
    result = await whitelist.check_all(panel)
    assert result.added == [] and result.restored == [] and BS not in panel.squads("c")

    # Лимитный план: срок от текущего момента (остаток сгорает, как и в панели), период тоже не сбрасывается
    await whitelist.on_payment(panel, 3, {"tariff_type_id": 1, "days": 30, "traffic_limit_bytes": 100 * GB})
    s = await sub(c)
    assert abs(datetime.fromisoformat(s["bs_paid_until"]) - (utcnow() + timedelta(days=30))) < timedelta(minutes=5)
    assert s["bs_period_anchor"] == before["bs_period_anchor"]

    # Неплативший исчерпал разовые 3 ГБ и оплатил — полный лимит сразу, сквад возвращается без ожидания проверки
    u = await make_sub(path, 8, "unpaid", anchor=NOW - timedelta(days=40))
    panel.add("unpaid", 108); panel.use(108, datetime.now(timezone.utc), 4)
    await whitelist.check_all(panel)
    assert state(path, u) == "exhausted" and BS not in panel.squads("unpaid")
    await whitelist.on_payment(panel, 8, {"tariff_type_id": 1, "days": 30, "traffic_limit_bytes": 0})
    assert state(path, u) is None and BS in panel.squads("unpaid")
    q = whitelist.get_quota(await sub(u))
    assert q.paid and q.limit == 50 * GB and q.start > datetime.now(timezone.utc) - timedelta(minutes=5)

    # Админ отключил БС — проверка и оплата не возвращают
    assert await whitelist.admin_set_user_squad(panel, await sub(c), BS, False)
    assert state(path, c) == "admin_off" and BS not in panel.squads("c")
    await whitelist.check_all(panel)
    await whitelist.on_payment(panel, 3, {"tariff_type_id": 1, "days": 30, "traffic_limit_bytes": 0})
    assert state(path, c) == "admin_off" and BS not in panel.squads("c")

    # Админ включил вручную — без лимита до обновления квоты
    assert await whitelist.admin_set_user_squad(panel, await sub(c), BS, True)
    assert state(path, c) == "admin_on"
    panel.use(103, datetime.now(timezone.utc), 500)
    result = await whitelist.check_all(panel)
    assert BS in panel.squads("c") and result.removed == []

    # Другие сквады админ переключает без изменения состояния БС
    assert await whitelist.admin_set_user_squad(panel, await sub(c), ADMIN_SQUAD, True)
    assert ADMIN_SQUAD in panel.squads("c") and state(path, c) == "admin_on"

    # Дни от админа неплатившему — полный лимит, период от момента выдачи
    t = await make_sub(path, 4, "trial", anchor=NOW - timedelta(days=50))
    panel.add("trial", 104)
    await whitelist.on_admin_days(panel, t, 30)
    q = whitelist.get_quota(await sub(t))
    assert q.paid and q.limit == 50 * GB and q.start > datetime.now(timezone.utc) - timedelta(minutes=5)
    # Дни от админа оплатившему — складываются, период не сбрасывается
    anchor_before = (await sub(t))["bs_period_anchor"]
    await whitelist.on_admin_days(panel, t, 10)
    s = await sub(t)
    assert s["bs_period_anchor"] == anchor_before
    assert abs(datetime.fromisoformat(s["bs_paid_until"]) - (utcnow() + timedelta(days=40))) < timedelta(minutes=5)
    # Админ уменьшил дни — уменьшается и оплаченный срок
    await db.reduce_bs_paid_days(t, 15)
    s = await sub(t)
    assert abs(datetime.fromisoformat(s["bs_paid_until"]) - (utcnow() + timedelta(days=25))) < timedelta(minutes=5)

    # Отключить у всех: снимается у всех, у кого есть (и у не-ботов), другие сквады остаются
    panel.add("friend", 999, squads=(ADMIN_SQUAD, BS))
    removed = await whitelist.disable_for_all(panel)
    assert removed == 4 and BS not in panel.squads("c") and panel.squads("friend") == [ADMIN_SQUAD]
    assert not await whitelist.is_enabled()
    print("OK  оплата и админ: досрочное продление не обновляет лимит, первая оплата — сразу полный лимит, "
          "лимитный план — от текущего момента, admin_off/admin_on, "
          "дни от админа = полный лимит, уменьшение дней, «отключить у всех»")


async def test_warning():
    path = await fresh_db()
    panel = FakePanel()
    now = datetime.now(timezone.utc)
    p = await make_sub(path, 1, "paid", anchor=now - timedelta(days=5), paid_until=now + timedelta(days=60))
    panel.add("paid", 101); panel.use(101, now, 42)                # 42 из 50 — осталось 8 ГБ
    short = await make_sub(path, 2, "short", anchor=now - timedelta(days=5), paid_until=now + timedelta(days=3))
    panel.add("short", 102); panel.use(102, now, 41)               # оплаченные дни кончатся раньше обновления
    u = await make_sub(path, 3, "unpaid", anchor=now - timedelta(days=5))
    panel.add("unpaid", 103); panel.use(103, now, 2.5)             # 2.5 из 3
    await make_sub(path, 4, "calm", paid_until=now + timedelta(days=60))
    panel.add("calm", 104); panel.use(104, now, 10)                # 10 из 50 — не предупреждаем

    result = await whitelist.check_all(panel)
    assert sorted(s["telegram_id"] for s, _ in result.warned) == [1, 2, 3]
    assert result.exhausted == [] and all(BS in panel.squads(n) for n in ("paid", "short", "unpaid", "calm"))
    bot = FakeBot()
    await whitelist.notify_users(bot, result)
    texts = dict(bot.sent)
    assert "осталось 8 из 50 ГБ" in texts[1] and "Лимит обновится" in texts[1] and "продлить" not in texts[1]
    assert "если продлить подписку" in texts[2]
    assert "осталось 0.5 из 3 ГБ" in texts[3] and "Оплати подписку" in texts[3] and "50 ГБ" in texts[3]

    # Повторно в той же квоте не предупреждаем, исчерпание — отдельное уведомление
    again = await whitelist.check_all(panel)
    assert again.warned == []
    panel.use(101, now, 10)
    again = await whitelist.check_all(panel)
    assert again.warned == [] and [s["telegram_id"] for s in again.exhausted] == [1]
    print("OK  предупреждение «осталось 20%»: один раз на квоту; тексты — когда обновится, «если продлить», «оплати подписку»")


async def test_referral_bonus():
    path = await fresh_db()
    panel = FakePanel()
    now = datetime.now(timezone.utc)

    # Неплатящий: 3 ГБ + 7 ГБ за друга = 10 ГБ
    u = await make_sub(path, 1, "farmer", anchor=now - timedelta(days=5))
    panel.add("farmer", 101); panel.use(101, now, 4)
    assert await whitelist.grant_referral_bonus(panel, u) == 7
    result = await whitelist.check_all(panel)
    assert result.exhausted == [] and BS in panel.squads("farmer")
    assert whitelist.get_quota(await sub(u)).total == 10 * GB
    text = whitelist.status_text(await whitelist.get_status(panel, await sub(u)))
    assert "бонусные" in text and "осталось" in text, text

    # Потратил всё — исчерпан; новый друг — доступ возвращается сразу, без ожидания проверки
    panel.use(101, now, 7)
    result = await whitelist.check_all(panel)
    assert [s["telegram_id"] for s in result.exhausted] == [1] and BS not in panel.squads("farmer")
    text = whitelist.status_text(await whitelist.get_status(panel, await sub(u)))
    assert "исчерпаны" in text and "друг по твоей ссылке" in text, text
    await whitelist.grant_referral_bonus(panel, u)
    assert state(path, u) is None and BS in panel.squads("farmer")
    text = whitelist.status_text(await whitelist.get_status(panel, await sub(u)))
    assert "идут бонусные: осталось <b>6 из 14 ГБ</b>" in text, text

    # Бонус начислен, когда доступ уже снят, — фоновая проверка тоже возвращает (и сообщает)
    c = await make_sub(path, 2, "paid", anchor=now - timedelta(days=5), paid_until=now + timedelta(days=60))
    panel.add("paid", 102); panel.use(102, now, 55)
    result = await whitelist.check_all(panel)
    assert [s["telegram_id"] for s in result.exhausted] == [2]
    await db.add_bs_bonus(c, 7 * GB)
    result = await whitelist.check_all(panel)
    assert [s["telegram_id"] for s in result.restored] == [2] and BS in panel.squads("paid")
    print("OK  бонус за друга: +7 ГБ поверх лимита (и для неплатящих), доступ возвращается сразу, не сгорает")


async def test_bonus_spent_at_rollover():
    path = await fresh_db()
    panel = FakePanel()
    sid = await make_sub(path, 1, "a", anchor=NOW - timedelta(days=20), paid_until=NOW + timedelta(days=90))
    panel.add("a", 101)
    await db.add_bs_bonus(sid, 14 * GB)
    await whitelist.check_all(panel, NOW)                          # квота запомнена, бонусы не тронуты
    panel.use(101, NOW, 60)                                        # 60 из 50 + 14
    result = await whitelist.check_all(panel, NOW)
    assert result.exhausted == [] and BS in panel.squads("a")

    # Новый период: потраченные сверх лимита 10 ГБ списываются из бонусов, остаётся 4 ГБ
    later = NOW + timedelta(days=10, hours=1)
    await whitelist.check_all(panel, later)
    s = await sub(sid)
    assert s["bs_bonus_used"] == 10 * GB, s["bs_bonus_used"] / GB
    assert whitelist.get_quota(s, later).total == 54 * GB
    await whitelist.check_all(panel, later)                        # повторная проверка ничего не списывает
    assert (await sub(sid))["bs_bonus_used"] == 10 * GB

    # Период «без лимита» от админа бонусы не тратит
    b = await make_sub(path, 2, "b", anchor=NOW - timedelta(days=20), paid_until=NOW + timedelta(days=90))
    panel.add("b", 102)
    await db.add_bs_bonus(b, 14 * GB)
    await whitelist.check_all(panel, NOW)
    assert await whitelist.admin_set_user_squad(panel, await sub(b), BS, True)
    sql(path, "UPDATE subscriptions SET bs_state_period = ? WHERE id = ?",
        (whitelist.get_quota(await sub(b), NOW).key, b))
    panel.use(102, NOW, 200)
    await whitelist.check_all(panel, later)
    assert (await sub(b))["bs_bonus_used"] == 0
    print("OK  бонусы списываются только на трафик сверх лимита закрывшейся квоты, один раз; «без лимита» их не тратит")


async def test_status_text():
    path = await fresh_db()
    panel = FakePanel()
    now = datetime.now(timezone.utc)
    sid = await make_sub(path, 1, "a", anchor=now - timedelta(days=1), paid_until=now + timedelta(days=60))
    panel.add("a", 101); panel.use(101, now, 17.4)

    text = whitelist.status_text(await whitelist.get_status(panel, await sub(sid)))
    assert "осталось <b>32.6 из 50 ГБ</b>" in text and "Лимит обновится" in text, text

    # Оплаченные дни кончаются раньше конца 30-дневного периода
    sql(path, "UPDATE subscriptions SET bs_paid_until = ? WHERE id = ?", (ts(now + timedelta(days=5)), sid))
    text = whitelist.status_text(await whitelist.get_status(panel, await sub(sid)))
    assert "по оплаченным дням" in text and "3 ГБ до продления" in text, text

    # Неоплаченные дни
    sql(path, "UPDATE subscriptions SET bs_paid_until = NULL, bs_period_anchor = ? WHERE id = ?",
        (ts(now - timedelta(days=1)), sid))
    panel.daily[101] = {}
    panel.use(101, now, 1)
    text = whitelist.status_text(await whitelist.get_status(panel, await sub(sid)))
    assert "осталось <b>2 из 3 ГБ</b>" in text and "Без оплаты лимит не пополняется" in text, text
    panel.use(101, now, 5)
    text = whitelist.status_text(await whitelist.get_status(panel, await sub(sid)))
    assert "исчерпан" in text and "после оплаты подписки" in text, text

    sql(path, "UPDATE tariff_types SET bs_limit_gb = NULL WHERE id = 1")
    assert await whitelist.get_status(panel, await sub(sid)) is None
    sql(path, "UPDATE tariff_types SET bs_limit_gb = 50 WHERE id = 1")
    await whitelist.set_enabled(False)
    assert await whitelist.get_status(panel, await sub(sid)) is None
    print("OK  карточка подписки: остаток, когда обновится, «по оплаченным дням», неоплаченный пул, исчерпан, скрыто без БС")


async def test_migration():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    db.DB_PATH = path
    await db.init_db()
    sql(path, "INSERT INTO users (telegram_id) VALUES (1), (2)")
    paid = await db.create_subscription(1, 1, 1, "s1", FUTURE, "u1")
    unpaid = await db.create_subscription(2, 1, 1, "s2", FUTURE, "u2")
    sql(path, "UPDATE subscriptions SET created_at = '2026-09-01 00:00:00'")
    # план 1 — 30 дней безлимит: оплаты 10.09 и 05.10 (раньше конца первой) складываются
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, activated_at) "
              "VALUES ('p1', 1, 1, 150, 'CONFIRMED', '2026-10-05 10:00:00'), "
              "('p0', 1, 1, 150, 'CONFIRMED', '2026-09-10 10:00:00'), "
              "('px', 1, 1, 150, 'CANCELED', '2026-10-10 10:00:00')")
    # «Боевая» БД v2.3.0: колонок БС ещё нет, user_version = 5
    conn = sqlite3.connect(path)
    for column in ("bs_period_anchor", "bs_state", "bs_state_period", "panel_user_id", "bs_paid_until"):
        conn.execute(f"ALTER TABLE subscriptions DROP COLUMN {column}")
    conn.execute("ALTER TABLE tariff_types DROP COLUMN bs_limit_gb")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    await db.init_db()
    rows = {r[0]: r[1:] for r in sql(path, "SELECT id, bs_period_anchor, bs_paid_until FROM subscriptions")}
    assert rows[paid] == ("2026-10-05 10:00:00", "2026-11-09 10:00:00"), rows[paid]
    assert rows[unpaid] == ("2026-09-01 00:00:00", None), rows[unpaid]
    assert sql(path, "PRAGMA user_version")[0][0] == len(db._MIGRATIONS)
    print("OK  миграция БС: период от последней оплаты, оплаченный срок по истории оплат (продления складываются)")


async def main():
    await test_period_math()
    await test_disabled()
    await test_check_all()
    await test_question_referral_days_then_payment()
    await test_question_referral_farmer()
    await test_payment_and_admin()
    await test_warning()
    await test_referral_bonus()
    await test_bonus_spent_at_rollover()
    await test_status_text()
    await test_migration()


if __name__ == "__main__":
    asyncio.run(main())
