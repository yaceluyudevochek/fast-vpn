import aiosqlite
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)
DB_PATH = "bot_database.db"

async def init_db():
    """Инициализация базы данных с новой структурой"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                role TEXT DEFAULT 'user',
                ref_code TEXT UNIQUE,
                referred_by INTEGER,
                referrals_activated INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица типов тарифов (Default, Premium и т.д.)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tariff_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                squad_id TEXT NOT NULL,
                hwid_limit INTEGER DEFAULT 3,
                description TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица сроков и цен для каждого типа тарифа
        await db.execute("""
                    CREATE TABLE IF NOT EXISTS tariff_plans (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tariff_type_id INTEGER NOT NULL,
                        days INTEGER NOT NULL,
                        price INTEGER NOT NULL,
                        traffic_limit_bytes BIGINT DEFAULT 0,  -- 0 = безлимит
                        is_active INTEGER DEFAULT 1,
                        FOREIGN KEY (tariff_type_id) REFERENCES tariff_types(id) ON DELETE CASCADE
                    )
                """)

        # Таблица подписок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                tariff_type_id INTEGER NOT NULL,
                tariff_plan_id INTEGER NOT NULL,
                short_uuid TEXT UNIQUE,
                username_in_panel TEXT UNIQUE,
                subscription_expiry TEXT,
                status TEXT DEFAULT 'ACTIVE',
                trial_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (tariff_type_id) REFERENCES tariff_types(id) ON DELETE RESTRICT,
                FOREIGN KEY (tariff_plan_id) REFERENCES tariff_plans(id) ON DELETE RESTRICT
            )
        """)

        # Таблица сбросов устройств
        await db.execute("""
            CREATE TABLE IF NOT EXISTS device_resets (
                subscription_id INTEGER PRIMARY KEY,
                last_reset TIMESTAMP,
                reset_count INTEGER DEFAULT 0,
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
                    CREATE TABLE IF NOT EXISTS payment_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transaction_id TEXT UNIQUE NOT NULL,      -- UUID из Platega
                        telegram_id INTEGER NOT NULL,
                        plan_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL,
                        currency TEXT DEFAULT 'RUB',
                        status TEXT DEFAULT 'PENDING',            -- PENDING, CONFIRMED, CANCELED, CHARGEBACKED
                        payment_method INTEGER,
                        payload TEXT,                              -- наши данные (user_plan)
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        confirmed_at TIMESTAMP,
                        FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                        FOREIGN KEY (plan_id) REFERENCES tariff_plans(id) ON DELETE RESTRICT
                    )
                """)

        await db.commit()
        await run_migrations(db)
        await create_default_tariffs(db)
        logger.info("✅ База данных инициализирована")


# ======================================================
# МИГРАЦИИ СХЕМЫ
# ======================================================
# Версия схемы хранится в PRAGMA user_version. Каждая миграция идемпотентна
# (сама проверяет, нужно ли ей что-то делать), поэтому безопасна и для боевой
# БД, где часть изменений уже была применена до появления версионирования.
# Чтобы изменить схему — добавьте функцию в конец _MIGRATIONS, не правя старые.

async def _column_names(db, table: str) -> set:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def _add_column(db, table: str, column: str, definition: str) -> bool:
    """Добавить колонку, если её ещё нет. Возвращает True, если добавили."""
    if column in await _column_names(db, table):
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


async def _m001_payment_activation(db):
    """Учёт реальной активации подписки по транзакции (v2.0.2)"""
    added = await _add_column(db, "payment_transactions", "activated_at", "TIMESTAMP")
    await _add_column(db, "payment_transactions", "activation_claimed_at", "TIMESTAMP")
    if added:
        # Все платежи, подтверждённые до миграции, считаем уже обработанными,
        # чтобы повторный callback по старой транзакции не продлил подписку ещё раз
        await db.execute("""
            UPDATE payment_transactions
            SET activated_at = COALESCE(confirmed_at, updated_at, CURRENT_TIMESTAMP)
            WHERE status = 'CONFIRMED'
        """)


async def _m002_tariff_full_description(db):
    """Полное описание тарифа (показывается на экране выбора срока)"""
    await _add_column(db, "tariff_types", "full_description", "TEXT")


async def _m003_trial_nudge(db):
    """Статус напоминания триальщику, который не подключился: NULL / sent / connected"""
    await _add_column(db, "subscriptions", "trial_nudge_status", "TEXT")


