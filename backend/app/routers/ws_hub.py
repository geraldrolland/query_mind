"""WebSocket hub: live-connection registry + Redis pub/sub delivery for chat.

Chat events are published to the Redis channel ``dataset:events`` by
the worker thread that runs a query.  A single subscriber task
(started at app startup) forwards each event to every live WebSocket
registered for the dataset.  The query therefore outlives any single
connection: a client that disconnects mid-query and reconnects simply
re-registers and keeps receiving the stream.
"""

import asyncio
import json
import logging

import redis.asyncio as aioredis
from fastapi import WebSocket

from app.core.settings import settings
from app.redis_conf import redis_client

logger = logging.getLogger(__name__)

CHANNEL = "dataset:events"

# In-memory registry: dataset_id -> list[WebSocket]
connections: dict[str, list[WebSocket]] = {}

_subscriber_task: asyncio.Task | None = None


def clear_resume_keys() -> None:
    """Remove stale ``resume:*`` keys (in-flight queries died with this process).

    Called at startup: after a backend restart the old worker threads
    are gone, so any leftover resume state would make reconnecting
    clients show a phantom "running" query that never completes.
    """
    try:
        removed = 0
        for key in redis_client.scan_iter("resume:*"):
            redis_client.delete(key)
            removed += 1
        if removed:
            logger.info("Cleared %d stale resume keys", removed)
    except Exception as exc:
        logger.warning("Failed to clear stale resume keys: %s", exc)


def register(dataset_id: str, websocket: WebSocket) -> None:
    """Register a WebSocket for a specific dataset's event stream.

    Args:
        dataset_id: Dataset to subscribe to.
        websocket: The client WebSocket connection.
    """
    sockets = connections.setdefault(dataset_id, [])
    if websocket not in sockets:
        sockets.append(websocket)


def unregister(dataset_id: str, websocket: WebSocket) -> None:
    """Remove a WebSocket from the dataset's connection pool.

    The pool is cleaned up entirely when no connections remain for
    the dataset to avoid memory leaks.

    Args:
        dataset_id: Dataset the socket was subscribed to.
        websocket: The client WebSocket connection.
    """
    sockets = connections.get(dataset_id)
    if not sockets:
        return
    try:
        sockets.remove(websocket)
    except ValueError:
        pass
    if not sockets:
        connections.pop(dataset_id, None)


async def _forward(dataset_id: str, event: str) -> None:
    """Forward a single event to all live sockets for a dataset.

    Failed sends cause the socket to be unregistered so future
    events are not attempted against dead connections.

    Args:
        dataset_id: Target dataset.
        event: Newline-terminated JSON event string.
    """
    sockets = connections.get(dataset_id)
    if not sockets:
        return
    for ws in list(sockets):
        try:
            await ws.send_text(event)
        except Exception as exc:
            logger.warning("WS forward failed for dataset %s: %s", dataset_id, exc)
            unregister(dataset_id, ws)


async def subscriber_loop() -> None:
    """Listen on the ``dataset:events`` channel and forward to live sockets.

    Reconnects indefinitely on Redis errors so a transient Redis
    outage does not kill the task.
    """
    while True:
        pubsub = None
        try:
            redis_url = settings.PROD_REDIS_URL if settings.ENVIRONMENT == "production" else settings.REDIS_URL
            client = aioredis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=None,
                health_check_interval=30,
            )
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)
            logger.info("WS hub subscriber connected to channel %s", CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("WS hub ignoring unparsable message: %r", raw)
                    continue
                logger.info(
                    "WS hub event: dataset=%s event=%s",
                    payload.get("dataset_id", ""),
                    payload.get("event", ""),
                )
                await _forward(str(payload.get("dataset_id", "")), str(payload.get("event", "")))
        except (aioredis.RedisError, ConnectionError, OSError) as exc:
            logger.warning("WS hub subscriber error: %s; retrying in 3s", exc)
            await asyncio.sleep(3)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass


def start_subscriber() -> None:
    """Launch the background Redis pub/sub subscriber as an asyncio task.

    Safe to call multiple times; no-op if the task is already running.
    """
    global _subscriber_task
    if _subscriber_task is None or _subscriber_task.done():
        _subscriber_task = asyncio.create_task(subscriber_loop())


async def stop_subscriber() -> None:
    """Cancel the subscriber task and wait for it to finish.

    Silently handles ``CancelledError`` since the task is expected
    to be interrupted.
    """
    global _subscriber_task
    task = _subscriber_task
    _subscriber_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
