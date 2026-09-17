"""Tests for app/routers/ws_hub.py (live-connection registry + delivery).

Covers:
    - register / unregister     (in-memory fan-out registry semantics)
    - _forward                  (delivery, dead-socket pruning)
    - clear_resume_keys         (startup sweep incl. redis failure swallow)
    - start / stop_subscriber   (task lifecycle idempotency)
    - subscriber_loop           (pubsub consumption: routing per dataset,
                                 malformed-message skipping, reconnect-after-
                                 error backoff, clean cancellation)

No real Redis or WebSocket connections are used; the async redis client is
faked at ``hub.aioredis.from_url``.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_ws_hub/__init__.py -v
"""

import asyncio
import json

import pytest
import redis.asyncio as aioredis

import app.routers.ws_hub as hub

# ===========================================================================
# Fakes
# ===========================================================================


class FakeWS:
    def __init__(self, name="ws", fail=False):
        self.name = name
        self.fail = fail
        self.sent = []

    async def send_text(self, text):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(text)


class FakeSyncRedis:
    """Stand-in for the sync redis_client used by clear_resume_keys."""

    def __init__(self, keys=None, fail=False):
        self._keys = keys or []
        self._fail = fail
        self.deleted = []

    def scan_iter(self, pattern):
        if self._fail:
            raise ConnectionError("redis down")
        for key in self._keys:
            yield key

    def delete(self, *keys):
        self.deleted.extend(keys)


class FakePubSub:
    def __init__(self, messages, fail_subscribe=False):
        self._messages = messages
        self._fail_subscribe = fail_subscribe
        self.subscribed = []
        self.closed = False
        self._hang = asyncio.Event()

    async def subscribe(self, channel):
        if self._fail_subscribe:
            raise aioredis.RedisError("subscribe refused")
        self.subscribed.append(channel)

    async def listen(self):
        for message in self._messages:
            yield message
        # Simulate an idle subscription: park until cancelled.
        await self._hang.wait()

    async def aclose(self):
        self.closed = True


class FakeAsyncRedis:
    def __init__(self, pubsubs):
        self._pubsubs = list(pubsubs)
        self.created = 0

    def pubsub(self):
        self.created += 1
        return self._pubsubs.pop(0)


# ===========================================================================
# register / unregister
# ===========================================================================


def test_register_adds_socket_under_dataset():
    hub.connections.clear()
    ws = FakeWS()
    hub.register("dst_1", ws)
    assert hub.connections["dst_1"] == [ws]
    hub.connections.clear()


def test_register_is_idempotent_per_socket():
    hub.connections.clear()
    ws = FakeWS()
    hub.register("dst_1", ws)
    hub.register("dst_1", ws)
    assert hub.connections["dst_1"] == [ws]
    hub.connections.clear()


def test_register_keeps_datasets_independent():
    hub.connections.clear()
    ws_a, ws_b = FakeWS(), FakeWS()
    hub.register("dst_a", ws_a)
    hub.register("dst_b", ws_b)
    assert hub.connections == {"dst_a": [ws_a], "dst_b": [ws_b]}
    hub.connections.clear()


def test_unregister_removes_socket_and_pops_empty_key():
    hub.connections.clear()
    ws = FakeWS()
    hub.register("dst_1", ws)
    hub.unregister("dst_1", ws)
    assert "dst_1" not in hub.connections
    hub.connections.clear()


def test_unregister_unknown_dataset_is_noop():
    hub.unregister("never-registered", FakeWS())  # must not raise


def test_unregister_foreign_socket_is_noop():
    hub.connections.clear()
    ws_a, ws_b = FakeWS(), FakeWS()
    hub.register("dst_1", ws_a)
    hub.unregister("dst_1", ws_b)  # ValueError swallowed
    assert hub.connections["dst_1"] == [ws_a]
    hub.connections.clear()


# ===========================================================================
# _forward
# ===========================================================================


def test_forward_delivers_to_all_registered_sockets():
    hub.connections.clear()
    ws_a, ws_b = FakeWS(), FakeWS()
    hub.register("dst_1", ws_a)
    hub.register("dst_1", ws_b)

    asyncio.run(hub._forward("dst_1", '{"done": true}'))

    assert ws_a.sent == ['{"done": true}']
    assert ws_b.sent == ['{"done": true}']
    hub.connections.clear()


def test_forward_prunes_dead_sockets_only():
    hub.connections.clear()
    healthy, dead = FakeWS(name="healthy"), FakeWS(name="dead", fail=True)
    hub.register("dst_1", healthy)
    hub.register("dst_1", dead)

    asyncio.run(hub._forward("dst_1", '{"progress": "x"}'))

    # Dead socket pruned; healthy kept and already delivered to.
    assert hub.connections["dst_1"] == [healthy]
    assert healthy.sent == ['{"progress": "x"}']


