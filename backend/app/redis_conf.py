"""Redis client for query caching and refresh-token storage.

In development a standard ``redis-py`` client connects to local Redis;
in production the same client connects to the Render Redis add-on
over TCP.  Two separate client instances are created so that cached
query results and refresh tokens live in different Redis databases and
can be flushed independently.
"""

import json
import logging

import redis

from app.core.settings import settings

logger = logging.getLogger(__name__)


if settings.ENVIRONMENT == "production":

    redis_client = redis.Redis.from_url(
        settings.PROD_REDIS_URL, decode_responses=True, socket_timeout=5
    )

    refresh_redis = redis_client
    
else:
    redis_client = redis.Redis.from_url(
        settings.REDIS_URL, decode_responses=True, socket_timeout=5
    )

    # Build the refresh-token Redis URL by swapping the database number
    # at the end of the base URL (e.g. /0 -> /1).
    refresh_redis = redis.Redis.from_url(
        settings.REDIS_URL.replace(f"/{settings.REDIS_URL.rsplit('/', 1)[-1]}", f"/{settings.REDIS_REFRESH_DB}"),
        decode_responses=True,
        socket_timeout=5,
    )


def gen_cache_key(config: dict) -> str:
    """Derive a deterministic 64-char hex cache key from a DSL config dict.

    The dict is serialized to a canonical JSON string (sorted keys,
    no whitespace) and then SHA-256-hashed so that logically
    equivalent queries always hit the same cache entry regardless of
    original key order.

    Args:
        config: DSL query plan dictionary to hash.

    Returns:
        Lowercase hex SHA-256 digest of the canonical JSON payload.
    """
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()


def ping() -> bool:
    """Probe Redis connectivity.

    Returns:
        ``True`` if a PING round-trip succeeds; ``False`` on any
        ``RedisError`` (connection refused, timeout, etc.).
    """
    try:
        return bool(redis_client.ping())
    except redis.RedisError as exc:
        logger.warning("Redis unavailable: %s", exc)
        return False