async def _m004_promo_codes(db):
    """Промокоды: коды, область действия, применения (по факту оплаты) и события для воронки"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE COLLATE NOCASE,
            kind TEXT NOT NULL DEFAULT 'regular',        -- regular | ad (рекламный, с учётом партнёра)
            title TEXT,
            discount_percent INTEGER NOT NULL DEFAULT 0,
            max_activations INTEGER,                      -- NULL = без лимита
            valid_until TEXT,                             -- ISO UTC, NULL = бессрочно
            first_payment_only INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            partner_name TEXT,
            commission_percent REAL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Пустая область действия = все планы
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_scope (
            promo_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            PRIMARY KEY (promo_id, plan_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            transaction_id TEXT NOT NULL UNIQUE,
            plan_id INTEGER,
            original_amount INTEGER NOT NULL,
            paid_amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_promo_redemptions_promo ON promo_redemptions(promo_id)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            event TEXT NOT NULL,                          -- link | applied
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_promo_events_promo ON promo_events(promo_id, event)")
    await _add_column(db, "payment_transactions", "promo_id", "INTEGER")
    await _add_column(db, "payment_transactions", "original_amount", "INTEGER")


async def _m005_link_revokes(db):
    """Перевыпуск ссылки подписки пользователем (не чаще раза в сутки)"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS link_revokes (
            subscription_id INTEGER PRIMARY KEY,
            last_revoke TIMESTAMP,
            revoke_count INTEGER DEFAULT 0,
            FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
        )
    """)


async def _m006_whitelist(db):
    """Белые списки (БС): лимит у типа тарифа, период и состояние у подписки, настройки бота"""
    await _add_column(db, "tariff_types", "bs_limit_gb", "INTEGER")       # NULL = БС не входит в тариф
    await _add_column(db, "subscriptions", "panel_user_id", "INTEGER")    # числовой id пользователя в панели
    added = await _add_column(db, "subscriptions", "bs_period_anchor", "TIMESTAMP")  # от него идут периоды по 30 дней
    await _add_column(db, "subscriptions", "bs_state", "TEXT")            # NULL / exhausted / admin_off / admin_on
    await _add_column(db, "subscriptions", "bs_state_period", "TEXT")     # начало периода, к которому относится bs_state
    paid_added = await _add_column(db, "subscriptions", "bs_paid_until", "TIMESTAMP")  # до какого момента дни оплачены
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    if added:
        # Период БС считается от последней оплаты подписки этого типа, без оплат — от создания подписки
        await db.execute("""
            UPDATE subscriptions SET bs_period_anchor = COALESCE(
                (SELECT MAX(COALESCE(p.activated_at, p.confirmed_at, p.created_at))
                 FROM payment_transactions p
                 JOIN tariff_plans tp ON tp.id = p.plan_id
                 WHERE p.telegram_id = subscriptions.telegram_id
                   AND tp.tariff_type_id = subscriptions.tariff_type_id
                   AND p.status = 'CONFIRMED'),
                created_at, CURRENT_TIMESTAMP)
        """)
    if paid_added:
        await _fill_bs_paid_until(db)


async def _fill_bs_paid_until(db):
    """
    Оплаченный срок по истории платежей: каждая оплата продлевает его на срок плана
    (безлимитные планы — от max(текущий срок, момент оплаты), лимитные — от момента оплаты,
    как продлевается и сама подписка). Бонусные/триальные дни сюда не входят.
    """
    async with db.execute("""
        SELECT s.id, COALESCE(p.activated_at, p.confirmed_at, p.created_at) AS paid_at,
               tp.days, tp.traffic_limit_bytes
        FROM subscriptions s
        JOIN payment_transactions p ON p.telegram_id = s.telegram_id AND p.status = 'CONFIRMED'
        JOIN tariff_plans tp ON tp.id = p.plan_id AND tp.tariff_type_id = s.tariff_type_id
        ORDER BY s.id, paid_at
    """) as cursor:
        rows = await cursor.fetchall()

    paid_until = {}
    for sub_id, paid_at, days, traffic_limit in rows:
        moment = datetime.fromisoformat(paid_at)
        current = paid_until.get(sub_id)
        start = max(current, moment) if (current and not traffic_limit) else moment
        paid_until[sub_id] = start + timedelta(days=days)
    for sub_id, value in paid_until.items():
        await db.execute("UPDATE subscriptions SET bs_paid_until = ? WHERE id = ?",
                         (value.strftime("%Y-%m-%d %H:%M:%S"), sub_id))


async def _m007_whitelist_warning(db):
    """Предупреждение «осталось 20% лимита белых списков»: для какой квоты уже отправлено"""
    await _add_column(db, "subscriptions", "bs_warned_period", "TEXT")


async def _m008_whitelist_bonus(db):
    """
    Бонусные ГБ белых списков (за друзей; позже — пакеты): начислено / потрачено в закрытых квотах.
    bs_quota_* — квота, которую бот сейчас отслеживает: при её смене трафик сверх обычного лимита
    в закрывшейся квоте списывается из бонусов.
    """
    await _add_column(db, "subscriptions", "bs_bonus_bytes", "INTEGER NOT NULL DEFAULT 0")
    await _add_column(db, "subscriptions", "bs_bonus_used", "INTEGER NOT NULL DEFAULT 0")
    await _add_column(db, "subscriptions", "bs_quota_key", "TEXT")
    await _add_column(db, "subscriptions", "bs_quota_start", "TIMESTAMP")
    await _add_column(db, "subscriptions", "bs_quota_limit", "INTEGER")


async def _m009_retention(db):
    """Возврат клиентов: журнал сообщений/подарков, блокировка бота пользователем, частота рассылки"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS retention_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            campaign TEXT NOT NULL,                       -- winback | trial_expired | trial_idle | start_only
            step TEXT NOT NULL,                           -- promo | gift | help | d1 / d3 / d7
            ref TEXT,                                     -- к чему относится (подписка и дата её окончания)
            subscription_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_retention_user ON retention_events(telegram_id, campaign, step)")
    await _add_column(db, "users", "bot_blocked_at", "TIMESTAMP")      # пользователь заблокировал бота
    await _add_column(db, "users", "last_marketing_at", "TIMESTAMP")   # последнее маркетинговое сообщение


