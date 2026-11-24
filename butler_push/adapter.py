"""The ``butler`` platform entry. Push is one-way, so the adapter is a stub that reserves the
platform name, surfaces ``BUTLER_PUSH_RELAY_URL`` in ``hermes config`` and lets the gateway
report the platform as connected. ``send`` (cron ``deliver: butler``, send_message) fails explicitly rather than treating delivery as successful."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import PLATFORM_NAME, RELAY_URL_ENV
from .hooks import relay_url_from_env

logger = logging.getLogger("butler_push")

SEND_UNSUPPORTED = "Butler Push is notification-only; cron and send_message delivery are not supported."


def check_requirements() -> bool:
    """Passive probe: stdlib plus cryptography, which hermes-agent already pins."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def is_connected(config: Any = None) -> bool:
    return bool(relay_url_from_env())


def env_enablement() -> Optional[dict]:
    """Auto-enable the platform when the relay URL is set, so ``gateway status`` shows it."""
    url = relay_url_from_env()
    return {"relay_url": url} if url else None


def make_adapter(config: Any):
    """``adapter_factory``; imports gateway modules lazily so the plugin loads without them."""
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class ButlerAdapter(BasePlatformAdapter):
        def __init__(self, cfg: Any) -> None:
            super().__init__(config=cfg, platform=Platform(PLATFORM_NAME))

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            if not relay_url_from_env():
                logger.warning("[%s] %s not configured", self.name, RELAY_URL_ENV)
                return False
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            self._mark_disconnected()

        async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> SendResult:
            return SendResult(success=False, error=SEND_UNSUPPORTED)

        async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
            return {"name": "Butler", "type": "dm"}

    return ButlerAdapter(config)
