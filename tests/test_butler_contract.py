import base64
import importlib.util
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from butler_push.hooks import ButlerPush, relay_url_from_env
from butler_push.keys import key_dir, load_or_create_keys
from butler_push.privacy import unseal
from butler_push.relay import allowed_relay_url

ROOT = Path(__file__).resolve().parents[1]


def _test_app(monkeypatch, tmp_path):
    module_path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("butler_test_dashboard", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_or_create_keys", lambda: load_or_create_keys(tmp_path))
    relay = {"url": ""}
    monkeypatch.setattr(module, "relay_url_from_env", lambda: relay["url"])

    # Model the host boundary (the plugin does not expose an independent listener).
    app = FastAPI()
    @app.middleware("http")
    async def hermes_auth_boundary(request: Request, call_next):
        if request.headers.get("Authorization") != "Bearer fixture-authenticated-owner":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)
    app.include_router(module.router, prefix="/api/plugins/butler-push")
    return TestClient(app), relay


def test_configured_pairing_requires_auth_and_only_has_push_routes(tmp_path, monkeypatch):
    client, relay = _test_app(monkeypatch, tmp_path)
    base = "/api/plugins/butler-push"
    auth = {"Authorization": "Bearer fixture-authenticated-owner"}
    assert client.get(base + "/pairing").status_code == 401
    assert client.get(base + "/pairing", headers=auth).status_code == 409
    assert not key_dir(tmp_path).exists()
    caps = client.get(base + "/capabilities", headers=auth).json()
    assert caps["routes"] == ["GET /capabilities", "GET /pairing"]
    relay["url"] = "https://relay.example.test"
    pairing = client.get(base + "/pairing", headers=auth)
    assert pairing.status_code == 200 and pairing.headers["Cache-Control"] == "no-store"
    assert pairing.json()["platform"] == "butler" and pairing.json()["payload_version"] == 1
    assert pairing.json()["relay_url"] == relay["url"]
    assert len(base64.b64decode(pairing.json()["preview_key"])) == 32
    assert client.get(base + "/pairing", headers=auth).json() == pairing.json()
    assert client.get(base + "/provider-quota", headers=auth).status_code == 404


def test_root_configuration_refuses_untrusted_url_and_no_legacy_fallback(tmp_path, monkeypatch):
    import butler_push.hooks as hooks
    monkeypatch.setattr(hooks, "hermes_root", lambda: tmp_path)
    monkeypatch.delenv("BUTLER_PUSH_RELAY_URL", raising=False)
    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://legacy.example.test")
    assert relay_url_from_env() == ""
    monkeypatch.setenv("BUTLER_PUSH_RELAY_URL", "https://env.example.test/")
    assert relay_url_from_env() == ""
    monkeypatch.delenv("HERMEX_PUSH_RELAY_URL")
    assert relay_url_from_env() == "https://env.example.test"
    (tmp_path / ".env").write_text("BUTLER_PUSH_RELAY_URL=https://configured.example.test/push/\n")
    assert relay_url_from_env() == "https://configured.example.test/push"
    (tmp_path / ".env").write_text("BUTLER_PUSH_RELAY_URL=\n")
    assert relay_url_from_env() == ""
    for invalid in ["http://relay.example.test", "https://u:p@relay.example.test", "https://relay.test/?x=1",
                    "https://relay.test/#", "https://relay.test:bogus", "https://fake.test\\@relay.test"]:
        assert not allowed_relay_url(invalid)


def test_key_store_is_isolated_and_refuses_link(tmp_path):
    legacy = tmp_path / "plugin-data/hermex-push"
    legacy.mkdir(parents=True)
    (legacy / "install_key").write_text("a" * 64)
    key = load_or_create_keys(tmp_path)
    assert key.install_key != "a" * 64
    assert load_or_create_keys(tmp_path) == key
    assert (key_dir(tmp_path) / "install_key").stat().st_mode & 0o777 == 0o600
    path = key_dir(tmp_path) / "preview_key"
    path.unlink()
    path.symlink_to(legacy / "install_key")
    with pytest.raises(OSError):
        load_or_create_keys(tmp_path)
    assert (legacy / "install_key").read_text() == "a" * 64


def _perform_turn(push, platform="ios"):
    context = {"session_id": "fixture-session", "platform": platform}
    push.pre_llm_call(**context)
    push.pre_approval_request(**context, request_id="approval-42",
                              command="command-secret", description="description-secret")
    push.pre_tool_call(**context, tool_name="clarify", tool_call_id="question-42",
                       args={"question": "question-secret"})
    push.post_llm_call(**context, user_message="user-secret", assistant_response="**assistant-secret**")
    push.on_session_end(**context, completed=True, turn_id="turn-42")


