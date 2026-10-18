"""Админка: белые списки (глобальный выключатель, проверка лимитов) и сквады пользователя по Telegram ID."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config.config import PANEL_API_KEY, PANEL_API_URL
from database import db as database
from services import whitelist
from services.panel_api import PanelAPI

router = Router()
logger = logging.getLogger(__name__)
panel_api = PanelAPI(PANEL_API_URL, PANEL_API_KEY)


class UserSquads(StatesGroup):
    user_id = State()


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def _require_admin(callback: CallbackQuery) -> bool:
    if await database.is_admin(callback.from_user.id):
        return True
    await callback.answer("❌ Нет прав", show_alert=True)
    return False


# ======================================================
# РАЗДЕЛ «БЕЛЫЕ СПИСКИ»
# ======================================================

async def _bs_menu():
    squad = whitelist.squad_uuid()
    if not squad:
        return (
            "🛡 <b>Белые списки</b>\n\n"
            "❌ Сквад белых списков не задан: укажите <code>BS_SQUAD_UUID</code> в config/config.py "
            "и перезапустите бота.",
            _kb([[_btn("« Назад", "admin_back")]])
        )

    enabled = await whitelist.is_enabled()
    squads = await panel_api.get_internal_squads() or []
    info = next((s for s in squads if s.get("uuid") == squad), None)
    if info:
        squad_line = (
            f"Сквад: <b>{info['name']}</b> — участников {(info.get('info') or {}).get('membersCount', '?')}, "
            f"инбаундов {len(info.get('inbounds', []))}"
        )
    else:
        squad_line = f"⚠️ Сквад <code>{squad}</code> не найден в панели"

    limits = []
    for tt in await database.get_all_tariff_types(active_only=False):
        value = f"{tt['bs_limit_gb']} ГБ" if tt.get("bs_limit_gb") else "не входят"
        limits.append(f"• {tt['name']}: {value}")
    states = await database.count_bs_states()

    text = (
        f"🛡 <b>Белые списки</b>\n\n"
        f"Статус: {'✅ <b>включены</b>' if enabled else '⛔ <b>выключены</b>'}\n"
        f"{squad_line}\n\n"
        f"<b>Лимиты за 30 дней</b> (меняются в карточке типа тарифа):\n"
        + "\n".join(limits) +
        f"\n• Ни разу не платившие (триал): {whitelist.format_gb(int(whitelist.trial_limit_gb() * whitelist.GB))} ГБ\n\n"
        f"Исчерпали лимит: {states.get('exhausted', 0)} · "
        f"отключены админом: {states.get('admin_off', 0)} · без лимита: {states.get('admin_on', 0)}"
    )

    rows = []
    if enabled:
        rows.append([_btn("🔄 Проверить лимиты сейчас", "admin_bs_check")])
        rows.append([_btn("⛔ Отключить у всех", "admin_bs_off")])
    else:
        rows.append([_btn("✅ Подключить всем", "admin_bs_on")])
    rows.append([_btn("🧩 Сквады пользователя", "admin_user_squads")])
    rows.append([_btn("« Назад", "admin_back")])
    return text, _kb(rows)


@router.callback_query(F.data == "admin_bs")
async def admin_bs(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    text, keyboard = await _bs_menu()
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_bs_on")
async def admin_bs_on(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.message.edit_text(
        "✅ <b>Подключить белые списки всем?</b>\n\n"
        "Сквад белых списков будет выдан всем пользователям бота с активной подпиской, "
        "которым он положен по тарифу (кроме исчерпавших лимит и отключённых вручную), "
        "и дальше будет выдаваться всем новым пользователям.",
        reply_markup=_kb([[_btn("✅ Да, подключить", "admin_bs_on_yes"), _btn("❌ Отмена", "admin_bs")]]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_bs_on_yes")
async def admin_bs_on_yes(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    if not whitelist.squad_uuid():
        await callback.answer("❌ BS_SQUAD_UUID не задан", show_alert=True)
        return
    await callback.message.edit_text("⏳ Подключаю белые списки...")
    await whitelist.set_enabled(True)
    result = await whitelist.check_all(panel_api)
    if result is None:
        note = "⚠️ Панель не ответила — сквад выдастся при следующей фоновой проверке."
    else:
        await whitelist.notify_users(callback.bot, result)
        note = f"Сквад выдан: {len(result.added)}, снят: {len(result.removed)}"
    text, keyboard = await _bs_menu()
    await callback.message.edit_text(f"✅ Белые списки включены. {note}\n\n{text}", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_bs_off")
async def admin_bs_off(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.message.edit_text(
        "⛔ <b>Отключить белые списки у всех?</b>\n\n"
        "Сквад белых списков будет снят у всех пользователей панели, у кого он есть, "
        "и перестанет выдаваться новым. Другие сквады не затрагиваются.",
        reply_markup=_kb([[_btn("⛔ Да, отключить", "admin_bs_off_yes"), _btn("❌ Отмена", "admin_bs")]]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_bs_off_yes")
async def admin_bs_off_yes(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.message.edit_text("⏳ Отключаю белые списки...")
    removed = await whitelist.disable_for_all(panel_api)
    note = "⚠️ Панель не ответила — повторите отключение." if removed is None else f"Сквад снят у {removed} польз."
    text, keyboard = await _bs_menu()
    await callback.message.edit_text(f"⛔ Белые списки выключены. {note}\n\n{text}", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_bs_check")
async def admin_bs_check(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    await callback.answer("⏳ Проверяю...")
    result = await whitelist.check_all(panel_api)
    if result is None:
        note = "⚠️ Проверка не выполнена (выключено или панель не ответила)."
    else:
        await whitelist.notify_users(callback.bot, result)
        note = (
            f"Проверено. Выдан: {len(result.added)}, снят: {len(result.removed)}, "
            f"исчерпали лимит: {len(result.exhausted)}, вернулись: {len(result.restored)}"
        )
    text, keyboard = await _bs_menu()
    await callback.message.edit_text(f"{note}\n\n{text}", reply_markup=keyboard, parse_mode="HTML")


# ======================================================
# СКВАДЫ ПОЛЬЗОВАТЕЛЯ ПО TELEGRAM ID
# ======================================================

@router.callback_query(F.data == "admin_user_squads")
async def admin_user_squads(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.set_state(UserSquads.user_id)
    await callback.message.answer("🧩 Введите <b>Telegram ID</b> пользователя:", parse_mode="HTML")
    await callback.answer()


@router.message(UserSquads.user_id)
async def admin_user_squads_id(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите корректный Telegram ID!")
        return
    await state.clear()

    telegram_id = int(message.text.strip())
    subscriptions = await database.get_user_subscriptions(telegram_id)
    if not subscriptions:
        await message.answer("❌ У пользователя нет подписок")
        return
    if len(subscriptions) == 1:
        text, keyboard = await _squads_card(subscriptions[0]["id"])
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        return

    rows = [[_btn(f"{s['tariff_name']} ({s['username_in_panel']})", f"adm_sqsub_{s['id']}")] for s in subscriptions]
    rows.append([_btn("« Назад", "admin_back")])
    await message.answer(f"👤 Пользователь <code>{telegram_id}</code>\nВыберите подписку:",
                         reply_markup=_kb(rows), parse_mode="HTML")


async def _squads_card(subscription_id: int):
    subscription = await database.get_subscription(subscription_id)
    back = _kb([[_btn("« Назад", "admin_back")]])
    if not subscription:
        return "❌ Подписка не найдена", back

    user = await panel_api.get_user_by_username(subscription["username_in_panel"])
    squads = await panel_api.get_internal_squads()
    if not user or squads is None:
        return f"❌ Пользователь <code>{subscription['username_in_panel']}</code> не найден в панели или панель не ответила", back

    active = {s["uuid"] for s in user.get("activeInternalSquads", [])}
    bs_squad = whitelist.squad_uuid()

    lines = [
        f"🧩 <b>Сквады пользователя</b>\n",
        f"👤 Telegram ID: <code>{subscription['telegram_id']}</code>",
        f"📦 Подписка: {subscription['tariff_name']} (<code>{subscription['username_in_panel']}</code>)",
    ]
    status = await whitelist.get_status(panel_api, subscription) if bs_squad else None
    if status:
        lines.append(whitelist.status_text(status).strip())
    lines.append("\nНажмите на сквад, чтобы включить или отключить его:")

    rows = []
    for squad in squads:
        mark = "✅" if squad["uuid"] in active else "▫️"
        label = f"{mark} {squad['name']}" + (" 🛡" if squad["uuid"] == bs_squad else "")
        rows.append([_btn(label, f"adm_sqt_{subscription_id}_{squad['uuid']}")])
    rows.append([_btn("« Назад", "admin_back")])
    if bs_squad:
        lines.append(
            "🛡 — сквад белых списков: отключение админом бот не отменит сам; "
            "включение админом действует без лимита до конца текущего периода."
        )
    return "\n".join(lines), _kb(rows)


@router.callback_query(F.data.startswith("adm_sqsub_"))
async def admin_user_squads_sub(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    text, keyboard = await _squads_card(int(callback.data.split("_")[2]))
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_sqt_"))
async def admin_user_squad_toggle(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    _, _, subscription_id, squad = callback.data.split("_", 3)
    subscription = await database.get_subscription(int(subscription_id))
    user = await panel_api.get_user_by_username(subscription["username_in_panel"]) if subscription else None
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    enable = squad not in {s["uuid"] for s in user.get("activeInternalSquads", [])}
    if not await whitelist.admin_set_user_squad(panel_api, subscription, squad, enable):
        await callback.answer("❌ Панель не приняла изменение", show_alert=True)
        return

    logger.info(f"🧩 Админ {callback.from_user.id}: сквад {squad} {'включён' if enable else 'отключён'} "
                f"для {subscription['username_in_panel']}")
    text, keyboard = await _squads_card(subscription["id"])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer("✅ Сквад включён" if enable else "✅ Сквад отключён")
