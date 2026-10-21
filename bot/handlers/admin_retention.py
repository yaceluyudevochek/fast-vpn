"""Админка: возврат клиентов (вкл/выкл, статистика, примеры сообщений, запуск вручную)."""
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config.config import PANEL_API_KEY, PANEL_API_URL
from database import db as database
from database import retention_db
from services import retention
from services.panel_api import PanelAPI

router = Router()
panel_api = PanelAPI(PANEL_API_URL, PANEL_API_KEY)


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)] for t, d in rows])


async def _require_admin(callback: CallbackQuery) -> bool:
    if await database.is_admin(callback.from_user.id):
        return True
    await callback.answer("❌ Нет прав", show_alert=True)
    return False


async def _menu():
    enabled = await retention.is_enabled()
    stats = await retention_db.stats(days=30)
    lines = [
        "📣 <b>Возврат клиентов</b>\n",
        f"Статус: {'✅ <b>включено</b>' if enabled else '⛔ <b>выключено</b>'}\n",
        "<b>Что делает бот:</b>",
        f"• не продлил платную подписку — через 24 ч дарит {retention.gift_days()} дня + {retention.gift_gb():g} ГБ белых списков",
        f"• триал закончился — через 24 ч рассказ о подписке, ещё через 24 ч тот же подарок",
        "• триал идёт, а подключения нет — помощь на 1-й и 2-й день",
        "• нажал /start и не брал триал — напоминания на 1, 3 и 7 день",
        f"Подарок одному человеку — не чаще раза в {retention.gift_cooldown().days} дней; "
        "не больше одного сообщения в сутки; только с 10:00 до 21:00 МСК.\n",
        "<b>За 30 дней</b> (оплатили — в течение 14 дней после первого сообщения):",
    ]
    if not stats:
        lines.append("пока ничего не отправлялось")
    for key, title in retention.CAMPAIGNS.items():
        s = stats.get(key)
        if s:
            lines.append(f"• {title}: людей {s['users']}, сообщений {s['messages']}, "
                         f"подарков {s['gifts']}, оплатили {s['paid']}")
    rows = [("⛔ Выключить" if enabled else "✅ Включить", "admin_ret_toggle"),
            ("📨 Прислать мне примеры сообщений", "admin_ret_preview")]
    if enabled:
        rows.append(("🔄 Запустить проверку сейчас", "admin_ret_run"))
    rows.append(("« Назад", "admin_back"))
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data == "admin_retention")
async def admin_retention(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    text, keyboard = await _menu()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_ret_toggle")
async def admin_retention_toggle(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    enabled = not await retention.is_enabled()
    await retention.set_enabled(enabled)
    text, keyboard = await _menu()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer("✅ Включено" if enabled else "⛔ Выключено")


@router.callback_query(F.data == "admin_ret_preview")
async def admin_retention_preview(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.answer("Отправляю примеры...")
    await retention.send_previews(callback.bot, callback.from_user.id)


@router.callback_query(F.data == "admin_ret_run")
async def admin_retention_run(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.answer("⏳ Проверяю...")
    sent = await retention.run(callback.bot, panel_api)
    if sent is None:
        note = "⏸ Сейчас не время рассылки (10:00–21:00 МСК) или функция выключена."
    elif not sent:
        note = "Проверено: сейчас писать некому."
    else:
        note = "Отправлено: " + ", ".join(f"{k} — {v}" for k, v in sorted(sent.items()))
    text, keyboard = await _menu()
    await callback.message.edit_text(f"{note}\n\n{text}", reply_markup=keyboard, parse_mode="HTML")
