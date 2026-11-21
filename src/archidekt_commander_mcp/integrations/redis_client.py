from __future__ import annotations

import redis.asyncio as redis_async

# Bounded socket timeouts keep requests responsive when Redis is unreachable:
# cache paths fall back to memory or upstream fetches instead of hanging on TCP.
REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0
REDIS_HEALTH_CHECK_INTERVAL_SECONDS = 30


def create_redis_client(redis_url: str) -> redis_async.Redis:
    client: redis_async.Redis = redis_async.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    )
    return client
