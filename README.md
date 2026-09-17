# QueryMind

An AI-powered data analysis platform. Upload a CSV, review the automated cleaning report, and ask questions in plain English. The assistant validates and runs its own query plans against your data and answers with real numbers and interactive charts.

## Features

- **Authentication** -- email/password sign-up with email verification, password reset, Google OAuth sign-in, session refresh with single-flight token rotation and CSRF-protected requests
- **Dataset upload** -- CSV ingestion (up to 50 MB) with automatic cleaning (duplicate removal, per-column null counts, type inference) and date enrichment (`_day`/`_month`/`_year` derived columns)
- **Dataset management** -- list, inspect records, view schema and profiling (column stats, quality metrics)
- **AI assistant chat** -- streaming NDJSON answers driven by an LLM tool loop that plans, validates, and executes DSL queries; responses include real computed values and interactive chart blocks (bar, line, pie, table, metric)
- **DSL query engine** -- structured JSON query plans compiled to SQL against PostgreSQL JSONB rows (metrics, filters, sorts, grouping with date granularity)
- **Chart record blocks** -- saved chart queries re-execute against the dataset so charts always reflect the validated plan

## Tech Stack

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript 5 |
| Styling | Tailwind CSS 4 + shadcn/ui |
| Charts | Recharts 3 |
| State | Zustand |
| Backend | FastAPI 0.136 + Uvicorn |
| Language | Python 3.12 |
| ORM | SQLModel / SQLAlchemy 2.0 |
| Database | PostgreSQL 16 (rows stored as JSONB) |
| Migrations | Alembic |
| Cache | Redis 7 |
| Auth | PyJWT + signed cookies, Argon2 hashing, Google OAuth 2.0 |
| LLM | OpenAI SDK (compatible with DeepSeek, NVIDIA NIM, OpenAI, etc.) |
| Data | pandas (cleaning) |
| Email | SendGrid HTTP API or SMTP |
| Deployment | Cloudflare Pages (frontend), Docker (backend) |

## Quick Start (Docker)

The full stack is orchestrated via `docker-compose.yml`:

```bash
docker compose up -d
```

This starts four services:

| Service | Port | Description |
| --- | --- | --- |
| `frontend` | 3001 | Next.js frontend |
| `backend` | 8000 | FastAPI application |
| `postgres` | 5433 | PostgreSQL 16 |
| `redis` | 6380 | Redis 7 |