async def _m010_abuse(db):
    """Оповещения админам о «плохишах»: что уже сообщали и кого игнорировать"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS abuse_alerts (
            key TEXT PRIMARY KEY,                         -- что именно сообщили (вид, пользователь/группа, период)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS abuse_ignore (
            telegram_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


async def _m011_referral_rewards(db):
    """Награда за друга — только после того, как друг подключился (появилось устройство в панели)"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS referral_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL UNIQUE,            -- за одного друга награда одна
            friend_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',       -- pending | granted | expired
            note TEXT,                                    -- например, общее устройство с пригласившим
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_referral_rewards_status ON referral_rewards(status)")


async def _m012_session_flags(db):
    """Подозрение «много одновременных IP при лимите устройств»: сколько проверок подряд оно держится"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS session_flags (
            panel_user_id INTEGER PRIMARY KEY,
            hits INTEGER NOT NULL DEFAULT 0,              -- сколько проверок подряд превышен порог
            last_hit_at TIMESTAMP,
            max_subnets INTEGER NOT NULL DEFAULT 0
        )
    """)


async def _m013_one_subscription(db):
    """
    Одна подписка на аккаунт, смена тарифа с пересчётом остатка по коэффициенту.
    Лишние подписки (у кого их несколько) переносим в архив: оставляем ту, что на активном
    тарифе и с самой поздней датой окончания.
    """
    await _add_column(db, "tariff_types", "switch_coef", "REAL")         # NULL — цена 30-дневного плана
    await _add_column(db, "subscriptions", "tariff_changed_at", "TIMESTAMP")
    await db.execute("CREATE TABLE IF NOT EXISTS subscriptions_archive AS SELECT * FROM subscriptions WHERE 0")
    await _add_column(db, "subscriptions_archive", "archived_at", "TIMESTAMP")
    async with db.execute("""
        SELECT s.id, s.telegram_id FROM subscriptions s JOIN tariff_types tt ON tt.id = s.tariff_type_id
        WHERE s.telegram_id IN (SELECT telegram_id FROM subscriptions GROUP BY telegram_id HAVING COUNT(*) > 1)
        ORDER BY s.telegram_id, tt.is_active DESC, s.subscription_expiry DESC, s.id DESC
    """) as cursor:
        rows = await cursor.fetchall()
    kept = set()
    for sub_id, telegram_id in rows:
        if telegram_id not in kept:
            kept.add(telegram_id)
            continue
        columns = [c for c in await _column_names(db, "subscriptions")]
        await db.execute(f"""
            INSERT INTO subscriptions_archive ({', '.join(columns)}, archived_at)
            SELECT {', '.join(columns)}, CURRENT_TIMESTAMP FROM subscriptions WHERE id = ?
        """, (sub_id,))
        await db.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))


async def _m014_trial_by_device(db):
    """Триал по устройству: проверено ли устройство триальщика, заблокирован ли повторный триал"""
    await _add_column(db, "subscriptions", "trial_device_checked", "INTEGER")
    await _add_column(db, "subscriptions", "trial_blocked_at", "TIMESTAMP")
    await _add_column(db, "subscriptions_archive", "trial_device_checked", "INTEGER")
    await _add_column(db, "subscriptions_archive", "trial_blocked_at", "TIMESTAMP")


async def _m015_whitelist_gb_per_day(db):
    """
    БС: каждый день подписки даёт лимит тарифа / 30 ГБ. Разового пула для неоплаченных дней больше нет —
    за уже идущие неоплаченные дни (триал, бонусы) начисляем бонусные ГБ по новому правилу.
    """
    await db.execute("""
        UPDATE subscriptions SET bs_bonus_bytes = COALESCE(bs_bonus_bytes, 0) + CAST(
            (julianday(subscription_expiry) - julianday(MAX(datetime('now'), COALESCE(bs_paid_until, datetime('now')))))
            * (SELECT bs_limit_gb FROM tariff_types t WHERE t.id = subscriptions.tariff_type_id)
            * 1073741824 / 30 AS INTEGER)
        WHERE (SELECT bs_limit_gb FROM tariff_types t WHERE t.id = subscriptions.tariff_type_id) > 0
          AND julianday(subscription_expiry) > julianday(MAX(datetime('now'), COALESCE(bs_paid_until, datetime('now'))))
    """)


async def _m016_mailing_gifts(db):
    """Подарок дней вместе с рассылкой: кому и сколько начислено (повторно в той же рассылке не начисляем)"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mailing_gifts (
            mailing_id TEXT NOT NULL,
            telegram_id INTEGER NOT NULL,
            subscription_id INTEGER,
            days INTEGER,                                -- в днях тарифа подписки
            bs_bytes INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',      -- pending, granted
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (mailing_id, telegram_id)
        )
    """)