def test_forward_without_sockets_is_noop():
    hub.connections.clear()
    asyncio.run(hub._forward("dst_none", "{}"))  # must not raise


def test_forward_other_datasets_untouched():
    hub.connections.clear()
    other = FakeWS(fail=True)
    hub.register("dst_other", other)

    asyncio.run(hub._forward("dst_target", "{}"))

    assert "dst_target" not in hub.connections
    assert "dst_other" in hub.connections  # failure scoped to target dataset
    hub.connections.clear()


# ===========================================================================
# clear_resume_keys
# ===========================================================================


def test_clear_resume_keys_deletes_all_scanned(monkeypatch):
    fake = FakeSyncRedis(keys=["resume:a", "resume:b"])
    monkeypatch.setattr(hub, "redis_client", fake)

    hub.clear_resume_keys()

    assert fake.deleted == ["resume:a", "resume:b"]


def test_clear_resume_keys_swallows_redis_errors(monkeypatch):
    fake = FakeSyncRedis(fail=True)
    monkeypatch.setattr(hub, "redis_client", fake)

    hub.clear_resume_keys()  # must not raise
    assert fake.deleted == []


# ===========================================================================
# subscriber_loop (integration through the faked async redis stack)
# ===========================================================================


def _message(dataset_id, event):
    return {
        "type": "message",
        "data": json.dumps({"dataset_id": dataset_id, "event": event}),
    }


def test_subscriber_routes_events_and_skips_malformed(monkeypatch):
    hub.connections.clear()
    ws_hit = FakeWS("hit")
    ws_other = FakeWS("other")
    hub.register("dst_1", ws_hit)
    hub.register("dst_2", ws_other)

    pubsub = FakePubSub(
        [
            _message("dst_1", '{"done": true}'),   # delivered
            {"type": "info", "data": "x"},          # non-message -> skipped
            {"type": "message", "data": ""},        # empty payload -> skipped
            {"type": "message", "data": "{bad json"},  # unparsable -> skipped
            _message("dst_2", '{"progress": "p"}'),  # routed to dst_2
        ]
    )
    monkeypatch.setattr(
        hub.aioredis,
        "from_url",
        lambda *a, **kw: FakeAsyncRedis([pubsub]),
    )

    async def scenario():
        task = asyncio.create_task(hub.subscriber_loop())
        # Wait until both routable events landed (or time out).
        for _ in range(200):
            if ws_hit.sent and ws_other.sent:
                break
            await asyncio.sleep(0.01)
        await hub.stop_subscriber()
        return pubsub

    pubsub = asyncio.run(scenario())

    assert ws_hit.sent == ['{"done": true}']
    assert ws_other.sent == ['{"progress": "p"}']
    assert pubsub.subscribed == ["dataset:events"]
    assert pubsub.closed is True  # cleaned up on cancellation
    hub.connections.clear()


def test_subscriber_reconnects_after_redis_error(monkeypatch):
    hub.connections.clear()
    failing_pubsub = FakePubSub([], fail_subscribe=True)
    healthy_pubsub = FakePubSub([_message("dst_9", '{"done": true}')])
    client = FakeAsyncRedis([failing_pubsub, healthy_pubsub])

    sleeps = []
    monkeypatch.setattr(hub.aioredis, "from_url", lambda *a, **kw: client)

    async def fast_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(hub.asyncio, "sleep", fast_sleep)

    ws = FakeWS()
    hub.register("dst_9", ws)

    async def scenario():
        task = asyncio.create_task(hub.subscriber_loop())
        for _ in range(200):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        await hub.stop_subscriber()

    asyncio.run(scenario())

    assert client.created >= 2          # retried after the failure
    assert 3 in sleeps                  # documented backoff interval
    assert ws.sent == ['{"done": true}']
    hub.connections.clear()


def test_stop_subscriber_without_task_is_noop():
    asyncio.run(hub.stop_subscriber())  # must not raise


def test_start_subscriber_is_idempotent(monkeypatch):
    pubsub = FakePubSub([])
    monkeypatch.setattr(
        hub.aioredis, "from_url", lambda *a, **kw: FakeAsyncRedis([pubsub])
    )
    monkeypatch.setattr(hub.asyncio, "sleep", lambda delay: _noop_future())

    async def scenario():
        hub.start_subscriber()
        first = hub._subscriber_task
        hub.start_subscriber()  # while running -> same task reused
        assert hub._subscriber_task is first
        await hub.stop_subscriber()
        assert hub._subscriber_task is None
        assert first.done()

    asyncio.run(scenario())


def _noop_future():
    return asyncio.ensure_future(asyncio.sleep(0))

