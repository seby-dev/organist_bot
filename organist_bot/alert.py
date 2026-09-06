import logging
import re

import requests as _requests

from organist_bot.config import settings

logger = logging.getLogger(__name__)

# Telegram MarkdownV2 requires every one of these literal characters to be
# backslash-escaped outside of an entity (bold/italic/etc.) — see
# https://core.telegram.org/bots/api#markdownv2-style. Used to safely embed
# free-form text (e.g. an exception message) inside a MarkdownV2 alert
# without risking a 400 from an unbalanced/reserved character.
_MARKDOWN_V2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_markdown_v2(text: str) -> str:
    """Escape `text` for safe inclusion in a MarkdownV2 message."""
    return _MARKDOWN_V2_SPECIAL.sub(r"\\\1", text)


def send_alert(
    message: str, reply_markup: dict | None = None, parse_mode: str | None = None
) -> None:
    """Post an alert to the configured Telegram chat, optionally with an
    inline keyboard (reply_markup, in Telegram Bot API shape) and/or a
    Telegram parse_mode ("MarkdownV2"/"HTML") for formatting. Plain text by
    default (parse_mode=None) since most callers embed free-form/exception
    text that hasn't been escaped for any markup language — pass
    parse_mode="MarkdownV2" only with content already escaped via
    escape_markdown_v2 for any dynamic portion.

    No-op if telegram_bot_token or telegram_chat_id is not configured.
    Any network or API failure is caught and logged at WARNING.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("send_alert: Telegram not configured — skipping")
        return
    payload: dict = {"chat_id": settings.telegram_chat_id, "text": message}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    try:
        resp = _requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json=payload,
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram alert returned %s", resp.status_code)
    except Exception as exc:
        logger.warning(
            "Telegram alert failed",
            extra={"error": str(exc)},
        )
