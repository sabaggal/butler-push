"""Opt-in, rate-bounded outcome markers with no conversation or relay data."""
from enum import Enum
import logging
import os
import threading
import time

_logger = logging.getLogger("butler_push")
_INTERVAL = 5.0


class Outcome(Enum):
    PRE_RELAY_UNCONFIGURED = "pre_relay_unconfigured"
    PRE_ALLOWED = "pre_allowed"
    PRE_FILTERED = "pre_filtered"
    POST_RELAY_UNCONFIGURED = "post_relay_unconfigured"
    POST_CAPTURED = "post_captured"
    POST_FILTERED = "post_filtered"
    END_INTERRUPTED = "end_interrupted"
    END_INCOMPLETE = "end_incomplete"
    END_COMPLETED_NO_CAPTURE = "end_completed_no_capture"
    END_COMPLETED_CAPTURED = "end_completed_captured"
    END_FAILED = "end_failed"
    BANNER_RELAY_UNCONFIGURED = "banner_relay_unconfigured"
    BANNER_FILTERED = "banner_filtered"
    BANNER_KEYS_UNAVAILABLE = "banner_keys_unavailable"
    BANNER_QUEUED = "banner_queued"
    BANNER_NOT_QUEUED = "banner_not_queued"
    BANNER_ACK = "banner_relay_ack"
    SEND_GUARD_REFUSED = "send_guard_refused"


_lock = threading.Lock()
_last: dict[Outcome, float] = {}


def trace(outcome: Outcome) -> None:
    if os.environ.get("BUTLER_PUSH_DIAGNOSTICS") != "1" or not _logger.isEnabledFor(logging.INFO):
        return
    now = time.monotonic()
    with _lock:
        if now - _last.get(outcome, float("-inf")) < _INTERVAL:
            return
        _last[outcome] = now
    _logger.info("butler-push hook outcome=%s", outcome.value)