_MIGRATIONS = [
    (1, _m001_payment_activation),
    (2, _m002_tariff_full_description),
    (3, _m003_trial_nudge),
    (4, _m004_promo_codes),
    (5, _m005_link_revokes),
    (6, _m006_whitelist),
    (7, _m007_whitelist_warning),
    (8, _m008_whitelist_bonus),
    (9, _m009_retention),
    (10, _m010_abuse),
    (11, _m011_referral_rewards),
    (12, _m012_session_flags),
    (13, _m013_one_subscription),
    (14, _m014_trial_by_device),
    (15, _m015_whitelist_gb_per_day),
    (16, _m016_mailing_gifts),
]


async def run_migrations(db):
    async with db.execute("PRAGMA user_version") as cursor:
        current = (await cursor.fetchone())[0]

    for version, migration in _MIGRATIONS:
        if version <= current:
            continue
        await migration(db)
        await db.execute(f"PRAGMA user_version = {version}")
        await db.commit()
        logger.info(f"🧱 Применена миграция схемы БД #{version}: {migration.__name__}")


async def create_default_tariffs(db):
    """Создание дефолтных тарифов"""
    # Проверяем, есть ли уже типы тарифов
    async with db.execute("SELECT COUNT(*) FROM tariff_types") as cursor:
        count = await cursor.fetchone()
        if count[0] == 0:
            # Создаем тип "Default"
            await db.execute("""
                INSERT INTO tariff_types (name, squad_id, hwid_limit, description)
                VALUES (?, ?, ?, ?)
            """, ("Default", "REPLACE-WITH-YOUR-SQUAD-UUID", 3, "Стандартная подписка"))

            # Создаем тип "Premium"
            await db.execute("""
                INSERT INTO tariff_types (name, squad_id, hwid_limit, description)
                VALUES (?, ?, ?, ?)
            """, ("Premium", "REPLACE-WITH-YOUR-SQUAD-UUID", 5, "Премиум подписка"))

            # Получаем ID созданных типов
            async with db.execute("SELECT id FROM tariff_types WHERE name = ?", ("Default",)) as cursor:
                default_id = (await cursor.fetchone())[0]
            async with db.execute("SELECT id FROM tariff_types WHERE name = ?", ("Premium",)) as cursor:
                premium_id = (await cursor.fetchone())[0]

            # Добавляем планы для Default (безлимитный трафик = 0)
            plans_default = [
                (default_id, 30, 150, 0),   # безлимит
                (default_id, 90, 250, 0),   # безлимит
                (default_id, 180, 450, 0)   # безлимит
            ]

            # Добавляем планы для Premium (можно тоже безлимит или с лимитом)
            plans_premium = [
                (premium_id, 30, 300, 0),   # безлимит
                (premium_id, 90, 500, 0),   # безлимит
                (premium_id, 180, 900, 0)   # безлимит
            ]

            for plan in plans_default + plans_premium:
                await db.execute("""
                    INSERT INTO tariff_plans (tariff_type_id, days, price, traffic_limit_bytes)
                    VALUES (?, ?, ?, ?)
                """, plan)

            await db.commit()
            logger.info("✅ Дефолтные тарифы и планы созданы")
            logger.warning(
                "⚠️ У тарифов 'Default'/'Premium' сейчас заглушка вместо squad_id. "
                "Откройте админ-панель бота и укажите реальный squad_id из вашей "
                "Remnawave-панели, иначе выдача подписки не сработает."
            )

# Новые функции для работы с тарифами
async def get_all_tariff_types(active_only: bool = True) -> List[Dict[str, Any]]:
    """Получить все типы тарифов"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM tariff_types"
        if active_only:
            query += " WHERE is_active = 1"

        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_tariff_plans(tariff_type_id: int) -> List[Dict[str, Any]]:
    """Получить все планы для конкретного типа тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
                              SELECT *
                              FROM tariff_plans
                              WHERE tariff_type_id = ?
                                AND is_active = 1
                              ORDER BY days
                              """, (tariff_type_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_tariff_plan(plan_id: int) -> Optional[Dict[str, Any]]:
    """Получить информацию о плане"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT tp.*, tt.name as tariff_name, tt.squad_id, tt.hwid_limit
            FROM tariff_plans tp
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE tp.id = ?
        """, (plan_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_tariff_type(type_id: int) -> Optional[Dict[str, Any]]:
    """Получить информацию о типе тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM tariff_types WHERE id = ?",
                (type_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ======================================================
# USERS
# ======================================================

async def generate_ref_code() -> str:
    return uuid.uuid4().hex[:8]


async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,)
        ) as cursor:
            return await cursor.fetchone()


async def get_user_by_ref_code(ref_code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM users WHERE ref_code = ?",
                (ref_code,)
        ) as cursor:
            return await cursor.fetchone()


async def add_user(telegram_id: int, username: str, referred_by: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        ref_code = await generate_ref_code()

        await db.execute("""
                         INSERT
                         OR IGNORE INTO users 
            (telegram_id, username, ref_code, referred_by)
            VALUES (?, ?, ?, ?)
                         """, (telegram_id, username, ref_code, referred_by))
        # username в Telegram мог смениться — храним актуальный (нужен для поиска в админке)
        await db.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username, telegram_id))

        await db.commit()


async def set_user_role(telegram_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET role = ? WHERE telegram_id = ?",
            (role, telegram_id)
        )
        await db.commit()


# ======================================================
# SUBSCRIPTIONS
# ======================================================


