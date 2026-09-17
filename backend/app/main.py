"""QueryMind FastAPI application entry point.

Configures the ASGI application with CORS, custom middleware
(authentication, caching, request logging), all HTTP routers,
and lifecycle hooks for database initialisation and the Redis
pub/sub subscriber used for real-time streaming.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.settings import settings
from app.db import init_db
from app.routers import auth, chat, datasets, review
from app.routers.ws_hub import clear_resume_keys, start_subscriber, stop_subscriber
from middleware import (
    AuthenticationMiddleware,
    CacheMiddleware,
    RequestLoggingMiddleware,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title=settings.APP_NAME)

# Middleware is applied in reverse order of ``add_middleware`` calls:
# the first-added middleware is the outermost (executed last on
# request, first on response).  Order here:
#   1. RequestLoggingMiddleware  (outermost — logs every request)
#   2. CORSMiddleware            (handles preflight / origin checks)
#   3. AuthenticationMiddleware  (extracts and validates JWT)
#   4. CacheMiddleware           (innermost — short-circuits cached)
app.add_middleware(CacheMiddleware)
app.add_middleware(AuthenticationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(auth.router)
app.include_router(datasets.router)
app.include_router(chat.router)
app.include_router(review.router)


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise the database, clear stale resume keys, and start the
    Redis pub/sub subscriber for SSE streaming.
    """
    init_db()
    clear_resume_keys()
    start_subscriber()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Gracefully shut down the Redis pub/sub subscriber."""
    await stop_subscriber()


@app.get("/")
def root():
    """Health-check landing page returning service name and docs URL."""
    return {"status": "ok", "service": settings.APP_NAME, "docs": "/docs"}


@app.get("/health")
def health():
    """Deep health check including Redis connectivity."""
    from app.redis_conf import ping

    return {"status": "ok", "redis": ping()}