@pytest.mark.parametrize("want_preview", [False, True])
def test_turn_wire_seals_only_optional_assistant_excerpt(sender, keys, want_preview):
    push = ButlerPush(profile="news", sender=sender, keys_loader=lambda: keys,
                      relay_url=lambda: "https://relay.example.test",
                      reply_previews=lambda: want_preview, schedule=lambda *_: None)
    _perform_turn(push)
    allowed = {"v", "kind", "event_id", "thread_id", "collapse_id", "session_id",
               "source", "is_subagent", "sent_at", "sealed"}
    payloads = {event["kind"]: event for url, event in sender.events if event["kind"] != "progress"}
    assert set(payloads) == {"approval", "clarify", "reply"}
    expected_ids = {"reply": "", "approval": "approval-42", "clarify": "question-42"}
    for kind, event in payloads.items():
        assert set(event) == allowed
        assert "-secret" not in json.dumps(event)
        decrypted = unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
        assert decrypted["profile"] == "news" and decrypted["request_id"] == expected_ids[kind]
        assert not decrypted["subtitle"]
        assert decrypted["body"] == ("assistant-secret" if kind == "reply" and want_preview else "")
    for target, event in sender.events:
        assert target == "https://relay.example.test/installs/" + keys.install_key + "/notify"
        if event["kind"] == "progress":
            assert event["tool"] is None


@pytest.mark.parametrize("platform", ["telegram", "", "unrecognized"])
def test_other_surfaces_fail_closed(sender, keys, platform):
    push = ButlerPush(sender=sender, keys_loader=lambda: keys,
                      relay_url=lambda: "https://relay.example.test", schedule=lambda *_: None)
    _perform_turn(push, platform)
    assert sender.events == []


def test_unconfigured_turn_neither_sends_nor_creates_keys(sender, tmp_path):
    push = ButlerPush(sender=sender, keys_loader=lambda: load_or_create_keys(tmp_path),
                      relay_url=lambda: "", schedule=lambda *_: None)
    _perform_turn(push)
    assert sender.events == []
    assert not key_dir(tmp_path).exists()


def test_linked_state_parent_is_rejected_before_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "plugin-data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        load_or_create_keys(tmp_path)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("where", ["root", "process"])
def test_legacy_sender_interlock_blocks_new_config_and_delivery(tmp_path, monkeypatch, where):
    import butler_push.hooks as hooks
    from butler_push.relay import RelaySender

    monkeypatch.setattr(hooks, "hermes_root", lambda: tmp_path)
    config = tmp_path / ".env"
    config.write_text("BUTLER_PUSH_RELAY_URL=https://relay.example.test\nHERMEX_PUSH_RELAY_URL=\n")
    assert hooks.relay_url_from_env() == "https://relay.example.test"
    if where == "root":
        with config.open("a") as f:
            f.write("HERMEX_PUSH_RELAY_URL=https://legacy.example.test\n")
    else:
        monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://legacy.example.test")
    assert hooks.legacy_sender_configured()
    assert hooks.relay_url_from_env() == ""
    invoked = []
    worker = RelaySender(post=lambda u, d: (invoked.append(u) or (200, "accepted")),
                         permit=lambda _: bool(hooks.relay_url_from_env()))
    assert not worker.deliver("https://relay.example.test/installs/fixture/notify", {"kind": "reply"})
    assert not invoked


def test_repeated_legacy_dotenv_lines_cannot_switch_on_two_relays(tmp_path, monkeypatch):
    import butler_push.hooks as hooks
    monkeypatch.setattr(hooks, "hermes_root", lambda: tmp_path)
    (tmp_path / ".env").write_text(
        "BUTLER_PUSH_RELAY_URL=https://relay.example.test\n"
        "export HERMEX_PUSH_RELAY_URL='https://legacy.example.test' # earlier record\n"
        "HERMEX_PUSH_RELAY_URL= # later blank record\n"
    )
    assert hooks.legacy_sender_configured()
    assert hooks.relay_url_from_env() == ""


def test_pairing_explains_legacy_block_without_minting_keys(tmp_path, monkeypatch):
    client, _ = _test_app(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://legacy.example.test")
    pairing = client.get("/api/plugins/butler-push/pairing",
                         headers={"Authorization": "Bearer fixture-authenticated-owner"})
    assert pairing.status_code == 409
    assert "Old Hermex Push relay" in pairing.json()["detail"]
    assert not key_dir(tmp_path).exists()
