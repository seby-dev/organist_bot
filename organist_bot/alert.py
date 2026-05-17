import logging

import requests as _requests

from organist_bot.config import settings

logger = logging.getLogger(__name__)


def send_alert(message: str) -> None:
    """Post a plain-text alert to the configured Telegram chat.

    No-op if telegram_bot_token or telegram_chat_id is not configured.
    Any network or API failure is caught and logged at WARNING.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("send_alert: Telegram not configured — skipping")
        return
    try:
        resp = _requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": message},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram alert returned %s", resp.status_code)
    except Exception as exc:
        logger.warning(
            "Telegram alert failed",
            extra={"error": str(exc)},
        )
