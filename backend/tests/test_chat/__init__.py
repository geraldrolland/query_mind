"""Tests for app/routers/chat.py (WebSocket chat surface).

Covers:
    - _agent_matches            (UA comparison)
    - _is_progress_or_done      (NDJSON frame classification)
    - _authenticated_user_id    (cookie/JWT/origin/user-agent handshake rules)
    - _produce                  (worker thread: redis publishing, resume-key
                                 persistence, done synthesis, error handling)
    - chat_query                (full WebSocket lifecycle over TestClient:
                                 auth/config/ownership close codes, resume
                                 push, ping/pong, invalid payload recovery,
                                 producer spawn, hub registration cleanup)

No live LLM, database, or Redis is touched.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_chat/__init__.py -v
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.routers.chat as chat_mod
import app.routers.ws_hub as hub
from app.auth.utils import make_tokens, sign_cookie
from app.db import get_session
from app.routers.chat import (
    RESUME_KEY,
    RESUME_TTL_SECONDS,
    WS_AUTH_CLOSE,
    WS_BAD_MESSAGE,
    WS_NOT_CONFIGURED,
    _agent_matches,
    _authenticated_user_id,
    _is_progress_or_done,
    _produce,
)
from app.schemas.dataset import ChatRequestSchema


# ===========================================================================
# Fakes / helpers
# ===========================================================================


class FakeDBSession:
    def __init__(self):
        self.query_results = []

    def exec(self, query):
        class _R:
            def __init__(self, items):
                self._items = items

            def first(self):
                return self._items[0] if self._items else None

        if self.query_results:
            return _R(self.query_results.pop(0))
        return _R([])


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.published = []
        self.set_calls = []
        self.fail_publish = False

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def publish(self, channel, payload):
        if self.fail_publish:
            raise ConnectionError("redis down")
        self.published.append((channel, payload))


def make_dataset(user_id=9, ds_id="dst_1"):
    from app.models.dataset import Dataset

    return Dataset(
        user_id=user_id,
        name="Sales",
        id=ds_id,
        populated=True,
        total_rows=5,
        dataset_schema={"name": {"type": "string"}},
    )


def signed_auth_cookie(user_id=9, email="u@x.com", ua="pytest-agent"):
    tokens = make_tokens(user_id, email, ua)
    return sign_cookie(
        {"access_token": tokens["access"], "refresh_token": tokens["refresh"]}
    )


def make_websocket(cookies=None, headers=None):
    """Minimal stand-in exposing the attributes the helper reads."""
    hdrs = dict(headers or {})
    return SimpleNamespace(headers=hdrs, cookies=dict(cookies or {}))


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(chat_mod, "redis_client", redis)
    return redis


@pytest.fixture
def cors(monkeypatch):
    allowed = ["http://localhost:3001"]
    monkeypatch.setattr(chat_mod.settings, "CORS_ORIGINS", allowed)
    return allowed


# ===========================================================================
# _agent_matches
# ===========================================================================


def test_agent_matches_equal():
    assert _agent_matches("Mozilla/5.0", "Mozilla/5.0") is True


def test_agent_matches_different():
    assert _agent_matches("Mozilla/5.0", "curl/8") is False


# ===========================================================================
# Frame classification
# ===========================================================================


def test_is_progress_frame_true():
    assert _is_progress_or_done('{"progress": "thinking"}') is True


def test_is_done_true_frame_true():
    assert _is_progress_or_done('{"done": true}') is True


def test_is_done_false_frame_not_terminal():
    assert _is_progress_or_done('{"done": false}') is False


def test_assistant_message_frame_false():
    line = json.dumps({"id": "m1", "role": "assistant", "content": "hi"})
    assert _is_progress_or_done(line) is False


def test_unparsable_line_false():
    assert _is_progress_or_done("not-json{") is False


def test_error_frame_not_classified_as_progress_or_done():
    assert _is_progress_or_done('{"error": "boom"}') is False


def test_close_code_constants_stable():
    assert WS_AUTH_CLOSE == 1008
    assert WS_NOT_CONFIGURED == 1013
    assert WS_BAD_MESSAGE == 1003
    assert RESUME_TTL_SECONDS == 600


# ===========================================================================
# _authenticated_user_id
# ===========================================================================


def test_handshake_success_returns_user_id(cors):
    ws = make_websocket(
        cookies={"auth_token": signed_auth_cookie(user_id=9)},
        headers={"user-agent": "pytest-agent"},
    )
    assert _authenticated_user_id(ws) == 9


def test_handshake_disallowed_origin_rejected(cors, fake_redis):
    ws = make_websocket(
        cookies={"auth_token": signed_auth_cookie()},
        headers={"user-agent": "pytest-agent", "origin": "http://evil.example"},
    )
    assert _authenticated_user_id(ws) is None


def test_handshake_allowed_origin_accepted(cors, fake_redis):
    ws = make_websocket(
        cookies={"auth_token": signed_auth_cookie()},
        headers={
            "user-agent": "pytest-agent",
            "origin": cors[0],
        },
    )
    assert _authenticated_user_id(ws) == 9


def test_handshake_missing_cookie_rejected(cors):
    ws = make_websocket(headers={"user-agent": "pytest-agent"})
    assert _authenticated_user_id(ws) is None


def test_handshake_tampered_cookie_rejected(cors):
    ws = make_websocket(
        cookies={"auth_token": "tampered-value"},
        headers={"user-agent": "pytest-agent"},
    )
    assert _authenticated_user_id(ws) is None


def test_handshake_garbage_inner_jwt_rejected(cors):
    broken = sign_cookie({"access_token": "not-a-jwt"})
    ws = make_websocket(
        cookies={"auth_token": broken},
        headers={"user-agent": "pytest-agent"},
    )
    assert _authenticated_user_id(ws) is None


def test_handshake_agent_mismatch_rejected(cors):
    ws = make_websocket(
        cookies={"auth_token": signed_auth_cookie(ua="Chrome-on-Linux")},
        headers={"user-agent": "Firefox"},
    )
    assert _authenticated_user_id(ws) is None


def test_handshake_empty_token_agent_skips_ua_check(cors):
    # Tokens minted without a bound user agent skip the binding entirely.
    ws = make_websocket(
        cookies={"auth_token": signed_auth_cookie(ua="")},
        headers={"user-agent": "Whatever/1.0"},
    )
    assert _authenticated_user_id(ws) == 9


# ===========================================================================
# _produce (worker thread)
# ===========================================================================


class FakeStreamSession:
    def __init__(self, engine=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def produce_env(monkeypatch, fake_redis):
    monkeypatch.setattr(chat_mod, "engine", object())
    monkeypatch.setattr(chat_mod, "Session", FakeStreamSession)
    return fake_redis


def _run_produce(dataset_id="dst_1", user_id=9, system_prompt="SYS"):
    body = ChatRequestSchema(
        message={
            "id": "m1",
            "role": "user",
            "status": "pending",
            "content": "hi",
        },
        history=[],
    )
    _produce(
        dataset_id=dataset_id,
        body=body,
        user_id=user_id,
        system_prompt=system_prompt,
    )


def test_produce_publishes_lines_and_persists_progress_resume(
    produce_env, monkeypatch
):
    seen = {}

    def fake_stream(**kwargs):
        seen.update(kwargs)
        yield '{"progress": "planning SQL"}\n'
        yield '{"done": true}\n'

    monkeypatch.setattr(chat_mod, "event_stream", fake_stream)
    _run_produce()

    channels = [c for c, _ in produce_env.published]
    assert channels == ["dataset:events", "dataset:events"]
    payloads = [json.loads(p)["event"] for _, p in produce_env.published]
    assert payloads[0] == '{"progress": "planning SQL"}\n'
    assert payloads[1] == '{"done": true}\n'

    # Progress AND terminal frames are persisted under resume:<id> with TTL;
    # the key always reflects the latest state for reconnecting clients.
    resume_key = RESUME_KEY.format(dataset_id="dst_1")
    persisted = [value for _, value, _ in produce_env.set_calls]
    assert persisted == ['{"progress": "planning SQL"}\n', '{"done": true}\n']
    assert produce_env.store[resume_key] == '{"done": true}\n'
    assert all(ttl == RESUME_TTL_SECONDS for _, _, ttl in produce_env.set_calls)

    # Stream received the exact collaboration context.
    assert seen["dataset_id"] == "dst_1"
    assert seen["user_id"] == 9
    assert seen["system_prompt"] == "SYS"
    assert seen["body"].message.content == "hi"


def test_produce_publishes_wrapped_envelope_with_dataset_id(produce_env, monkeypatch):
    monkeypatch.setattr(
        chat_mod, "event_stream", lambda **kw: iter(['{"done": true}\n'])
    )
    _run_produce()

    channel, payload = produce_env.published[0]
    decoded = json.loads(payload)
    assert decoded["dataset_id"] == "dst_1"
    assert "event" in decoded


def test_produce_synthesizes_done_when_stream_ends_without_terminal(
    produce_env, monkeypatch, caplog
):
    def silent_stream(**kwargs):
        yield '{"progress": "halfway"}\n'
        # ...then just stops: no done, no error.

    monkeypatch.setattr(chat_mod, "event_stream", silent_stream)
    _run_produce()

    last_payload = json.loads(produce_env.published[-1][1])["event"]
    assert last_payload == '{"done": true}\n'
    resume_key = RESUME_KEY.format(dataset_id="dst_1")
    assert produce_env.store[resume_key] == '{"done": true}\n'


def test_produce_stream_exception_publishes_error_and_done_resume(
    produce_env, monkeypatch
):
    def exploding_stream(**kwargs):
        yield '{"progress": "starting"}\n'
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr(chat_mod, "event_stream", exploding_stream)
    _run_produce()

    events = [json.loads(p)["event"] for _, p in produce_env.published]
    assert any('"error"' in e for e in events)

    resume_key = RESUME_KEY.format(dataset_id="dst_1")
    assert produce_env.store[resume_key] == '{"done": true}\n'


def test_produce_swallows_redis_publish_failures(produce_env, monkeypatch):
    produce_env.fail_publish = True

    def stream(**kwargs):
        yield '{"done": true}\n'

    monkeypatch.setattr(chat_mod, "event_stream", stream)
    _run_produce()  # must not raise despite redis being down
    assert produce_env.set_calls  # resume write still attempted


# ===========================================================================
# chat_query — full WebSocket lifecycle
# ===========================================================================


@pytest.fixture
def ws_env(monkeypatch, fake_redis):
    db = FakeDBSession()
    # NOTE: dependency overrides must be keyed on the original callable that
    # the route captured at import time (app.db.get_session), not on any
    # module attribute.
    app = FastAPI()
    app.include_router(chat_mod.router)

    def override_session():
        yield db

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)
    return SimpleNamespace(
        client=client,
        db=db,
        redis=fake_redis,
        settings=chat_mod.settings,
    )


def _ws_headers(cookie_value, ua="pytest-agent", origin=None):
    headers = {"cookie": f"auth_token={cookie_value}", "user-agent": ua}
    if origin:
        headers["origin"] = origin
    return headers


def _connect(env, url="/api/v1/chat/dst_1/query", headers=None):
    return env.client.websocket_connect(url, headers=headers or {})


def test_ws_rejects_unauthenticated_client(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(ws_env) as ws:
            ws.receive_text()

    assert excinfo.value.code == WS_AUTH_CLOSE


def test_ws_rejects_when_llm_not_configured(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "")
    cookie = signed_auth_cookie()

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(ws_env, headers=_ws_headers(cookie)) as ws:
            ws.receive_text()

    assert excinfo.value.code == WS_NOT_CONFIGURED


def test_ws_rejects_missing_dataset(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")
    ws_env.db.query_results.append([])  # dataset lookup misses

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(
            ws_env, headers=_ws_headers(signed_auth_cookie())
        ) as ws:
            ws.receive_text()

    assert excinfo.value.code == WS_AUTH_CLOSE


def test_ws_rejects_foreign_dataset(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")
    ws_env.db.query_results.append([make_dataset(user_id=77)])  # owned by someone else

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _connect(
            ws_env, headers=_ws_headers(signed_auth_cookie(user_id=9))
        ) as ws:
            ws.receive_text()

    assert excinfo.value.code == WS_AUTH_CLOSE


def test_ws_pushes_resume_state_on_connect(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")
    ws_env.db.query_results.append([make_dataset(user_id=9)])
    ws_env.redis.store[RESUME_KEY.format(dataset_id="dst_1")] = '{"progress": "running"}'

    with _connect(ws_env, headers=_ws_headers(signed_auth_cookie())) as ws:
        assert ws.receive_text() == '{"progress": "running"}'


def test_ws_ping_pong_roundtrip_and_hub_registration(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")
    ws_env.db.query_results.append([make_dataset(user_id=9)])

    with _connect(ws_env, headers=_ws_headers(signed_auth_cookie())) as ws:
        ws.send_text('{"ping": 1}')
        assert ws.receive_text() == '{"pong": 1}\n'

        # Round-trip proves the server loop is live -> hub registration
        # (which happens before the loop) must have completed.
        assert hub.connections.get("dst_1")

    time.sleep(0.05)  # allow finally-block unregister to settle
    assert not hub.connections.get("dst_1")


def test_ws_invalid_payload_recovers(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")
    ws_env.db.query_results.append([make_dataset(user_id=9)])

    with _connect(ws_env, headers=_ws_headers(signed_auth_cookie())) as ws:
        ws.send_text("not-json-at-all")
        assert ws.receive_text() == '{"error": "Invalid payload"}\n'

        # Socket stays usable after the bad frame.
        ws.send_text('{"ping": 1}')
        assert ws.receive_text() == '{"pong": 1}\n'


def test_ws_valid_query_spawns_producer_thread(ws_env, monkeypatch, cors):
    monkeypatch.setattr(ws_env.settings, "LLM_API_KEY", "key")
    ws_env.db.query_results.append([make_dataset(user_id=9)])

    produced = threading.Event()
    captured = {}

    def fake_produce(*, dataset_id, body, user_id, system_prompt):
        captured.update(
            {
                "dataset_id": dataset_id,
                "body": body,
                "user_id": user_id,
                "system_prompt": system_prompt,
            }
        )
        produced.set()

    monkeypatch.setattr(chat_mod, "_produce", fake_produce)

    frame = json.dumps(
        {
            "message": {
                "id": "client-m1",
                "role": "user",
                "status": "pending",
                "content": "average revenue?",
            },
            "history": [{"role": "user", "content": "earlier q"}],
        }
    )

    with _connect(ws_env, headers=_ws_headers(signed_auth_cookie())) as ws:
        ws.send_text(frame)
        assert produced.wait(timeout=3), "producer thread never ran"

    assert captured["dataset_id"] == "dst_1"
    assert captured["user_id"] == 9
    assert captured["system_prompt"]  # built from the dataset schema
    assert captured["body"].message.content == "average revenue?"
    assert captured["body"].history == [
        {"role": "user", "content": "earlier q"}
    ]


