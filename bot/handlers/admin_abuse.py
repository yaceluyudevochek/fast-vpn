"""Админка: «плохиши» (игнор-лист, проверка вручную) и поиск Telegram ID по username."""
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config.config import PANEL_API_KEY, PANEL_API_URL
from database import abuse_db
from database import db as database
from services import abuse
from services.panel_api import PanelAPI

router = Router()
panel_api = PanelAPI(PANEL_API_URL, PANEL_API_KEY)


class AbuseInput(StatesGroup):
    lookup = State()
    ignore = State()


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def _require_admin(callback: CallbackQuery) -> bool:
    if await database.is_admin(callback.from_user.id):
        return True
    await callback.answer("❌ Нет прав", show_alert=True)
    return False


# ======================================================
# «ПЛОХИШИ»
# ======================================================

async def _menu():
    ignored = sorted(await abuse_db.ignored())
    infos = await abuse_db.users_info(ignored)
    text = (
        "🚨 <b>Плохиши</b>\n\n"
        "Бот раз в час проверяет и присылает всем админам:\n"
        f"• аномальный трафик — от <b>{abuse.day_gb():g} ГБ</b> за сутки или <b>{abuse.week_gb():g} ГБ</b> за 7 дней (все ноды);\n"
        "• одно устройство (HWID) у нескольких Telegram-аккаунтов;\n"
        f"• один IP у 2–{abuse.ip_group_max()} аккаунтов, из которых хотя бы двое не платили.\n"
        "Админы в оповещения не попадают.\n\n"
        f"<b>Игнорируются ({len(ignored)}):</b> "
        + (", ".join(f"<code>{t}</code>" + (f" @{html.escape(infos[t]['username'])}" if infos.get(t, {}).get("username") else "")
                     for t in ignored) or "никто")
    )
    rows = [[_btn("🔄 Проверить сейчас", "abuse_run")],
            [_btn("🔇 Добавить в игнор по Telegram ID", "abuse_ignore_add")]]
    rows += [[_btn(f"✖ Убрать из игнора {t}", f"abuse_unignore_{t}")] for t in ignored[:20]]
    rows.append([_btn("« Назад", "admin_back")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_abuse")
async def admin_abuse(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.clear()
    text, keyboard = await _menu()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("abuse_ignore_") & (F.data != "abuse_ignore_add"))
async def abuse_ignore(callback: CallbackQuery):
    """Кнопка в оповещении: больше не сообщать об этом пользователе"""
    if not await _require_admin(callback):
        return
    tid = int(callback.data.rsplit("_", 1)[1])
    await abuse_db.set_ignored(tid, True, callback.from_user.id)
    await callback.answer(f"🔇 {tid} больше не попадёт в оповещения", show_alert=True)


@router.callback_query(F.data.startswith("abuse_unignore_"))
async def abuse_unignore(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await abuse_db.set_ignored(int(callback.data.rsplit("_", 1)[1]), False)
    text, keyboard = await _menu()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer("Убран из игнора")


@router.callback_query(F.data == "abuse_ignore_add")
async def abuse_ignore_add(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.set_state(AbuseInput.ignore)
    await callback.message.answer("🔇 Введите <b>Telegram ID</b>, о котором не нужно присылать оповещения:", parse_mode="HTML")
    await callback.answer()


@router.message(AbuseInput.ignore)
async def abuse_ignore_input(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Нужен числовой Telegram ID")
        return
    await state.clear()
    await abuse_db.set_ignored(int(message.text.strip()), True, message.from_user.id)
    text, keyboard = await _menu()
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "abuse_run")
async def abuse_run(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.answer("⏳ Проверяю...")
    result = await abuse.run(callback.bot, panel_api)
    def fmt(v):
        return "панель не ответила" if v is None else str(v)
    await callback.message.answer(
        f"Проверено. Новых оповещений о трафике: {fmt(result.get('traffic'))}, "
        f"новых совпадений устройств/IP: {fmt(result.get('devices'))}."
    )


# ======================================================
# TELEGRAM ID ПО USERNAME
# ======================================================

@router.callback_query(F.data == "admin_lookup")
async def admin_lookup(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.set_state(AbuseInput.lookup)
    await callback.message.answer(
        "🔎 Введите <b>username</b> пользователя в Telegram (можно с @) "
        "или имя из панели вида <code>tg123456789_1</code>:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AbuseInput.lookup)
async def admin_lookup_input(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text:
        return
    await state.clear()
    users = await abuse_db.find_users(message.text)
    if not users:
        await message.answer(
            f"❌ Пользователь <b>{html.escape(message.text.strip())}</b> не найден.\n\n"
            "Бот знает username только тех, кто нажимал /start, и обновляет его при каждом /start — "
            "если человек сменил username и с тех пор не заходил в бота, найти его по новому не получится.",
            parse_mode="HTML"
        )
        return

    infos = await abuse_db.users_info(u["telegram_id"] for u in users)
    parts = []
    for user in users:
        tid = user["telegram_id"]
        info = infos.get(tid, {})
        lines = [
            f"👤 Telegram ID: <code>{tid}</code>",
            f"Username: @{html.escape(user['username'])}" if user.get("username") else "Username: —",
            f"В боте с: {str(user.get('created_at') or '—')[:10]} · роль: {user.get('role') or 'user'}",
            f"Платил: {'да' if info.get('has_paid') else 'нет'} · триал: {'был' if info.get('had_trial') else 'не брал'}",
        ]
        for sub in await database.get_user_subscriptions(tid):
            lines.append(f"• {html.escape(sub['tariff_name'])} — до {str(sub['subscription_expiry'] or '—')[:10]} "
                         f"(<code>{html.escape(sub['username_in_panel'] or '')}</code>)")
        parts.append("\n".join(lines))
    await message.answer("\n\n".join(parts), parse_mode="HTML")