Frontend: [http://localhost:3001](http://localhost:3001)
API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

## Manual Setup

### Prerequisites

- Node.js 22+ and npm 10+
- Python 3.12
- PostgreSQL 16
- Redis 7

### Backend

```bash
cd backend
cp .env.example .env       # configure secrets (see below)
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Key environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `SECRET_KEY` | Yes | Long random string for JWT + cookie signing |
| `DATABASE_URL` | Yes | PostgreSQL URL (default: `postgresql://postgres:querymind123@localhost:5433/querymind`) |
| `REDIS_URL` | Yes | Redis URL (default: `redis://localhost:6380/0`) |
| `LLM_BASE_URL` | Yes | OpenAI-compatible API base |
| `LLM_API_KEY` | Yes | API key for the LLM provider |
| `LLM_MODEL` | Yes | Model id (e.g. `deepseek-v4-flash`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | For Google sign-in | OAuth web client credentials |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` | For email flows | SMTP server for verification/reset emails |
| `SENDGRID_API_KEY` | Alternative to SMTP | SendGrid API key |

### Frontend

```bash
cd frontend
cp .env.local.example .env.local
npm ci
npm run dev
```

| Variable | Required | Description |
| --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | Yes | Backend URL, e.g. `http://localhost:8000` |
| `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | For Google sign-in | OAuth 2.0 web client ID |

## Project Structure

```
query_mind/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory, middleware, routers, lifecycle
│   │   ├── db.py                # Engine + session factory
│   │   ├── redis_conf.py        # Redis client + cache
│   │   ├── auth/                # JWT/cookie utils, Google OAuth
│   │   ├── cleaning/            # CSV cleaning pipeline + date enrichment
│   │   ├── core/settings.py     # Pydantic-settings configuration
│   │   ├── dsl/                 # DSL compiler + JSONB SQL executor
│   │   ├── email/sender.py      # SendGrid/SMTP email dispatch
│   │   ├── mcp/                 # LLM tool registry + execution loop
│   │   ├── models/              # SQLModel: User, Dataset, DatasetRow, Message
│   │   ├── profiler/            # Dataset profiling (stats + quality metrics)
│   │   ├── routers/             # Auth, datasets, chat, WebSocket hub
│   │   └── schemas/             # Pydantic request/response schemas
│   ├── middleware/               # Request logging, auth, cache middleware
│   ├── alembic/                 # Database migrations
│   ├── tests/                   # pytest suite
│   ├── Dockerfile
│   ├── entry_point.sh
│   └── requirements.txt
├── frontend/
│   ├── app/                     # App Router pages
│   │   ├── (dashboard)          # Protected: datasets, assistant
│   │   ├── signin/ signup/      # Auth pages
│   │   ├── verify-email/        # Email verification
│   │   └── forgot-password/     # Password reset
│   ├── components/
│   │   ├── assistant/           # Chat UI, message list, chart blocks
│   │   ├── auth/                # Auth shell with Google OAuth
│   │   ├── datasets/            # Upload modal, dataset views
│   │   └── landing/             # Marketing landing page
│   ├── hooks/                   # useAuth, useChat (NDJSON streaming), useDatasets
│   ├── lib/api/                 # Axios client, auth/chat/dataset API modules
│   ├── Dockerfile
│   └── package.json
├── docker-compose.yml
└── README.md
```

## API Overview

### Authentication (`/api/v1/auth`)

| Endpoint | Method | Description |
| --- | --- | --- |
| `/register` | POST | Create account, send verification email |
| `/login` | POST | Authenticate, set session cookies |
| `/logout` | POST | Revoke refresh token, clear cookies |
| `/me` | GET | Return authenticated user profile |
| `/refresh-token` | POST | Rotate refresh token |
| `/verify-email` | POST | Confirm email ownership via token |
| `/reset-password` | POST | Send password-reset email |
| `/google/url` | GET | Return Google OAuth authorization URL |
| `/google-auth` | GET | Handle Google OAuth callback |

### Datasets (`/api/v1/datasets`)

| Endpoint | Method | Description |
| --- | --- | --- |
| `/upload` | POST | Upload CSV -- cleaning report + enriched dataset |
| `/` | GET | List all user datasets |
| `/{id}` | GET | Get single dataset metadata |
| `/{id}` | DELETE | Delete dataset (cascades rows + messages) |
| `/{id}/records` | GET | Paginated data rows |
| `/{id}/schema` | GET | Return JSONB column schema |
| `/{id}/profile` | GET | Dataset profiling (stats + quality metrics) |
| `/{id}/query` | POST | Execute a DSL query plan |

### Chat (`/api/v1/chat`)

| Endpoint | Method | Description |
| --- | --- | --- |
| `/{dataset_id}/query` | WebSocket | Stream LLM answers as NDJSON events |

## Chat Assistant Protocol

1. The router builds a system prompt embedding the dataset schema, then runs an OpenAI-compatible tool loop (`MAX_TOOL_ITERATIONS`).
2. Tools: `get_profile` (statistics + sample rows), `validate_dsl_tool` (validates and executes a plan; returns real result rows), `generate_final_response` (final text + record blocks).
3. Deterministic safeguards: every successful `validate_dsl_tool` must be followed by a final response containing a record block with the exact validated DSL; conversation history is sanitized so the model cannot replay stale numbers.
4. Output is NDJSON: `{"type":"text"|"record", ...}` messages, `{"done":true}`, and error events.

## DSL Engine

Query plans are JSON validated by Pydantic then compiled to SQL against JSONB rows:

- **`select`** -- Plain field names (time bucketing only via `group_by`)
- **`filters`** -- `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `between`, `in`, `nin`, `contains`, `before`, `after`
- **`metrics`** -- `sum`, `count`, `min`, `max`, `avg` with aliases
- **`group_by`** -- Field names or `{field, granularity: day|month|quarter|year}` date buckets
- **`sorts`** -- `{field, direction: asc|desc}` specifications
- **`limit`** -- Row cap (1--1000)

## Deployment

### Frontend (Cloudflare Pages)

The frontend deploys automatically to Cloudflare Pages on merge to `main` via GitHub Actions. See `frontend/.github/workflows/deploy.yml`.

```bash
cd frontend
npm run deploy    # opennextjs-cloudflare build + deploy
```

### Backend (Docker)

```bash
cd backend
docker build -t query-mind-backend .
docker run -p 8000:8000 \
  --env-file .env \
  -e DATABASE_URL=... \
  -e REDIS_URL=... \
  query-mind-backend
```

The entrypoint runs `alembic upgrade head` before starting Uvicorn.

## Security

- Passwords hashed with Argon2; JWTs rotated on refresh; access tokens short-lived (15 min)
- Session stored in signed, HTTP-only cookies; CSRF tokens required on state-changing requests
- `.env` files are git-ignored -- secrets are never committed
- CORS restricted to configured origins; user data queries are ownership-checked per request
- WebSocket connections authenticate via JWT cookie; origin header is validated
- OAuth state tokens stored in Redis (10 min TTL) to prevent login-CSRF
- Verification and reset tokens are single-use with 30-minute expiry

## Testing

### Backend

```bash
cd backend
pytest
```

### Frontend

```bash
cd frontend
npm run lint
```

## License

Private project.
