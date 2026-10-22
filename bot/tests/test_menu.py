"""Тесты навигации: главное меню в трёх состояниях, Подписка / Продлить / Тарифы / Как подключиться."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from handlers import handlers  # noqa: E402
from handlers import ui_screens  # noqa: E402


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    conn.execute(query, params)
    conn.commit()
    conn.close()


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


screens = []


async def fake_show_screen(target, image_key, text, reply_markup=None):
    screens.append((image_key, text, reply_markup))


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class FakeCallback:
    def __init__(self, user_id, data=""):
        self.from_user = SimpleNamespace(id=user_id, first_name="Вася")
        self.data = data
        self.message = None

    async def answer(self, *a, **k):
        return None


class FakePanel:
    async def format_subscription_info_by_short_uuid(self, short_uuid):
        return {"is_active": True, "formatted_expiry": "12.11.2026 12:00 МСК"}

    async def get_user_by_username(self, username):
        return {"id": 1, "subscriptionUrl": "https://sub.example/abc"}

    async def get_user_hwid_devices_count_by_username(self, username):
        return 2


async def main():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    handlers.show_screen = fake_show_screen
    handlers.panel_api = FakePanel()
    sql(path, "INSERT INTO users (telegram_id) VALUES (1), (2), (3), (4)")
    now = datetime.now(timezone.utc)

    # Картинки необязательны (white-label), но у каждого экрана есть своё имя файла
    assert {"start", "subs", "renew", "trial", "guide"} <= set(ui_screens.IMAGES)

    # 1. Новый пользователь: триал + тарифы
    await handlers.render_main_menu(None, 1, "Вася")
    image, text, kb = screens[-1]
    assert image == "start" and "бесплатно" in text
    assert buttons(kb)[:2] == ["Попробовать бесплатно", "Тарифы"] and "Пригласить друга" in buttons(kb)

    # 1б. Триал уже был (подписка ушла в архив) — без кнопки триала
    sql(path, "INSERT INTO subscriptions_archive (id, telegram_id, tariff_type_id, tariff_plan_id, trial_used) "
              "VALUES (99, 4, 1, 1, 1)")
    await handlers.render_main_menu(None, 4, "Петя")
    assert "Попробовать бесплатно" not in buttons(screens[-1][2]) and buttons(screens[-1][2])[0] == "Тарифы"

    # 2. Активная подписка: шапка со статусом, Подписка / Продлить
    sid = await db.create_subscription(2, 1, 1, "s2", iso(now + timedelta(days=21, hours=2)), "tg2_1")
    await handlers.render_main_menu(None, 2, "Вася")
    image, text, kb = screens[-1]
    assert "Default" in text and "3 устройства" in text and "21 день" in text, text
    assert buttons(kb)[:2] == ["Подписка", "Продлить"] and callbacks(kb)[1] == "renew"

    # 3. Истекла: Продлить — первой
    await db.create_subscription(3, 1, 1, "s3", iso(now - timedelta(days=2)), "tg3_1")
    await handlers.render_main_menu(None, 3, "Вася")
    image, text, kb = screens[-1]
    assert "закончилась" in text and buttons(kb)[:2] == ["Продлить", "Подписка"]

    # Подписка: одна — сразу карточка с новой раскладкой и картинкой «подписка»
    await handlers.my_subscriptions(FakeCallback(2))
    image, text, kb = screens[-1]
    assert image == "subs" and "Тариф Default" in text and "<code>https://sub.example/abc</code>" in text
    assert buttons(kb) == ["Продлить", "Сменить тариф", "Сбросить устройства", "Перевыпустить ссылку",
                           "Как подключиться", "В меню"], buttons(kb)

    # Нет подписки: экран с триалом и тарифами
    await handlers.my_subscriptions(FakeCallback(1))
    assert buttons(screens[-1][2]) == ["Попробовать бесплатно", "Тарифы", "В меню"]

    # Продлить: планы текущего тарифа, картинка «продлить»; без подписки — тарифы
    await handlers.renew_main(FakeCallback(2))
    image, text, kb = screens[-1]
    assert image == "renew" and any(c.startswith("renew_plan_") for c in callbacks(kb)) and "Сменить тариф" in buttons(kb)
    assert callbacks(kb)[-1] == "back_to_main"                                   # из меню — назад в меню
    await handlers.renew_select_plan(FakeCallback(2, data=f"renew_type_1_{sid}"))
    assert screens[-1][0] == "renew" and callbacks(screens[-1][2])[-1] == f"view_sub_{sid}"   # из карточки — в карточку
    await handlers.renew_main(FakeCallback(1))
    assert screens[-1][0] == "tariffs"

    # Тарифы: текущий отмечен
    await handlers.show_tariff_types(FakeCallback(2))
    image, text, kb = screens[-1]
    assert "твой тариф" in text and "Default ✓" in buttons(kb)

    # Как подключиться
    await handlers.connection_guide(FakeCallback(2))
    image, text, kb = screens[-1]
    assert image == "guide" and "Happ" in text and buttons(kb)[0] == "Подписка"
    from services import tariffs
    assert [tariffs.plural_devices(n) for n in (1, 3, 5, 10, 21)] == \
        ["1 устройство", "3 устройства", "5 устройств", "10 устройств", "21 устройство"]
    print("OK  меню: нет подписки (триал/тарифы), активна (статус в шапке, Подписка/Продлить), истекла (Продлить первой); "
          "карточка, Продлить, Тарифы с отметкой, Как подключиться — со своими картинками; «Назад» из «Продлить» — туда, откуда пришли; склонение устройств")


if __name__ == "__main__":
    asyncio.run(main())