async def mark_subscription_trial_used(subscription_id: int):
    """Отметить подписку как использовавшую триал"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions
            SET trial_used = 1
            WHERE id = ?
        """, (subscription_id,))
        await db.commit()

async def get_user_subscriptions(telegram_id: int) -> List[Dict[str, Any]]:
    """Получить все подписки пользователя с информацией о тарифах"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*,
                   tt.name as tariff_name,
                   tt.hwid_limit as tariff_hwid_limit,
                   tp.days as tariff_days,
                   tp.price as tariff_price
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.telegram_id = ?
            ORDER BY s.created_at DESC
        """, (telegram_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_subscription(subscription_id: int) -> Optional[Dict[str, Any]]:
    """Получить информацию о конкретной подписке"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*,
                   tt.name as tariff_name,
                   tt.hwid_limit as tariff_hwid_limit,
                   tt.squad_id,
                   tt.bs_limit_gb,
                   tp.days as tariff_days,
                   tp.price as tariff_price
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.id = ?
        """, (subscription_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_subscription_by_short_uuid(short_uuid: str) -> Optional[Dict[str, Any]]:
    """Получить подписку по short_uuid"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
                              SELECT s.*, t.name as tariff_name
                              FROM subscriptions s
                                       JOIN tariffs t ON s.tariff_id = t.id
                              WHERE s.short_uuid = ?
                              """, (short_uuid,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def user_had_trial(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("subscriptions", "subscriptions_archive"):
            try:
                async with db.execute(f"SELECT 1 FROM {table} WHERE telegram_id = ? AND trial_used = 1 LIMIT 1",
                                      (telegram_id,)) as cursor:
                    if await cursor.fetchone():
                        return True
            except aiosqlite.OperationalError:
                pass  # архива ещё нет (до миграции #13)
    return False


async def get_user_main_subscription(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Подписка пользователя (одна на аккаунт; если по старым данным их больше — самая поздняя по сроку)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT id FROM subscriptions WHERE telegram_id = ? ORDER BY subscription_expiry DESC, id DESC LIMIT 1",
                (telegram_id,)) as cursor:
            row = await cursor.fetchone()
    return await get_subscription(row[0]) if row else None


async def get_tariff_coefficient(tariff_type_id: int) -> Optional[float]:
    """Коэффициент тарифа для пересчёта дней: поле switch_coef, иначе цена 30-дневного плана"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT switch_coef FROM tariff_types WHERE id = ?", (tariff_type_id,)) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            return float(row[0])
        async with db.execute("""
            SELECT price FROM tariff_plans WHERE tariff_type_id = ? AND is_active = 1
            ORDER BY ABS(days - 30), id LIMIT 1
        """, (tariff_type_id,)) as cursor:
            row = await cursor.fetchone()
        return float(row[0]) if row else None


async def change_subscription_tariff(subscription_id: int, tariff_type_id: int, plan_id: int, expiry: str,
                                     bs_paid_until: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions
            SET tariff_type_id = ?, tariff_plan_id = ?, subscription_expiry = ?, bs_paid_until = ?,
                tariff_changed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (tariff_type_id, plan_id, expiry, bs_paid_until, subscription_id))
        await db.commit()


async def get_user_subscription_by_tariff_type(telegram_id: int, tariff_type_id: int) -> Optional[Dict[str, Any]]:
    """Получить подписку пользователя на конкретный тип тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, tt.name as tariff_name
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            WHERE s.telegram_id = ? AND s.tariff_type_id = ?
        """, (telegram_id, tariff_type_id)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_subscription(
        telegram_id: int,
        tariff_type_id: int,  # было tariff_id
        tariff_plan_id: int,  # новый параметр
        short_uuid: str,
        expiry: str,
        username_in_panel: str,
        trial: bool = False
) -> int:
    """Создать новую подписку"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO subscriptions 
            (telegram_id, tariff_type_id, tariff_plan_id, short_uuid, subscription_expiry, username_in_panel, trial_used,
             bs_period_anchor)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (telegram_id, tariff_type_id, tariff_plan_id, short_uuid, expiry, username_in_panel, 1 if trial else 0))

        await db.commit()
        return cursor.lastrowid


async def update_subscription_expiry(subscription_id: int, new_expiry: str):
    """Обновить дату окончания подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         UPDATE subscriptions
                         SET subscription_expiry = ?
                         WHERE id = ?
                         """, (new_expiry, subscription_id))
        await db.commit()

async def update_subscription_plan(subscription_id: int, new_plan_id: int):
    """Обновить план подписки (например, при изменении лимита трафика)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions
            SET tariff_plan_id = ?
            WHERE id = ?
        """, (new_plan_id, subscription_id))
        await db.commit()
        logger.info(f"✅ Subscription {subscription_id} plan updated to {new_plan_id}")


async def get_subscriptions_by_telegram_id(telegram_id: int) -> List[Dict[str, Any]]:
    """Получить все подписки пользователя по Telegram ID (для админки)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*,
                   tt.name as tariff_name,
                   tt.hwid_limit as tariff_hwid_limit,
                   tp.days as tariff_days,
                   tp.price as tariff_price,
                   tp.traffic_limit_bytes
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.telegram_id = ?
            ORDER BY s.created_at DESC
        """, (telegram_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def update_subscription_status(subscription_id: int, status: str):
    """Обновить статус подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         UPDATE subscriptions
                         SET status = ?
                         WHERE id = ?
                         """, (status, subscription_id))
        await db.commit()


async def get_all_subscriptions() -> List[tuple]:
    """Получить все подписки (для reminder)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
                              SELECT id, telegram_id, subscription_expiry, status
                              FROM subscriptions
                              """) as cursor:
            return await cursor.fetchall()


