"""
Эксплуатация: ежедневный бэкап базы и оповещения админам о сбоях.

Бэкап (раз в сутки после BACKUP_HOUR_MSK):
- снимок базы через SQLite backup API (согласованная копия, даже если бот в этот момент пишет),
  сжатый, в BACKUP_DIR — его видно в разделе бэкапов админки;
- файл уходит владельцу (BACKUP_CHAT_ID, по умолчанию ADMIN_ID) в Telegram — копия вне сервера;
- автоматические бэкапы старше BACKUP_KEEP_DAYS удаляются (ручные не трогаем);
- заодно чистятся старые отметки отправленных напоминаний.

Оповещения (раз в MONITOR_INTERVAL):
- нода панели недоступна две проверки подряд → сообщение; снова в строю → сообщение;
- панель не отвечает три проверки подряд → сообщение; ответила → сообщение;
- ошибки в логах бота: ошибки оплаты — сразу, остальные — сводкой, если их набралось
  ERROR_ALERT_THRESHOLD за интервал (не чаще раза в час).
"""
import asyncio
import gzip
import html
import logging
import os
import re
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiosqlite
from aiogram.types import FSInputFile

from config import config
from database import db

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
AUTO_PREFIX = "vpnbot_autobackup_"
SETTING_LAST_BACKUP = "last_auto_backup"


def backup_dir() -> str:
    return getattr(config, "BACKUP_DIR", "/opt/fastvpnbot/backups")


# ======================================================
# БЭКАП
# ======================================================

async def make_backup(now: Optional[datetime] = None) -> str:
    """Согласованный снимок базы, сжатый. Возвращает путь к .db.gz."""
    now = now or datetime.now(MSK)
    os.makedirs(backup_dir(), exist_ok=True)
    raw = os.path.join(backup_dir(), f"{AUTO_PREFIX}{now:%Y%m%d_%H%M%S}.db")
    async with aiosqlite.connect(db.DB_PATH) as src, aiosqlite.connect(raw) as dst:
        await src.backup(dst)
    with open(raw, "rb") as f_in, gzip.open(f"{raw}.gz", "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw)
    return f"{raw}.gz"


def rotate_backups(keep_days: int, now: Optional[datetime] = None) -> int:
    """Удалить автоматические бэкапы старше keep_days (ручные остаются)"""
    now = now or datetime.now(MSK)
    removed = 0
    for name in os.listdir(backup_dir()):
        match = re.fullmatch(rf"{AUTO_PREFIX}(\d{{8}})_\d{{6}}\.db\.gz", name)
        if not match:
            continue
        made = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=MSK)
        if now - made > timedelta(days=keep_days):
            os.remove(os.path.join(backup_dir(), name))
            removed += 1
    return removed


