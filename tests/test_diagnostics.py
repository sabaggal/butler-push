from collections import Counter
import logging

import pytest

from butler_push import diagnostics
from butler_push.hooks import ButlerPush
from butler_push.privacy import unseal


def _turn(push, *, platform="desktop", post=True, interrupted=False):
    common = {"session_id": "private-session-identifier", "platform": platform,
              "task_id": "private-task-identifier", "turn_id": "private-turn-identifier"}
    push.pre_llm_call(**common, user_message="private-question-data")
    if post:
        push.post_llm_call(**common, assistant_response="private-answer-data")
    push.on_session_end(**common, completed=True, interrupted=interrupted)


def _outcomes(caplog):
    return [r.getMessage().split("=")[-1] for r in caplog.records if r.name == "butler_push"]


@pytest.fixture
def make_push(keys, sender):
    return lambda profile="default", relay="https://relay.example.test": ButlerPush(
        profile=profile, sender=sender, keys_loader=lambda: keys, relay_url=lambda: relay,
        bot_name=lambda: "private-profile-display", schedule=lambda *_: None,
        reply_previews=lambda: False,
    )


@pytest.mark.parametrize("profile", ["default", "named-profile"])
def test_hermes_gui_desktop_turn_reaches_the_sender(make_push, profile, sender, keys, monkeypatch, caplog):
    monkeypatch.setenv("BUTLER_PUSH_DIAGNOSTICS", "1")
    monkeypatch.setattr(diagnostics, "_last", {})
    caplog.set_level(logging.INFO, logger="butler_push")
    _turn(make_push(profile))
    assert [evt["kind"] for _, evt in sender.events] == ["progress", "progress", "reply"]
    reply = sender.events[-1][1]
    assert reply["source"] == "bot"
    preview = unseal(reply["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert preview["profile"] == ("" if profile == "default" else profile)
    assert preview["body"] == ""
    assert set(_outcomes(caplog)) == {"pre_allowed", "post_captured", "end_completed_captured",
                                     "banner_queued", "banner_relay_ack"}
    for forbidden in ["private-", "http", "@", keys.install_key]:
        assert forbidden not in caplog.text


def test_opt_in_and_no_repeat_within_interval(make_push, sender, monkeypatch, caplog):
    monkeypatch.delenv("BUTLER_PUSH_DIAGNOSTICS", raising=False)
    monkeypatch.setattr(diagnostics, "_last", {})
    caplog.set_level(logging.INFO, logger="butler_push")
    push = make_push()
    _turn(push)
    assert _outcomes(caplog) == []
    monkeypatch.setenv("BUTLER_PUSH_DIAGNOSTICS", "1")
    for _ in range(10):
        _turn(push, post=False)
    assert Counter(_outcomes(caplog)) == {"pre_allowed": 1, "end_completed_no_capture": 1}
    assert sum(evt["kind"] == "reply" for _, evt in sender.events) == 1


@pytest.mark.parametrize("platform", ["local", "cli"])
def test_direct_websocket_storage_and_terminal_labels_still_fail_closed(
        platform, make_push, sender, monkeypatch, caplog):
    monkeypatch.setenv("BUTLER_PUSH_DIAGNOSTICS", "1")
    monkeypatch.setattr(diagnostics, "_last", {})
    caplog.set_level(logging.INFO, logger="butler_push")
    _turn(make_push(), platform=platform)
    assert sender.events == []
    assert set(_outcomes(caplog)) == {"pre_filtered", "post_filtered", "end_completed_no_capture"}
