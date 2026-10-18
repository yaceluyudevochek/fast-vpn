"""Тесты перевыпуска ссылки подписки: суточный лимит, обновление shortUuid, сбой панели."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from services.subscription_link import reissue_subscription_link  # noqa: E402


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


class FakePanel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def revoke_subscription_by_username(self, username):
        self.calls.append(username)
        return self.response


async def main():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    assert "link_revokes" in {r[0] for r in sql(path, "SELECT name FROM sqlite_master WHERE type='table'")}

    sid = await db.create_subscription(1, 1, 1, "old-short-uuid", "2099-01-01T00:00:00.000Z", "tg1_1")
    sub = await db.get_subscription(sid)

    # Суточный лимит: сначала можно, после перевыпуска — нет, через 24 часа — снова можно
    assert (await db.can_revoke_link(sid)) == (True, "")
    await db.record_link_revoke(sid)
    ok, message = await db.can_revoke_link(sid)
    assert not ok and "Следующий перевыпуск" in message, message
    day_ago = (datetime.now() - timedelta(hours=24, minutes=1)).isoformat()
    sql(path, "UPDATE link_revokes SET last_revoke = ? WHERE subscription_id = ?", (day_ago, sid))
    assert (await db.can_revoke_link(sid))[0]
    await db.record_link_revoke(sid)
    assert sql(path, "SELECT revoke_count FROM link_revokes WHERE subscription_id = ?", (sid,))[0][0] == 2

    # Лимит перевыпуска не зависит от лимита сброса устройств
    assert (await db.can_reset_devices(sid))[0]

    # Успешный перевыпуск: новая ссылка, новый shortUuid в БД
    panel = FakePanel({"shortUuid": "new-short-uuid", "subscriptionUrl": "https://sub.example/new-short-uuid"})
    link = await reissue_subscription_link(panel, sub)
    assert link == "https://sub.example/new-short-uuid"
    assert panel.calls == ["tg1_1"]
    assert (await db.get_subscription(sid))["short_uuid"] == "new-short-uuid"

    # Панель не ответила: ссылки нет, shortUuid в БД прежний
    link = await reissue_subscription_link(FakePanel(None), await db.get_subscription(sid))
    assert link is None
    assert (await db.get_subscription(sid))["short_uuid"] == "new-short-uuid"
    print("OK  перевыпуск ссылки: лимит раз в сутки, отдельный от сброса устройств, shortUuid обновляется, сбой панели безопасен")


if __name__ == "__main__":
    asyncio.run(main())
