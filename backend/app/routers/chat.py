"""Chat router: AI assistant backed by DeepSeek with a DSL query tool.

The chat surface is a persistent WebSocket endpoint
``/api/v1/chat/{dataset_id}/query``.  The client connects once (on
page mount) and sends ONE text frame per query with
``{message, history}``.  Each text frame from the server is one
newline-delimited JSON event::

    {"id", "role", "type", "chart_type", "content", "created_at"}
        assistant message (text or record content)
    {"progress": "..."}
        status update emitted while the tool loop runs
    {"done": true}
    {"error": "..."}

The connection stays open across queries until the client disconnects.

Delivery is decoupled from the connection: the worker thread that
runs a query publishes every event to Redis (channel
``dataset:events``), and the ``ws_hub`` subscriber forwards it to
all live sockets registered for the dataset.  A query therefore
outlives any single connection.  On connect the server immediately
pushes the current query state (last progress event or done) stored
under ``resume:<dataset_id>`` if any.
"""

import hashlib
import json
import logging
import threading

from fastapi import APIRouter, Depends, WebSocket
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from app.auth.utils import decode_token, unsign_cookie
from app.core.settings import settings
from app.db import engine, get_session
from app.redis_conf import redis_client
from app.routers.llm_event_stream import event_stream
from app.routers.llm_utils import _build_system_prompt
from app.routers.ws_hub import register, unregister
from app.schemas.dataset import ChatRequestSchema
from ..models.dataset import Dataset

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# WebSocket close codes
WS_AUTH_CLOSE = 1008      # policy violation
WS_NOT_CONFIGURED = 1013  # service restart / not configured
WS_BAD_MESSAGE = 1003

CHAT_CHANNEL = "dataset:events"
RESUME_KEY = "resume:{dataset_id}"
RESUME_TTL_SECONDS = 600


def _agent_matches(a: str, b: str) -> bool:
    """Compare two user-agent strings via SHA-256 to avoid cookie-mismatch
    on minor UA changes (e.g. minor version bumps).

    Args:
        a: Stored user-agent string.
        b: Incoming user-agent string.

    Returns:
        ``True`` if the hashes match (or the raw strings match).
    """
    return hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(
        b.encode()
    ).hexdigest() or a == b


def _authenticated_user_id(websocket: WebSocket) -> int | None:
    """Authenticate a WebSocket handshake from the ``auth_token`` cookie.

    The HTTP ``AuthenticationMiddleware`` does not run for WebSocket
    upgrades, so this performs its own validation.

    Args:
        websocket: The incoming WebSocket connection.

    Returns:
        The authenticated user ID, or ``None`` if authentication
        fails.
    """
    origin = websocket.headers.get("origin", "")
    if origin and origin not in settings.CORS_ORIGINS:
        logger.warning("WS handshake rejected: origin %r not allowed", origin)
        return None

    raw = websocket.cookies.get("auth_token")
    if not raw:
        return None
    payload = unsign_cookie(raw, max_age=None)
    if not payload:
        return None
    access = decode_token(payload.get("access_token"), "access_token")
    if access is None:
        return None
    user_agent = websocket.headers.get("user-agent", "")
    if access.get("user_agent") and not _agent_matches(
        access.get("user_agent"), user_agent
    ):
        return None
    return int(access["sub"])


