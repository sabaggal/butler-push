"""Only known first-party conversation origins may emit a push."""

BOT_PLATFORMS = frozenset({"ios", "desktop", "tui", "butler"})
WEBUI_PLATFORMS = frozenset({"webui"})
GROUP_PLATFORMS = frozenset({"bot_room"})
ALLOWED_PLATFORMS = BOT_PLATFORMS | WEBUI_PLATFORMS | GROUP_PLATFORMS


def coarse_source(platform: str | None) -> str:
    name = (platform or "").strip().lower()
    if name in BOT_PLATFORMS:
        return "bot"
    if name in WEBUI_PLATFORMS:
        return "webui"
    return "other"


def should_notify(session_id: str | None, platform: str | None) -> bool:
    return (bool(session_id) and not session_id.startswith(("cron_", "kanban_"))
            and (platform or "").strip().lower() in ALLOWED_PLATFORMS)
