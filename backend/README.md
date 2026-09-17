# QueryMind Backend

FastAPI backend for **QueryMind** — an AI-powered data analysis platform. It handles user authentication, CSV ingestion with automated cleaning, dataset storage on PostgreSQL, a custom JSON-B DSL query engine, and an LLM-powered chat assistant that answers questions about uploaded data with real numbers and charts.

## Features

- **Authentication** — Email/password registration with e-mail verification, login/logout with rotating access + refresh JWTs in signed cookies, CSRF protection, Google OAuth 2.0, and password reset via transactional e-mail (SendGrid or SMTP).
- **Dataset pipeline** — CSV upload (up to 50 MB), automated cleaning (duplicate removal, per-column null counts and type inference), date enrichment (`<col>_day/_month/_year` derived columns), JSONB row storage.
- **DSL query engine** — Validate, compile, and execute structured query plans against JSONB rows directly in PostgreSQL (metrics, filters, sorts, grouping with date granularity day/month/quarter/year, limits).
- **AI chat assistant** — Streaming NDJSON answers driven by an OpenAI-compatible LLM tool loop: `get_profile` → `validate_dsl_tool` → `generate_final_response`. Records (charts) are deterministically enforced, answers are history-sanitized to force fresh queries, and all reported values come from executed queries.
- **Middleware** — Cookie-based authentication, per-user Redis response cache, CORS with credentials, structured request logging.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Framework | FastAPI 0.136 + Starlette 0.50 + Uvicorn |
| Language | Python 3.12 |
| ORM | SQLModel / SQLAlchemy 2.0 |
| Database | PostgreSQL 16 (rows stored as JSONB) |
| Migrations | Alembic |
| Cache / tokens | Redis 7 (verification tokens, OAuth state, response cache) |
| Auth | PyJWT + signed cookies, Argon2 password hashing, Google OAuth 2.0 |
| LLM | OpenAI SDK (compatible with DeepSeek, NVIDIA NIM, OpenAI, etc.) |
| Data | pandas (cleaning), httpx |
| Email | SendGrid HTTP API or SMTP (fallback) |
| Tests | pytest + pytest-asyncio |

## Getting Started

### Prerequisites

- Python 3.12
- PostgreSQL (default port 5433, database `querymind`)
- Redis (default port 6380)

### 1. Configure environment

```bash
cp .env.example .env
```

Key variables (full list in `.env.example` and `app/core/settings.py`):

