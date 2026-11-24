"""Relay-facing event shapes. Pure functions; the only inputs with content are sealed here.

Notification event (``POST /installs/{install_key}/notify``)::

    {"v": 1, "kind": "reply" | "approval" | "clarify" | "turn_error",
     "event_id": <32 hex>, "thread_id": <32 hex>, "collapse_id": <32 hex>,
     "session_id": str, "source": "bot" | "webui" | "other", "is_subagent": bool,
     "sent_at": <unix seconds>, "sealed": <base64> | null}

``sealed`` decrypts to ``{"title", "subtitle", "body", "profile", "request_id", "bot_name"}``.
``bot_name`` is the name the Butler roster shows for the bot, omitted when unknown; the phone
builds a title in its own language from it and ``kind``, and older builds show the English
``title``. A null ``sealed`` means the host could not encrypt; the phone shows a generic
"New activity" banner.

Progress event (Live Activity state, same route)::

    {"v": 1, "kind": "progress", "event_id", "thread_id", "session_id", "source", "is_subagent",
     "sent_at", "status": "running" | "waiting" | "done" | "failed",
     "tool": <built-in tool name> | null, "tool_calls": int, "started_at": <unix seconds>}

Progress carries no ``sealed`` blob: the relay has to build the activity's content state itself.
It never carries tool arguments or results, only the tool's name.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .keys import Keys
from .privacy import keyed_id, seal

PAYLOAD_VERSION = 1
NOTIFY_KINDS = frozenset({"reply", "approval", "clarify", "turn_error"})
PROGRESS_STATUSES = frozenset({"running", "waiting", "done", "failed"})

TITLE_CHARS = 80
SUBTITLE_CHARS = 120
BODY_CHARS = 400
# Bound the amount of response text inspected independently of the banner limit.
EXCERPT_INPUT_CHARS = 8000


def _clip(value: Any, limit: int) -> str:
    """Plain text only: no interpretation of code snippets or Markdown delimiters."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value[:EXCERPT_INPUT_CHARS].replace("\x00", "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def assistant_excerpt(value: Any) -> str:
    """Only supported textual assistant output can be retained for an optional preview."""
    if isinstance(value, str):
        return _clip(value, BODY_CHARS)
    # Read at most the head of a multimodal answer, and never stringify unknown tool payloads.
    parts = value if isinstance(value, list) else []
    text: list[str] = []
    budget = EXCERPT_INPUT_CHARS
    for part in parts[:32]:
        if isinstance(part, dict) and part.get("type", "text") == "text":
            part = part.get("text")
        if isinstance(part, str):
            text.append(part[:budget])
            budget -= len(text[-1])
        if budget <= 0:
            break
    return _clip(" ".join(text), BODY_CHARS)


def preview(*, title: Any, body: Any, profile: str, subtitle: Any = "", request_id: str = "",
            bot_name: Any = "") -> dict[str, str]:
    """Bounded plain text and encrypted routing metadata for the paired app."""
    content = {
        "title": _clip(title, TITLE_CHARS),
        "subtitle": _clip(subtitle, SUBTITLE_CHARS),
        "body": _clip(body, BODY_CHARS),
        "profile": profile,
        "request_id": request_id,
    }
    name = _clip(bot_name, TITLE_CHARS)
    if name:
        content["bot_name"] = name
    return content

def _identity(keys: Keys, kind: str, session_id: str, event_ref: str) -> dict[str, str]:
    return {
        "event_id": keyed_id("event", f"{kind}:{session_id}:{event_ref}", install_key=keys.install_key),
        "thread_id": keyed_id("thread", session_id, install_key=keys.install_key),
        "collapse_id": keyed_id("collapse", session_id, install_key=keys.install_key),
    }


def notify_event(
    *, kind: str, session_id: str, event_ref: str, source: str, is_subagent: bool, keys: Keys,
    preview: Optional[dict[str, Any]], now: Optional[float] = None,
) -> dict[str, Any]:
    """Build one notification event. ``event_ref`` makes ``event_id`` stable across retries
    (turn id, approval request id, tool call id)."""
    if kind not in NOTIFY_KINDS:
        raise ValueError(f"unknown push kind {kind!r}")
    sealed = None
    if preview is not None:
        sealed = seal(preview, preview_key=keys.preview_key, install_key=keys.install_key)
    return {
        "v": PAYLOAD_VERSION,
        "kind": kind,
        **_identity(keys, kind, session_id, event_ref),
        "session_id": session_id,
        "source": source,
        "is_subagent": bool(is_subagent),
        "sent_at": int(now if now is not None else time.time()),
        "sealed": sealed,
    }


def progress_event(
    *, session_id: str, source: str, is_subagent: bool, keys: Keys, status: str, tool: Optional[str],
    tool_calls: int, started_at: float, now: Optional[float] = None,
) -> dict[str, Any]:
    if status not in PROGRESS_STATUSES:
        raise ValueError(f"unknown progress status {status!r}")
    sent_at = int(now if now is not None else time.time())
    identity = _identity(keys, "progress", session_id, f"{status}:{tool_calls}:{sent_at}")
    return {
        "v": PAYLOAD_VERSION,
        "kind": "progress",
        "event_id": identity["event_id"],
        "thread_id": identity["thread_id"],
        "session_id": session_id,
        "source": source,
        "is_subagent": bool(is_subagent),
        "sent_at": sent_at,
        "status": status,
        "tool": tool or None,
        "tool_calls": int(tool_calls),
        "started_at": int(started_at),
    }