# ======================================================
# REFERRALS
# ======================================================

async def increment_referral_activated(referrer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         UPDATE users
                         SET referrals_activated = referrals_activated + 1
                         WHERE telegram_id = ?
                         """, (referrer_id,))
        await db.commit()


# ======================================================
# ADMIN FUNCTIONS
# ======================================================

async def is_admin(telegram_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT role FROM users WHERE telegram_id = ?",
                (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            from config.config import ADMIN_ID
            return (row and row[0] == "admin") or telegram_id == ADMIN_ID


async def get_all_admins():
    """Возвращает список всех администраторов"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT telegram_id, username FROM users WHERE role = 'admin'"
        ) as cursor:
            return await cursor.fetchall()


async def get_all_users():
    """Возвращает список всех пользователей (только telegram_id)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM users") as cursor:
            return await cursor.fetchall()


async def get_banned_ids() -> set:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM users WHERE role = 'banned'") as cursor:
            return {row[0] for row in await cursor.fetchall()}


MAILING_AUDIENCES = {
    "all": "Все пользователи бота",
    "active": "С активной подпиской",
    "expired": "С истёкшей подпиской",
    "both": "С активной или истёкшей подпиской",
}


def _is_subscription_active(expiry: Optional[str], now: datetime) -> bool:
    if not expiry:
        return False
    try:
        return datetime.fromisoformat(expiry.replace("Z", "+00:00")) > now
    except ValueError:
        return False


async def get_mailing_recipients(audience: str) -> List[int]:
    """
    Получатели рассылки.
      all     — все пользователи (как и раньше, включая забаненных);
      active  — есть хотя бы одна действующая подписка;
      expired — подписки были, но ни одна уже не действует;
      both    — подписки есть/были (активные + истёкшие).
    Активность определяется по дате окончания, а не по полю status, которое
    может отставать (например, после ручного продления). Забаненные и
    пользователи без подписок в отфильтрованные группы не попадают.
    """
    if audience not in MAILING_AUDIENCES:
        raise ValueError(f"Неизвестная аудитория рассылки: {audience}")

    async with aiosqlite.connect(DB_PATH) as db:
        if audience == "all":
            async with db.execute("SELECT telegram_id FROM users") as cursor:
                return [row[0] for row in await cursor.fetchall()]

        async with db.execute("""
            SELECT u.telegram_id, s.subscription_expiry
            FROM users u
            JOIN subscriptions s ON s.telegram_id = u.telegram_id
            WHERE COALESCE(u.role, 'user') != 'banned' AND s.status != 'DISABLED'
        """) as cursor:
            rows = await cursor.fetchall()

    now = datetime.now().astimezone()
    has_active, has_any = set(), set()
    for telegram_id, expiry in rows:
        has_any.add(telegram_id)
        if _is_subscription_active(expiry, now):
            has_active.add(telegram_id)

    if audience == "active":
        return sorted(has_active)
    if audience == "expired":
        return sorted(has_any - has_active)
    return sorted(has_any)


# ======================================================
# НАПОМИНАНИЕ ТРИАЛЬЩИКАМ, КОТОРЫЕ НЕ ПОДКЛЮЧИЛИСЬ
# ======================================================

async def get_trial_nudge_candidates(min_age_hours: float, max_age_hours: float) -> List[Dict[str, Any]]:
    """
    Подписки-триалы, созданные от min_age до max_age часов назад, которым ещё не
    отправляли напоминание и у владельцев которых нет ни одной оплаты.
    (Триал, "навешенный" на уже существующую подписку, сюда не попадает: у неё
    created_at старый.)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.id, s.telegram_id, s.username_in_panel, s.subscription_expiry
            FROM subscriptions s
            WHERE s.trial_used = 1
              AND s.trial_nudge_status IS NULL
              AND s.status != 'DISABLED'
              AND s.created_at <= datetime('now', ?)
              AND s.created_at >= datetime('now', ?)
              AND NOT EXISTS (
                  SELECT 1 FROM payment_transactions p
                  WHERE p.telegram_id = s.telegram_id AND p.status = 'CONFIRMED'
              )
        """, (f"-{min_age_hours} hours", f"-{max_age_hours} hours")) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def set_trial_nudge_status(subscription_id: int, status: str):
    """status: 'sent' (напоминание отправлено) или 'connected' (уже пользуется, не беспокоим)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET trial_nudge_status = ? WHERE id = ?",
            (status, subscription_id)
        )
        await db.commit()


# ======================================================
# DEVICE RESET FUNCTIONS (обновленные)
# ======================================================

