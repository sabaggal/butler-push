"""Hook callbacks. One :class:`ButlerPush` instance owns the per-session state that turns
hermes-agent lifecycle hooks into relay events.

Per turn: ``pre_llm_call`` records the platform and starts progress, ``post_llm_call`` captures
the final reply text, ``on_session_end`` is the single boundary that sends exactly one ``reply``
or ``turn_error`` (or nothing, for an interrupted turn). Approvals come from the
``pre_approval_request`` observer, questions from ``pre_tool_call`` on the ``clarify`` tool.
Every callback swallows its own errors: a push must never break a turn. A session whose
platform was never seen gets nothing (fail closed), so an evicted chat-platform session can
never be pushed to the phone by mistake.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import RELAY_URL_ENV, REPLY_PREVIEWS_ENV
from .keys import Keys, hermes_root, load_or_create_keys
from .payload import assistant_excerpt, notify_event, preview, progress_event
from .privacy import keyed_id
from .progress import ProgressCoalescer, ProgressSnapshot
from .relay import RelaySender, ResultFn, allowed_relay_url, notify_url
from .sources import coarse_source, should_notify

logger = logging.getLogger("butler_push")

CLARIFY_TOOL = "clarify"
# The hermes-agent hooks this plugin observes; each is a same-named ``ButlerPush`` method.
HOOK_NAMES = (
    "pre_llm_call", "post_llm_call", "on_session_end", "pre_tool_call", "post_tool_call",
    "pre_approval_request", "post_approval_response", "subagent_start",
)
MAX_TRACKED_SESSIONS = 4096
RELAY_URL_CACHE_SECONDS = 5.0
# The English label after the bot's name in each sealed title; a reply is titled by the name
# alone. Butler builds the same title in the phone's language from ``kind`` and ``bot_name``, so
# this English copy only shows on app builds that predate ``bot_name``.
TITLE_LABELS = {"approval": "Approval needed", "clarify": "Question", "turn_error": "Turn failed"}


LEGACY_RELAY_URL_ENV = "HERMEX_PUSH_RELAY_URL"


def _root_settings(*keys: str) -> dict[str, str]:
    """Read only named non-secret settings from the one shared Hermes root."""
    result = {}
    try:
        with (hermes_root() / ".env").open(encoding="utf-8") as config:
            for line in config:
                name, sep, value = line.strip().partition("=")
                name = name.strip().removeprefix("export ").strip()
                if not sep or name not in keys:
                    continue
                value = value.strip()
                if value[:1] in {"'", '"'}:
                    value = value[1:].partition(value[0])[0]
                else:
                    value = value.split("#", 1)[0].strip() if value.startswith("#") else value.split(" #", 1)[0].strip()
                if name != LEGACY_RELAY_URL_ENV or value or name not in result:
                    result[name] = value
    except (OSError, UnicodeError):
        pass
    return result


def _host_setting(key: str) -> str:
    return _root_settings(key).get(key, os.environ.get(key, "").strip())


def legacy_sender_configured(*, root_values: Optional[dict[str, str]] = None) -> bool:
    values = _root_settings(LEGACY_RELAY_URL_ENV) if root_values is None else root_values
    # Either location can keep an earlier Hermex process configured. A blank root
    # value deliberately does not clear a still-present environment override.
    return bool(values.get(LEGACY_RELAY_URL_ENV) or os.environ.get(LEGACY_RELAY_URL_ENV, "").strip())


def relay_url_from_env() -> str:
    values = _root_settings(RELAY_URL_ENV, LEGACY_RELAY_URL_ENV)
    if legacy_sender_configured(root_values=values):
        return ""
    value = values.get(RELAY_URL_ENV, os.environ.get(RELAY_URL_ENV, "").strip())
    if value and not allowed_relay_url(value):
        logger.warning("butler-push: invalid relay configuration; refusing to send")
        return ""
    return value.rstrip("/")


def reply_previews_enabled() -> bool:
    return _host_setting(REPLY_PREVIEWS_ENV) == "1"


def bot_name_for(profile: str) -> str:
    """The name Butler's bot roster shows for a Profile: its Desktop title, then its
    ``display_name``, then the slug, with the default Profile as "Hermes". Read from the host's
    ``profile.yaml`` on every banner (hermes-agent caches the read by file signature), so a rename
    shows on the next one. ``hermes_cli.profiles`` is a hermes-agent internal: a host without it,
    or a read that fails, gets the slug."""
    fallback = profile if profile and profile != "default" else "Hermes"
    try:
        from hermes_cli.profiles import get_profile_dir, read_profile_meta
        meta = read_profile_meta(get_profile_dir(profile or "default"))
        names = (str(meta.get(key) or "").strip() for key in ("bot_title", "display_name"))
        return next((name for name in names if name), fallback)
    except Exception:
        return fallback


class _Recent(OrderedDict):
    """Bounded insertion-ordered map so long-lived gateways never grow without limit."""

    def remember(self, key: str, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > MAX_TRACKED_SESSIONS:
            self.popitem(last=False)


class ButlerPush:
    # Marks this plugin's hook callbacks across module copies (see ``scopes``).
    is_butler_push = True

    def __init__(
        self, *, profile: str = "", sender: Optional[RelaySender] = None,
        keys_loader: Callable[[], Keys] = load_or_create_keys,
        relay_url: Callable[[], str] = relay_url_from_env, now: Callable[[], float] = time.time,
        schedule: Optional[Callable[[float, Callable[[], None]], Any]] = None,
        bot_name: Optional[Callable[[], str]] = None,
        reply_previews: Callable[[], bool] = reply_previews_enabled,
    ) -> None:
        self._profile = "" if profile == "default" else profile
        self._reply_previews = reply_previews
        self._bot_name = bot_name or (lambda: bot_name_for(self._profile))
        self._sender = sender or RelaySender(permit=self._delivery_permitted)
        self._keys_loader = keys_loader
        self._relay_url = relay_url
        self._relay_url_cache: tuple[float, str] = (float("-inf"), "")
        self._now = now
        self._schedule = schedule or self._timer
        self._lock = threading.RLock()
        self._keys: Optional[Keys] = None
        self._platform_by_session = _Recent()
        self._reply_by_session = _Recent()
        self._subagent_sessions = _Recent()
        self._progress = ProgressCoalescer()
        self._flush_at: Optional[float] = None  # when the armed flush fires; None when unarmed
        self._flush_timer: Optional[threading.Timer] = None
        self._closed = False

    # -- wiring ---------------------------------------------------------------------------

    def hook_callbacks(self) -> dict[str, Callable[..., None]]:
        return {name: getattr(self, name) for name in HOOK_NAMES}

    def close(self) -> None:
        """Plugin unload: stop the flush timer and the relay thread so a reload leaks nothing."""
        with self._lock:
            self._closed = True
            timer, self._flush_timer = self._flush_timer, None
        if timer is not None:
            timer.cancel()
        self._sender.close()

    def _timer(self, delay: float, fn: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()
        return timer

    def _keys_or_none(self) -> Optional[Keys]:
        if self._keys is None:
            try:
                self._keys = self._keys_loader()
            except Exception:
                logger.warning("butler-push: keys unavailable; nothing will be sent")
        return self._keys

    def _relay(self) -> str:
        """Relay URL, re-resolved at most every few seconds so the turn path never re-reads a file."""
        with self._lock:
            resolved_at, url = self._relay_url_cache
            if self._now() - resolved_at < RELAY_URL_CACHE_SECONDS:
                return url
        url = self._relay_url()
        with self._lock:
            self._relay_url_cache = (self._now(), url)
        return url

    def _delivery_permitted(self, target: str) -> bool:
        # Re-read at the last network boundary, not on Hermes' turn thread. A
        # cached URL or an already-queued event must not survive a live change.
        url = self._relay_url()
        return bool(url and self._keys and target == notify_url(url, self._keys.install_key))

    def _send(self, event: dict[str, Any], on_result: Optional[ResultFn] = None) -> bool:
        url = self._relay()
        if not url or self._closed:
            logger.debug("butler-push: relay not configured; dropping a %s event", event.get("kind"))
            return False
        keys = self._keys_or_none()
        return keys is not None and self._sender.enqueue(notify_url(url, keys.install_key), event, on_result)

    def _session(self, session_id: str) -> tuple[str, str, bool]:
        platform = self._platform_by_session.get(session_id, "")
        return platform, coarse_source(platform), session_id in self._subagent_sessions

    def _notify(self, kind: str, session_id: str, event_ref: str, **content: Any) -> None:
        """Seal one banner. ``content`` is the ``preview`` fields besides the title, which is
        the bot's name and the kind's label; the name is only read when a banner goes out."""
        platform, source, is_subagent = self._session(session_id)
        if not self._relay() or not should_notify(session_id, platform):
            return
        keys = self._keys_or_none()
        if keys is None:
            return
        name = self._bot_name() or self._profile or "Hermes"
        label = TITLE_LABELS.get(kind)
        sealed = preview(title=f"{name} · {label}" if label else name, bot_name=name,
                         profile=self._profile, **content)
        self._send(notify_event(
            kind=kind, session_id=session_id, event_ref=event_ref, source=source, is_subagent=is_subagent,
            keys=keys, preview=sealed, now=self._now(),
        ))

    # -- progress -------------------------------------------------------------------------

    def _emit_progress(self, snapshot: Optional[ProgressSnapshot]) -> None:
        if snapshot is None:
            self._arm_flush()
            return
        platform, source, is_subagent = self._session(snapshot.session_id)
        if not self._relay() or not should_notify(snapshot.session_id, platform):
            return
        keys = self._keys_or_none()
        if keys is None:
            return
        session_id = snapshot.session_id
        self._send(progress_event(
            session_id=session_id, source=source, is_subagent=is_subagent, keys=keys,
            status=snapshot.status, tool=None, tool_calls=snapshot.tool_calls,
            started_at=snapshot.started_at, now=self._now(),
        ), on_result=lambda result: self._progress_answered(session_id, result))

    def _progress_answered(self, session_id: str, result: str) -> None:
        """The relay's answer to a delivered progress event (drain thread). ``no_activity`` means
        no phone shows a Live Activity for the session, so its routine updates quiet down; any
        other answer, including an older relay's ``accepted``, keeps the normal cadence."""
        with self._lock:
            if result == "no_activity":
                self._progress.unwatched(session_id, self._now())
            else:
                self._progress.watched(session_id)
        self._arm_flush()

    def _arm_flush(self) -> None:
        """Schedule a flush for when the earliest held update's hold ends, replacing an armed
        flush that would fire later (another session's hold can end first). A stray flush is
        harmless: it sends only what is due and re-arms."""
        with self._lock:
            if self._closed:
                return
            now = self._now()
            delay = self._progress.flush_delay(now)
            if delay is None or (self._flush_at is not None and self._flush_at <= now + delay):
                return
            self._flush_at = now + delay
            replaced, self._flush_timer = self._flush_timer, None
        if replaced is not None:
            replaced.cancel()
        try:
            timer = self._schedule(delay, self.flush_progress)
        except Exception:
            with self._lock:
                self._flush_at = None
            logger.debug("butler-push: could not schedule a progress flush")
            return
        with self._lock:
            self._flush_timer = timer if isinstance(timer, threading.Timer) else None

    def flush_progress(self) -> None:
        with self._lock:
            self._flush_at = None
            self._flush_timer = None
            snapshots = self._progress.due(self._now())
        for snapshot in snapshots:
            self._emit_progress(snapshot)
        self._arm_flush()

    # -- hook callbacks (kwargs only; unknown kwargs ignored) ------------------------------

    def _remember_platform(self, session_id: str, platform: str) -> None:
        if platform:
            self._platform_by_session.remember(session_id, platform)

    def pre_llm_call(self, *, session_id: str = "", platform: str = "", **_: Any) -> None:
        if not self._relay():
            return
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
                self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_started(session_id, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("butler-push: pre_llm_call failed")

    def post_llm_call(self, *, session_id: str = "", assistant_response: Any = "",
                      platform: str = "", **_: Any) -> None:
        if not self._relay():
            return
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
                if should_notify(session_id, self._session(session_id)[0]):
                    # Never retain the user message. Store only a small optional assistant excerpt.
                    reply = assistant_excerpt(assistant_response) if self._reply_previews() else ""
                    self._reply_by_session.remember(session_id, reply)
        except Exception:
            logger.warning("butler-push: post_llm_call failed")

    def on_session_end(self, *, session_id: str = "", completed: bool = False, interrupted: bool = False,
                       failed: bool = False, turn_id: str = "", platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
                captured = self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_ended(session_id, self._now(), failed=bool(failed))
            self._emit_progress(snapshot)
            if interrupted:
                return
            ref = turn_id or str(int(self._now()))
            if failed:
                self._notify("turn_error", session_id, ref, body="")
            elif completed and captured is not None:
                self._notify("reply", session_id, ref, body=captured if self._reply_previews() else "")
        except Exception:
            logger.warning("butler-push: on_session_end failed; a push was dropped")

    def pre_tool_call(self, *, tool_name: str = "", session_id: str = "",
                      tool_call_id: str = "", **_: Any) -> None:
        try:
            now = self._now()
            if tool_name == CLARIFY_TOOL:
                with self._lock:
                    snapshot = self._progress.waiting(session_id, now)
                self._emit_progress(snapshot)
                self._notify("clarify", session_id, tool_call_id or f"{tool_name}:{int(now)}",
                             body="", request_id=tool_call_id)
                return
            with self._lock:
                snapshot = self._progress.tool_started(session_id, tool_name, now)
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("butler-push: pre_tool_call failed")

    def post_tool_call(self, *, tool_name: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if tool_name == CLARIFY_TOOL:
                    snapshot = self._progress.resumed(session_id, self._now())
                else:
                    snapshot = self._progress.tool_finished(session_id, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("butler-push: post_tool_call failed")

    def _approval_session(self, session_id: str, session_key: str) -> str:
        """The agent session id when the host bound one, else an opaque id derived from the
        gateway session key, which can embed chat ids or phone numbers and must never go out raw."""
        if session_id:
            return session_id
        if not self._relay():
            return ""
        keys = self._keys_or_none()
        if not session_key or keys is None:
            return ""
        return keyed_id("session", session_key, install_key=keys.install_key)

    def pre_approval_request(self, *, session_key: str = "",
                             session_id: str = "", surface: str = "", request_id: str = "",
                             tool_call_id: str = "", **_: Any) -> None:
        if surface == "smart":  # auxiliary-model decision, nobody is waiting
            return
        try:
            sid = self._approval_session(session_id, session_key)
            with self._lock:
                snapshot = self._progress.waiting(sid, self._now())
            self._emit_progress(snapshot)
            ref = request_id or tool_call_id or f"approval:{int(self._now())}"
            self._notify("approval", sid, ref, body="", request_id=request_id)
        except Exception:
            logger.warning("butler-push: pre_approval_request failed")

    def post_approval_response(self, *, session_key: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                snapshot = self._progress.resumed(self._approval_session(session_id, session_key), self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("butler-push: post_approval_response failed")

    def subagent_start(self, *, child_session_id: Optional[str] = None, **_: Any) -> None:
        if child_session_id:
            with self._lock:
                self._subagent_sessions.remember(child_session_id, True)
