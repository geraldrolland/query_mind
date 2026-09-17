"""Tests for WebSocket authentication in the chat router.

Verifies that:
- Unauthenticated connections are closed with code 1008.
- Auth is checked *before* ``websocket.accept()`` for rejected
  connections (close without accept).
- Authenticated connections with a valid dataset proceed normally.
"""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.utils import sign_cookie, generate_access_token
from app.db import get_session
from app.routers.chat import router as chat_router, WS_AUTH_CLOSE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 TestAgent/1.0"
USER_ID = 42
DATASET_ID = "dst_test"


def _make_auth_cookie(user_id: int = USER_ID, user_agent: str = USER_AGENT) -> str:
    """Build a valid signed ``auth_token`` cookie value."""
    access = generate_access_token(
        {"sub": str(user_id), "email": "u@test.com", "user_agent": user_agent}
    )
    return sign_cookie({"access_token": access, "refresh_token": "irrelevant"})


def _app() -> FastAPI:
    """Create a minimal FastAPI app with the chat router mounted."""
    app = FastAPI()
    app.include_router(chat_router)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWSAuthRejected:
    """Connections that fail auth are closed with code 1008 without accept."""

    def test_no_cookie_closes_with_1008(self):
        """Without an auth_token cookie the socket is rejected immediately."""
        app = _app()
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/api/v1/chat/{DATASET_ID}/query"):
                pass
        assert exc_info.value.code == WS_AUTH_CLOSE

    def test_invalid_cookie_closes_with_1008(self):
        """A tampered/unsigned cookie triggers auth rejection."""
        app = _app()
        client = TestClient(app)
        client.cookies.set("auth_token", "not-a-valid-cookie")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/chat/{DATASET_ID}/query",
            ):
                pass
        assert exc_info.value.code == WS_AUTH_CLOSE

    def test_expired_token_closes_with_1008(self):
        """An expired JWT inside a valid cookie envelope is rejected."""
        import jwt
        from datetime import datetime, timedelta, timezone
        from app.core.settings import settings

        expired = jwt.encode(
            {
                "sub": str(USER_ID),
                "email": "u@test.com",
                "user_agent": USER_AGENT,
                "type": "access_token",
                "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
            },
            settings.SECRET_KEY,
            algorithm="HS256",
        )
        cookie_val = sign_cookie({"access_token": expired, "refresh_token": "x"})

        app = _app()
        client = TestClient(app)
        client.cookies.set("auth_token", cookie_val)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/chat/{DATASET_ID}/query",
            ):
                pass
        assert exc_info.value.code == WS_AUTH_CLOSE


class TestWSAuthAccepted:
    """Authenticated connections with a valid dataset proceed past auth."""

    @patch("app.routers.chat._build_system_prompt", return_value="system prompt")
    @patch("app.routers.chat._authenticated_user_id", return_value=USER_ID)
    @patch("app.routers.chat.settings")
    @patch("app.routers.chat.register")
    @patch("app.routers.chat.unregister")
    @patch("app.routers.chat.redis_client")
    @patch("app.routers.chat.select")
    def test_valid_auth_proceeds_to_register(
        self, mock_select, mock_redis, mock_unregister, mock_register, mock_settings,
        _mock_auth, _mock_build_prompt,
    ):
        """A valid cookie + matching dataset lets the connection register."""
        mock_settings.LLM_API_KEY = "sk-test-key"
        mock_settings.CORS_ORIGINS = ["http://localhost:3001"]

        # Mock dataset lookup
        fake_dataset = SimpleNamespace(id=DATASET_ID, user_id=USER_ID)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = fake_dataset
        mock_select.return_value.where.return_value = mock_exec_result

        # Mock redis get (no resume state)
        mock_redis.get.return_value = None

        app = _app()

        # Override get_session dependency
        fake_session = MagicMock()
        fake_session.exec.return_value = mock_exec_result
        app.dependency_overrides[get_session] = lambda: fake_session

        client = TestClient(app)

        with client.websocket_connect(
            f"/api/v1/chat/{DATASET_ID}/query",
        ) as ws:
            # Connection should be registered (server-side WebSocket object)
            mock_register.assert_called_once()
            assert mock_register.call_args[0][0] == DATASET_ID

            # Send a ping to verify the socket is alive
            ws.send_text('{"ping": 1}')
            resp = ws.receive_text()
            assert "pong" in resp

    @patch("app.routers.chat.settings")
    @patch("app.routers.chat.select")
    def test_wrong_user_dataset_closes_with_1008(self, mock_select, mock_settings):
        """Authenticated user who doesn't own the dataset gets rejected."""
        mock_settings.LLM_API_KEY = "sk-test-key"
        mock_settings.CORS_ORIGINS = ["http://localhost:3001"]

        fake_dataset = SimpleNamespace(id=DATASET_ID, user_id=999)  # different user
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = fake_dataset
        mock_select.return_value.where.return_value = mock_exec_result

        app = _app()

        # Override get_session dependency
        fake_session = MagicMock()
        fake_session.exec.return_value = mock_exec_result
        app.dependency_overrides[get_session] = lambda: fake_session

        client = TestClient(app, cookies={"auth_token": _make_auth_cookie()})

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/chat/{DATASET_ID}/query",
            ):
                pass
        assert exc_info.value.code == WS_AUTH_CLOSE


class TestAuthBeforeAccept:
    """Verify auth logic runs before the WebSocket handshake completes."""

    def test_auth_checked_before_accept(self):
        """When auth fails, _authenticated_user_id is called before
        the connection is used for anything else."""
        app = _app()

        with patch("app.routers.chat._authenticated_user_id") as mock_auth:
            mock_auth.return_value = None  # force auth failure

            client = TestClient(app)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/api/v1/chat/{DATASET_ID}/query"):
                    pass
            assert exc_info.value.code == WS_AUTH_CLOSE

            # _authenticated_user_id was called (auth runs)
            mock_auth.assert_called_once()