def _daily_cooldown(last_iso: Optional[str], action: str) -> tuple[bool, str]:
    """Действие доступно раз в 24 часа. Возвращает (можно ли, сообщение о времени ожидания)"""
    if not last_iso:
        return True, ""

    time_diff = datetime.now() - datetime.fromisoformat(last_iso)
    if time_diff.total_seconds() >= 24 * 60 * 60:
        return True, ""

    hours_left = 24 - (time_diff.total_seconds() / 3600)
    hours = int(hours_left)
    minutes = int((hours_left - hours) * 60)

    if hours > 0:
        return False, f"⏳ {action} будет доступен через {hours} ч. {minutes} мин."
    return False, f"⏳ {action} будет доступен через {minutes} мин."


async def can_reset_devices(subscription_id: int) -> tuple[bool, str]:
    """
    Проверяет, можно ли сбросить устройства для конкретной подписки
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT last_reset FROM device_resets WHERE subscription_id = ?",
                (subscription_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _daily_cooldown(row[0] if row else None, "Следующий сброс")


async def record_device_reset(subscription_id: int):
    """Записывает время сброса устройств для конкретной подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()

        await db.execute("""
                         INSERT INTO device_resets (subscription_id, last_reset, reset_count)
                         VALUES (?, ?, 1) ON CONFLICT(subscription_id) DO
                         UPDATE SET
                             last_reset = excluded.last_reset,
                             reset_count = reset_count + 1
                         """, (subscription_id, now))

        await db.commit()


async def get_last_reset_info(subscription_id: int) -> dict:
    """Получает информацию о последнем сбросе для подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT last_reset, reset_count FROM device_resets WHERE subscription_id = ?",
                (subscription_id,)
        ) as cursor:
            row = await cursor.fetchone()

            if row:
                return {
                    "last_reset": row[0],
                    "reset_count": row[1]
                }
            return {
                "last_reset": None,
                "reset_count": 0
            }


# ======================================================
# ПЕРЕВЫПУСК ССЫЛКИ ПОДПИСКИ
# ======================================================

async def can_revoke_link(subscription_id: int) -> tuple[bool, str]:
    """Можно ли пользователю перевыпустить ссылку подписки (раз в 24 часа; админ — без ограничения)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT last_revoke FROM link_revokes WHERE subscription_id = ?",
                (subscription_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _daily_cooldown(row[0] if row else None, "Следующий перевыпуск")


async def record_link_revoke(subscription_id: int):
    """Записать время перевыпуска ссылки пользователем"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO link_revokes (subscription_id, last_revoke, revoke_count)
            VALUES (?, ?, 1) ON CONFLICT(subscription_id) DO
            UPDATE SET
                last_revoke = excluded.last_revoke,
                revoke_count = revoke_count + 1
        """, (subscription_id, datetime.now().isoformat()))
        await db.commit()


# ======================================================
# НАСТРОЙКИ БОТА (ключ-значение)
# ======================================================

async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        await db.commit()


# ======================================================
# БЕЛЫЕ СПИСКИ (БС)
# ======================================================

async def get_bs_subscriptions() -> List[Dict[str, Any]]:
    """Все подписки (кроме отключённых) с лимитом БС их тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, tt.name AS tariff_name, tt.bs_limit_gb
            FROM subscriptions s
            JOIN tariff_types tt ON tt.id = s.tariff_type_id
            WHERE s.status != 'DISABLED' AND s.username_in_panel IS NOT NULL
        """) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def set_bs_state(subscription_id: int, state: Optional[str], period: Optional[str] = None):
    """state: None (по умолчанию), 'exhausted' (лимит исчерпан), 'admin_off' / 'admin_on' (решение админа)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET bs_state = ?, bs_state_period = ? WHERE id = ?",
            (state, period, subscription_id)
        )
        await db.commit()


def _sql_ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def add_bs_paid_days(subscription_id: int, days: int, now: datetime, from_now: bool = False):
    """
    Оплаченные дни (оплата или дни от админа): продлевают bs_paid_until.
    Досрочное продление (оплаченные дни ещё идут) только добавляет дни — 30-дневный период
    и уже потраченный в нём трафик не сбрасываются. Если оплаченных дней не было (триал,
    бонусы, оплаченный срок истёк) — оплаченный период начинается с этого момента.
    from_now — срок считается от текущего момента (лимитные тарифы: остаток сгорает, как и в панели).
    """
    subscription = await get_subscription(subscription_id)
    if not subscription:
        return
    if now.tzinfo:  # в БД время хранится в UTC без зоны, как CURRENT_TIMESTAMP
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    paid_until = subscription.get("bs_paid_until")
    paid_until = datetime.fromisoformat(paid_until) if paid_until else None
    was_paid = paid_until is not None and paid_until > now
    base = now if (from_now or not was_paid) else paid_until
    old_anchor = subscription.get("bs_period_anchor")
    anchor = datetime.fromisoformat(old_anchor) if (was_paid and old_anchor) else now

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET bs_paid_until = ?, bs_period_anchor = ? WHERE id = ?",
            (_sql_ts(base + timedelta(days=days)), _sql_ts(anchor), subscription_id)
        )
        await db.commit()


async def add_bs_bonus(subscription_id: int, amount: int):
    """Начислить бонусные байты белых списков"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE subscriptions SET bs_bonus_bytes = bs_bonus_bytes + ? WHERE id = ?",
                         (int(amount), subscription_id))
        await db.commit()