def _is_progress_or_done(line: str) -> bool:
    """Check whether an NDJSON line is a progress or terminal event.

    Args:
        line: Newline-terminated JSON string.

    Returns:
        ``True`` if the line contains a ``progress`` key or
        ``done: true``.
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return False
    return "progress" in event or event.get("done") is True


def _produce(
    *,
    dataset_id: str,
    body: ChatRequestSchema,
    user_id: int,
    system_prompt: str,
) -> None:
    """Run the blocking event stream in a worker thread.

    Each NDJSON line is published to the Redis channel
    ``dataset:events`` (the ``ws_hub`` subscriber delivers it to live
    sockets).  Progress/done events are also persisted under
    ``resume:<dataset_id>`` so a reconnecting client can learn the
    current query state.  The query runs to completion even if no
    socket is connected, so a reconnect can resume the stream.

    The thread owns its own SQLAlchemy session: the connection's
    request session is closed by FastAPI when the WebSocket handler
    exits (client disconnect/refresh), so using it here would kill
    the query mid-stream.

    Args:
        dataset_id: Dataset being queried.
        body: Parsed chat request payload.
        user_id: Authenticated user ID.
        system_prompt: Rendered system prompt for the dataset.
    """
    with Session(engine) as session:
        saw_terminal = False

        def _publish(line: str) -> None:
            payload = json.dumps({"dataset_id": dataset_id, "event": line})
            try:
                redis_client.publish(CHAT_CHANNEL, payload)
            except Exception as exc:
                logger.warning("Redis publish failed: %s", exc)

        try:
            for line in event_stream(
                session=session,
                dataset_id=dataset_id,
                body=body,
                user_id=user_id,
                system_prompt=system_prompt,
            ):
                _publish(line)
                try:
                    evt = json.loads(line)
                    if evt.get("done") is True or "error" in evt:
                        saw_terminal = True
                except (json.JSONDecodeError, TypeError):
                    pass
                if _is_progress_or_done(line):
                    try:
                        redis_client.set(
                            RESUME_KEY.format(dataset_id=dataset_id),
                            line,
                            ex=RESUME_TTL_SECONDS,
                        )
                    except Exception as exc:
                        logger.warning("Redis resume set failed: %s", exc)
            if not saw_terminal:
                logger.warning(
                    "event_stream ended without a terminal event; publishing done"
                )
                done_line = '{"done": true}\n'
                _publish(done_line)
                try:
                    redis_client.set(
                        RESUME_KEY.format(dataset_id=dataset_id),
                        done_line,
                        ex=RESUME_TTL_SECONDS,
                    )
                except Exception as exc:
                    logger.warning("Redis resume set failed: %s", exc)
        except Exception as exc:
            logger.error("WS event_stream failed: %s", exc, exc_info=True)
            payload = json.dumps(
                {
                    "dataset_id": dataset_id,
                    "event": '{"error": "The AI assistant hit an unexpected error."}\n',
                }
            )
            try:
                redis_client.publish(CHAT_CHANNEL, payload)
            except Exception:
                pass
            try:
                redis_client.set(
                    RESUME_KEY.format(dataset_id=dataset_id),
                    '{"done": true}\n',
                    ex=RESUME_TTL_SECONDS,
                )
            except Exception:
                pass


@router.websocket("/{dataset_id}/query")
async def chat_query(
    websocket: WebSocket, dataset_id: str, session: Session = Depends(get_session)
):
    """Stream AI answers for the dataset over a persistent WebSocket.

    The connection is authenticated once and reused for many queries:
    the client sends one ``{message, history}`` text frame per query
    and receives newline-delimited JSON events ending with
    ``{"done": true}``.  The socket stays open until the client
    disconnects; delivery happens via Redis pub/sub, so events are
    forwarded to every live socket for the dataset.
    """
    user_id = _authenticated_user_id(websocket)
    if user_id is None:
        await websocket.close(code=WS_AUTH_CLOSE, reason="Not authenticated")
        return

    await websocket.accept()

    if not settings.LLM_API_KEY:
        await websocket.close(code=WS_NOT_CONFIGURED, reason="AI assistant is not configured")
        return

    dataset = session.exec(
        select(Dataset).where(Dataset.id == dataset_id)
    ).first()
    if not dataset or dataset.user_id != user_id:
        await websocket.close(code=WS_AUTH_CLOSE, reason="Dataset not found")
        return

    system_prompt = _build_system_prompt(dataset)
    register(dataset_id, websocket)
    logger.info("WS chat connected: user=%s dataset=%s", user_id, dataset_id)

    try:
        # Push the last known query state so a reconnecting client
        # immediately sees whether a query is running or finished.
        try:
            state = redis_client.get(RESUME_KEY.format(dataset_id=dataset_id))
        except Exception as exc:
            logger.warning("Redis resume get failed: %s", exc)
            state = None
        if state:
            try:
                await websocket.send_text(state)
            except Exception as exc:
                logger.warning("WS resume push failed: %s", exc)

        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect as exc:
                logger.info(
                    "WS chat disconnected: user=%s dataset=%s code=%s reason=%r",
                    user_id,
                    dataset_id,
                    exc.code,
                    exc.reason,
                )
                break
            except Exception as exc:
                logger.warning("WS receive failed: %s", exc)
                break

            # Respond to client keepalive pings.
            try:
                if json.loads(raw).get("ping") == 1:
                    try:
                        await websocket.send_text('{"pong": 1}\n')
                    except Exception:
                        break
                    continue
            except Exception:
                pass

            try:
                body = ChatRequestSchema.model_validate_json(raw)
            except Exception as exc:
                logger.warning("WS chat payload invalid: %s", exc)
                try:
                    await websocket.send_text('{"error": "Invalid payload"}\n')
                except Exception:
                    break
                continue

            logger.info("message received: %s", body.message.content)
            thread = threading.Thread(
                target=_produce,
                kwargs={
                    "dataset_id": dataset_id,
                    "body": body,
                    "user_id": user_id,
                    "system_prompt": system_prompt,
                },
                daemon=True,
            )
            logger.info(
                "WS chat query: user=%s dataset=%s message=%r history=%d",
                user_id,
                dataset_id,
                body.message.content[:200],
                len(body.history),
            )
            thread.start()
    finally:
        unregister(dataset_id, websocket)
