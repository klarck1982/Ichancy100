"""Pure mapping for safe Mini App shortcuts.

This module deliberately has no Telegram or database imports, so the mapping can
be simulated without starting the bot or connecting to Neon.
"""

BOT_USERNAME = "Caesar_Robert_bot"

ACTION_TO_PAYLOAD = {
    "deposit": "app_deposit",
    "withdraw": "app_withdraw",
    "gift": "app_gift",
}

PAYLOAD_TO_ACTION = {payload: action for action, payload in ACTION_TO_PAYLOAD.items()}


def resolve_miniapp_shortcut(payload):
    """Return the whitelisted action for a Telegram /start payload."""
    normalized = str(payload or "").strip().lower()
    return PAYLOAD_TO_ACTION.get(normalized)


def build_miniapp_shortcut_url(action, bot_username=BOT_USERNAME):
    """Build a Telegram deep link only for an approved shortcut action."""
    normalized = str(action or "").strip().lower()
    payload = ACTION_TO_PAYLOAD.get(normalized)
    if not payload:
        return None
    username = str(bot_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start={payload}"