async def take_bs_bonus(subscription_id: int, amount: int):
    """Забрать бонусные байты белых списков (не больше, чем осталось)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions SET bs_bonus_bytes = MAX(bs_bonus_used, bs_bonus_bytes - ?) WHERE id = ?
        """, (int(amount), subscription_id))
        await db.commit()


async def close_bs_quota(subscription_id: int, bonus_spent: int, key: str, start: str, limit: int):
    """Квота сменилась: списать потраченные в закрытой квоте бонусы и запомнить новую квоту"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions
            SET bs_bonus_used = MIN(bs_bonus_bytes, bs_bonus_used + ?),
                bs_quota_key = ?, bs_quota_start = ?, bs_quota_limit = ?
            WHERE id = ?
        """, (int(bonus_spent), key, start, limit, subscription_id))
        await db.commit()


async def set_bs_warned(subscription_id: int, quota_key: str):
    """Предупреждение «осталось 20%» отправлено для этой квоты"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE subscriptions SET bs_warned_period = ? WHERE id = ?", (quota_key, subscription_id))
        await db.commit()


async def reduce_bs_paid_days(subscription_id: int, days: float):
    """Админ уменьшил дни подписки — уменьшаем и оплаченный срок"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions SET bs_paid_until = datetime(bs_paid_until, ?)
            WHERE id = ? AND bs_paid_until IS NOT NULL
        """, (f"-{int(round(days * 86400))} seconds", subscription_id))
        await db.commit()


async def count_bs_states() -> Dict[str, int]:
    """Сколько подписок в каждом состоянии БС (exhausted / admin_off / admin_on)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT bs_state, COUNT(*) FROM subscriptions WHERE bs_state IS NOT NULL GROUP BY bs_state"
        ) as cursor:
            return {row[0]: row[1] for row in await cursor.fetchall()}


async def set_panel_user_id(subscription_id: int, panel_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE subscriptions SET panel_user_id = ? WHERE id = ?", (panel_user_id, subscription_id))
        await db.commit()


async def update_subscription_short_uuid(subscription_id: int, short_uuid: str):
    """После перевыпуска ссылки у пользователя в панели новый shortUuid"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET short_uuid = ? WHERE id = ?",
            (short_uuid, subscription_id)
        )
        await db.commit()


# ======================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С ТРАНЗАКЦИЯМИ
# ======================================================

async def create_payment_transaction(
        transaction_id: str,
        telegram_id: int,
        plan_id: int,
        amount: int,
        payment_method: int,
        currency: str = "RUB",
        promo_id: Optional[int] = None,
        original_amount: Optional[int] = None
) -> int:
    """Создать запись о транзакции (amount — фактическая сумма к оплате, уже со скидкой по промокоду)"""
    payload = f"{telegram_id}_{plan_id}_{int(datetime.now().timestamp())}"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO payment_transactions
            (transaction_id, telegram_id, plan_id, amount, currency, payment_method, payload, status,
             promo_id, original_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
        """, (transaction_id, telegram_id, plan_id, amount, currency, payment_method, payload,
              promo_id, original_amount))

        await db.commit()
        return cursor.lastrowid


async def get_transaction(transaction_id: str) -> Optional[Dict[str, Any]]:
    """Получить информацию о транзакции по её ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT pt.*, tp.days, tp.price, tt.name as tariff_name, tt.squad_id, tt.hwid_limit
            FROM payment_transactions pt
            JOIN tariff_plans tp ON pt.plan_id = tp.id
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE pt.transaction_id = ?
        """, (transaction_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_transaction_by_payload(payload: str) -> Optional[Dict[str, Any]]:
    """Получить транзакцию по payload (наши данные)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM payment_transactions WHERE payload = ?
        """, (payload,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_transaction_status(transaction_id: str, status: str):
    """Обновить статус транзакции"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE payment_transactions 
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
        """, (status, transaction_id))

        if status == "CONFIRMED":
            await db.execute("""
                UPDATE payment_transactions 
                SET confirmed_at = CURRENT_TIMESTAMP
                WHERE transaction_id = ?
            """, (transaction_id,))

        await db.commit()


async def claim_activation(transaction_id: str) -> bool:
    """
    Атомарно "занять" активацию транзакции. True — активировать должен именно
    этот вызов; False — уже активирована либо активируется другим вызовом.
    Зависший claim (старше 10 минут, например после падения бота) перехватывается.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE payment_transactions
            SET activation_claimed_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
              AND activated_at IS NULL
              AND (activation_claimed_at IS NULL
                   OR activation_claimed_at < datetime('now', '-10 minutes'))
        """, (transaction_id,))
        await db.commit()
        return cursor.rowcount == 1


async def mark_activated(transaction_id: str):
    """Отметить, что подписка по транзакции успешно активирована"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE payment_transactions
            SET activated_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
        """, (transaction_id,))
        await db.commit()


async def release_activation_claim(transaction_id: str):
    """Снять claim после неудачной активации, чтобы её можно было повторить"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE payment_transactions
            SET activation_claimed_at = NULL
            WHERE transaction_id = ? AND activated_at IS NULL
        """, (transaction_id,))
        await db.commit()


async def get_pending_transactions() -> List[Dict[str, Any]]:
    """Получить все ожидающие транзакции (для проверки)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM payment_transactions 
            WHERE status = 'PENDING' 
            AND datetime(created_at) > datetime('now', '-1 day')
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