async def _backup_stats() -> str:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        users = (await (await conn.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        subs = (await (await conn.execute("SELECT COUNT(*) FROM subscriptions")).fetchone())[0]
        paid = (await (await conn.execute(
            "SELECT COUNT(*) FROM payment_transactions WHERE status = 'CONFIRMED'")).fetchone())[0]
    return f"пользователей {users}, подписок {subs}, оплат {paid}"


async def daily_backup(bot, now: Optional[datetime] = None) -> Optional[str]:
    """Сделать бэкап, если сегодня его ещё не было и уже наступил BACKUP_HOUR_MSK. Возвращает путь или None."""
    if not getattr(config, "BACKUP_ENABLED", True):
        return None
    now = now or datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    if now.hour < getattr(config, "BACKUP_HOUR_MSK", 4) or await db.get_setting(SETTING_LAST_BACKUP) == today:
        return None

    path = await make_backup(now)
    await db.set_setting(SETTING_LAST_BACKUP, today)
    removed = rotate_backups(getattr(config, "BACKUP_KEEP_DAYS", 14), now)
    cleaned = await db.cleanup_reminders(30)
    size = os.path.getsize(path)
    logger.info(f"💾 Бэкап базы: {path} ({size // 1024} КБ), удалено старых {removed}, отметок напоминаний {cleaned}")

    chat_id = getattr(config, "BACKUP_CHAT_ID", None) or getattr(config, "ADMIN_ID", None)
    if chat_id:
        try:
            await bot.send_document(
                chat_id, FSInputFile(path),
                caption=(f"💾 Ежедневный бэкап базы бота · {now:%d.%m.%Y %H:%M} МСК\n"
                         f"{await _backup_stats()} · {size / 1024:.0f} КБ\n"
                         f"Восстановить: /admin → бэкапы, или распаковать gzip в bot_database.db"),
            )
        except Exception as e:
            # Копия на сервере уже есть; без файла в Telegram она не «вне сервера» — сообщаем отдельно
            logger.error(f"Не удалось отправить бэкап в Telegram: {e}")
    return path


# ======================================================
# ОШИБКИ В ЛОГАХ
# ======================================================

# Шум, который сам проходит: обрывы связи с Telegram, незакрытые сессии при остановке
_NOISE = ("Failed to fetch updates", "Unclosed client session", "Unclosed connector")
_URGENT_LOGGERS = ("services.payment_webhook", "handlers.payment", "services.platega_api")


class ErrorCollector(logging.Handler):
    """Копит ошибки из логов бота между проверками монитора"""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.counts: Counter = Counter()
        self.urgent: List[str] = []

    def emit(self, record: logging.LogRecord):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if any(noise in message for noise in _NOISE):
            return
        signature = re.sub(r"\d+", "N", message)[:150]
        self.counts[signature] += 1
        if record.name.startswith(_URGENT_LOGGERS) and len(self.urgent) < 20:
            self.urgent.append(message[:300])

    def take(self):
        counts, urgent = self.counts, self.urgent
        self.counts, self.urgent = Counter(), []
        return counts, urgent


collector = ErrorCollector()


def install_error_collector():
    root = logging.getLogger()
    if collector not in root.handlers:
        root.addHandler(collector)


# ======================================================
# МОНИТОР
# ======================================================

class Monitor:
    def __init__(self):
        self.node_fail: Dict[str, int] = {}     # uuid → проверок подряд без связи
        self.node_down: Dict[str, str] = {}     # uuid → имя, о недоступности уже сообщили
        self.panel_fail = 0
        self.panel_down = False
        self.first_check = True
        self.last_digest = None

    async def check(self, bot, panel_api, now: Optional[datetime] = None) -> List[str]:
        """Одна проверка. Возвращает отправленные сообщения (для тестов и логов)."""
        now = now or datetime.now(MSK)
        sent = []
        sent += await self._check_nodes(panel_api)
        sent += self._check_errors(now)
        for text in sent:
            await _alert(bot, text)
        self.first_check = False
        return sent

    async def _check_nodes(self, panel_api) -> List[str]:
        messages = []
        response = await panel_api._request("GET", "nodes") if panel_api else None
        nodes = (response or {}).get("response") if isinstance(response, dict) else None
        if nodes is None:
            self.panel_fail += 1
            if self.panel_fail >= 3 and not self.panel_down:
                self.panel_down = True
                messages.append("🔴 <b>Панель Remnawave не отвечает</b> (3 проверки подряд).\n"
                                "Бот не может выдавать и продлевать подписки.")
            return messages
        if self.panel_down:
            messages.append("🟢 <b>Панель Remnawave снова отвечает</b>")
        self.panel_fail, self.panel_down = 0, False

        ignore = set(getattr(config, "MONITOR_IGNORE_NODES", []) or [])
        went_down, came_up = [], []
        for node in nodes:
            uuid, name = node.get("uuid"), node.get("name") or node.get("address") or "?"
            if not uuid or node.get("isDisabled") or name in ignore:
                continue
            if node.get("isConnected"):
                self.node_fail.pop(uuid, None)
                if uuid in self.node_down:
                    came_up.append(self.node_down.pop(uuid))
                continue
            self.node_fail[uuid] = self.node_fail.get(uuid, 0) + 1
            # при запуске бота сразу сообщаем о лежащих нодах, дальше — после двух проверок подряд
            if uuid not in self.node_down and (self.first_check or self.node_fail[uuid] >= 2):
                self.node_down[uuid] = name
                reason = (node.get("lastStatusMessage") or "").strip()
                went_down.append(f"{html.escape(name)} ({html.escape(node.get('address') or '')})"
                                 + (f": {html.escape(reason[:120])}" if reason else ""))
        if went_down:
            title = "🔴 <b>Недоступны ноды</b>" + (" (при запуске бота)" if self.first_check else "")
            messages.append(title + "\n" + "\n".join(f"• {line}" for line in went_down)
                            + "\n\nПользователи этих серверов без связи.")
        if came_up:
            messages.append("🟢 <b>Ноды снова в строю</b>\n" + "\n".join(f"• {html.escape(n)}" for n in came_up))
        return messages

    def _check_errors(self, now: datetime) -> List[str]:
        counts, urgent = collector.take()
        messages = []
        if urgent:
            messages.append("💳 <b>Ошибки оплаты</b>\n" + "\n".join(f"• <code>{html.escape(m)}</code>" for m in urgent[:10])
                            + "\n\nПодробности: <code>journalctl -u fastvpnbot</code>")
        total = sum(counts.values())
        threshold = getattr(config, "ERROR_ALERT_THRESHOLD", 20)
        if total >= threshold and (self.last_digest is None or now - self.last_digest >= timedelta(hours=1)):
            self.last_digest = now
            top = "\n".join(f"• {n}× <code>{html.escape(sig)}</code>" for sig, n in counts.most_common(5))
            messages.append(f"⚠️ <b>Много ошибок в боте: {total} за {monitor_interval() // 60} мин</b>\n{top}\n\n"
                            f"Подробности: <code>journalctl -u fastvpnbot</code>")
        return messages


def monitor_interval() -> int:
    return getattr(config, "MONITOR_INTERVAL", 300)


async def _alert(bot, text: str):
    from services.abuse import _broadcast
    await _broadcast(bot, text)


monitor = Monitor()