| Variable | Required | Description |
| --- | --- | --- |
| `SECRET_KEY` | Yes | Long random string used for JWT + cookie signing |
| `DATABASE_URL` | Yes | SQLAlchemy Postgres URL (default: `postgresql://postgres:querymind123@localhost:5433/querymind`) |
| `REDIS_URL` | Yes | Redis URL used for tokens and caching (default: `redis://localhost:6380/0`) |
| `LLM_BASE_URL` | Yes | OpenAI-compatible API base (e.g. `https://api.deepseek.com`, NVIDIA NIM) |
| `LLM_API_KEY` | Yes | API key for the LLM provider |
| `LLM_MODEL` | Yes | Model id (e.g. `deepseek-v4-flash`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | For Google sign-in | OAuth web client credentials |
| `GOOGLE_REDIRECT_URI` | For Google sign-in | Must be registered exactly in the Google Cloud Console |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` | For e-mail flows | SMTP server for verification/reset e-mails |
| `SENDGRID_API_KEY` | Alternative to SMTP | SendGrid API key (preferred in production) |
| `CORS_ORIGINS` | No | JSON list of allowed frontend origins (default: `["http://localhost:3000", "http://localhost:3001"]`) |
| `MAX_UPLOAD_BYTES` | No | CSV upload limit (default 50 MB) |
| `ENVIRONMENT` | No | Set to `production` for Upstash Redis and Partitioned cookies |

### 2. Run

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive API docs: http://localhost:8000/docs

### 3. Test

```bash
pytest
```

## Project Structure

```
backend/
├── app/
│   ├── main.py                # FastAPI app factory, middleware stack, routers, lifecycle hooks
│   ├── db.py                  # Engine + session factory + init_db
│   ├── redis_conf.py          # Redis client (dev/persistent) + cache key generation + ping
│   ├── auth/                  # JWT/cookie utils, Google OAuth code exchange
│   ├── cleaning/              # CSV cleaning pipeline + date enrichment
│   ├── core/
│   │   └── settings.py        # Pydantic-settings configuration (all env vars)
│   ├── dsl/
│   │   ├── base.py            # Abstract DSL compiler base
│   │   └── postgresjsonBcompiler.py  # JSONB SQL compiler + executor
│   ├── email/
│   │   └── sender.py          # SendGrid/SMTP e-mail dispatch + HTML templates
│   ├── mcp/
│   │   ├── tool_registry.py   # LLM tool definitions (get_profile, validate_dsl_tool, etc.)
│   │   └── executor.py        # Tool-call execution loop
│   ├── models/
│   │   ├── user.py            # User + AuthProviderEnum
│   │   ├── dataset.py         # Dataset (owns JSONB schema)
│   │   ├── datasetrow.py      # DatasetRow (JSONB data)
│   │   └── message.py         # Message + role/status/chart enums
│   ├── profiler/
│   │   └── profiler.py        # Dataset profiling (column stats, quality metrics)
│   ├── routers/
│   │   ├── auth.py            # /api/v1/auth — register, login, logout, OAuth, reset
│   │   ├── datasets.py        # /api/v1/datasets — upload, list, get, delete, records, query
│   │   ├── chat.py            # /api/v1/chat — WebSocket chat endpoint
│   │   ├── ws_hub.py          # WebSocket connection registry + Redis pub/sub subscriber
│   │   ├── llm_utils.py       # Prompt assembly, history sanitization, message persistence
│   │   ├── llm_event_stream.py # LLM tool-loop event generator (NDJSON lines)
│   │   └── llm_prompt.py      # System prompt template
│   └── schemas/
│       ├── auth.py            # Registration, login, reset, verification, user-out schemas
│       ├── dataset.py         # Upload, records, cleaning report, chat request schemas
│       ├── llm.py             # LLM response content blocks (text/record union)
│       └── query.py           # DSL query plan validation (filters, metrics, group_by)
├── middleware/
│   └── __init__.py            # RequestLoggingMiddleware, AuthenticationMiddleware, CacheMiddleware
├── alembic/
│   ├── env.py                 # Alembic environment configuration
│   └── versions/              # 8 migration scripts
├── tests/                     # pytest suite (~39 files, ~6800 lines)
├── Dockerfile / entry_point.sh # alembic upgrade head + uvicorn on :8000
├── requirements.txt
├── pytest.ini
└── alembic.ini
```

## API Overview

### Authentication (`/api/v1/auth`)

| Endpoint | Method | Auth | Description |
| --- | --- | --- | --- |
| `/register` | POST | No | Create account, send verification e-mail |
| `/login` | POST | No | Authenticate, set session cookies |
| `/logout` | POST | Yes | Revoke refresh token, clear cookies |
| `/me` | GET | Yes | Return authenticated user profile |
| `/refresh-token` | POST | Yes | Rotate refresh token, issue fresh pair |
| `/email` | POST | No | Resend verification e-mail |
| `/verify-email` | POST | No | Confirm e-mail ownership via token |
| `/reset-password` | POST | No | Send password-reset e-mail |
| `/reset-password/confirm` | POST | No | Apply new password via reset token |
| `/google/url` | GET | No | Return Google OAuth authorization URL |
| `/google-auth` | GET | No | Handle Google OAuth callback (307 redirect) |
| `/session` | POST | No | Exchange OAuth session code for cookies |

### Datasets (`/api/v1/datasets`)

| Endpoint | Method | Auth | Description |
| --- | --- | --- | --- |
| `/upload` | POST | Yes | Upload CSV → cleaning report + enriched dataset |
| `/` | GET | Yes | List all user datasets (newest first) |
| `/{id}` | GET | Yes | Get single dataset metadata |
| `/{id}` | DELETE | Yes | Delete dataset (cascades rows + messages) |
| `/{id}/records` | GET | Yes | Paginated data rows (50 per page) |
| `/{id}/messages` | GET | Yes | Paginated chat messages (newest first) |
| `/{id}/schema` | GET | Yes | Return JSONB column schema |
| `/{id}/profile` | GET | Yes | Dataset profiling (stats + quality metrics) |
| `/{id}/query` | POST | Yes | Execute a DSL query plan |

### Chat (`/api/v1/chat`)

| Endpoint | Method | Auth | Description |
| --- | ---| ---| ---|
| `/{dataset_id}/query` | WebSocket | Yes | Stream LLM answers as NDJSON events |

### System

| Endpoint | Method | Auth | Description |
| --- | ---| ---| --- |
| `/` | GET | No | Service landing page |
| `/health` | GET | No | Liveness + Redis connectivity check |
| `/docs` | GET | No | Swagger UI |

## Chat Assistant Protocol

1. The router builds a system prompt embedding the dataset schema and a strict ruleset, then runs an OpenAI-compatible tool loop (`MAX_TOOL_ITERATIONS`).
2. Tools: `get_profile` (statistics + sample rows), `validate_dsl_tool` (validates and executes one plan; returns real result rows), `generate_final_response` (final text + record blocks).
3. Deterministic safeguards:
   - Every successful `validate_dsl_tool` must be followed by a final response containing a record block with the exact validated DSL; text-only replies after tool use are bounced.
   - Record blocks without a this-turn validation are bounced.
   - Conversation history is sanitized (truncated text, placeholder records) so the model cannot replay stale numbers and must re-query.
4. Output is NDJSON: `{"type":"text"|"record", ...}` messages, `{"done":true}`, and error events; only final assistant messages are persisted.

## DSL Engine

Plans are JSON validated by Pydantic (`app/schemas/query.py`) then compiled to SQL by `postgresjsonBcompiler` against JSONB row data:

- **`select`** — Plain field names (time bucketing only via `group_by`)
- **`filters`** — Operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `between`, `in`, `nin`, `contains`, `before`, `after`
- **`metrics`** — `sum`, `count`, `min`, `max`, `avg` with aliases
- **`group_by`** — Field names or `{field, granularity: day|month|quarter|year}` date buckets
- **`sorts`** — `{field, direction: asc|desc}` specifications
- **`limit`** — Row cap (1–1000)

Validation guarantees: `group_by` ⊆ `select` (plain names), metric fields exist, bucket expressions match the selected grouping mechanism, metric aliases differ from field names.

## WebSocket Protocol

The chat WebSocket at `/api/v1/chat/{dataset_id}/query` uses newline-delimited JSON (NDJSON):

**Client → Server** (one text frame per query):
```json
{"message": {"id": "m1", "role": "user", "status": "pending", "content": "What is the average revenue?"}, "history": []}
```

**Server → Client** (stream of events):
```json
{"progress": "Analyzing your question..."}
{"type": "text", "text": "Here's what I found..."}
{"type": "record", "dsl_field": {...}, "chart_type": "barchart"}
{"done": true}
```

Keepalive: client sends `{"ping": 1}`, server replies `{"pong": 1}`.

## Docker

The full stack is orchestrated via `docker-compose.yml` at the repository root:

```bash
docker compose up -d
```

This starts four services:
| Service | Port | Description |
| --- | --- | --- |
| `backend` | 8000 | FastAPI application |
| `frontend` | 3001 | Next.js frontend |
| `postgres` | 5433 | PostgreSQL 16 |
| `redis` | 6380 | Redis 7 |

For a standalone backend build:

```bash
docker build -t query-mind-backend .
docker run -p 8000:8000 \
  --env-file .env \
  -e DATABASE_URL=... -e REDIS_URL=... \
  query-mind-backend
```

The entrypoint runs `alembic upgrade head` before starting Uvicorn. In the docker-compose stack the service connects to `postgres:5432` / `redis:6379` internally.

## Security

- Passwords hashed with Argon2; JWTs rotated on refresh; access tokens short-lived (default 15 min)
- Session stored in signed, HTTP-only cookies; CSRF tokens required on state-changing requests
- `.env` is git-ignored — secrets never committed
- CORS restricted to configured origins; user data queries are ownership-checked per request
- WebSocket connections authenticate via the same JWT cookie; origin header is validated
- OAuth state tokens stored in Redis (10 min TTL) to prevent login-CSRF
- Verification and reset tokens are single-use with 30-minute expiry

## License

Private project.
