"""Перевыпуск ссылки подписки — общий для пользователя (раз в сутки) и админа (без ограничения)."""
import logging
from typing import Any, Dict, Optional

from database import db

logger = logging.getLogger(__name__)


async def reissue_subscription_link(panel_api, subscription: Dict[str, Any]) -> Optional[str]:
    """
    Перевыпустить ссылку в панели и сохранить новый shortUuid в БД.
    Возвращает новую ссылку подписки или None, если панель не ответила.
    Ограничение "раз в сутки" здесь не проверяется — это делает вызывающий.
    """
    user = await panel_api.revoke_subscription_by_username(subscription["username_in_panel"])
    if not user:
        return None

    new_short_uuid = user.get("shortUuid")
    if new_short_uuid:
        await db.update_subscription_short_uuid(subscription["id"], new_short_uuid)
    else:
        logger.warning(f"⚠️ Панель не вернула shortUuid после перевыпуска для подписки {subscription['id']}")

    return user.get("subscriptionUrl")
