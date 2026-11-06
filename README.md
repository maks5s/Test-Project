# AI-first Helpdesk Draft Service

An AI-first helpdesk that turns incoming customer questions into grounded reply
drafts, lets an operator approve or reject them, and delivers the approved answer
back to the helpdesk.

The service is built to survive the messy parts of the real contract: duplicate
webhooks, slow and rate-limited LLM calls, a helpdesk that sometimes returns 5xx
or times out, and a process that can restart at any moment. Tickets live in
PostgreSQL, work is claimed from a PostgreSQL queue, and replies are delivered
with a deterministic `Idempotency-Key`.

## Features

- `POST /webhooks/helpdesk` validates, persists, and answers `202` without calling
  the LLM, the knowledge base, or the helpdesk inline.
- Duplicate and concurrent deliveries are collapsed by `event_id`, `ticket_id`,
  and a partial unique index for active jobs.
- Draft generation runs in a separate worker with `FOR UPDATE SKIP LOCKED`
  claiming, leases, a reaper, retries, and a terminal failed state.
- A PostgreSQL token bucket enforces **60 LLM requests per minute for the whole
  service**, no matter how many worker processes run.
- The knowledge base is searched with PostgreSQL full-text search (with a trigram
  fallback). Citations come from retrieval, never from model output.
- Without an API key the service still makes the real provider request, then
  falls back to a static draft after a 5-30 second delay.
- Approve creates the outbound message and the send job in one transaction
  (transactional outbox). Repeating an action returns the current state.
- Delivery retries network errors, timeouts, `408`, `425`, `429`, and `5xx` with
  exponential backoff and the same idempotency key. Redirects are never followed.
- A mock helpdesk deduplicates replies by `Idempotency-Key`, and a React operator
  console shows tickets, drafts, delivery state, the knowledge base, and the mock
  inbox.
- `/health` returns `200: OK` without touching the database.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI, Pydantic v2 |
| Database | PostgreSQL 16, async SQLAlchemy 2.0, asyncpg |
| Migrations | Alembic (the only schema source) |
| Queue and rate limit | PostgreSQL tables, no Redis or Celery |
| HTTP client | `httpx.AsyncClient` |
| Worker | `python -m app.worker` |
| Frontend | React 19, Vite, Tailwind CSS 4, JavaScript only |
| Local runtime | Docker Compose |

## Architecture

```text
 helpdesk
    |  POST /webhooks/helpdesk (202 in one short transaction)
    v
 +--------+      +---------------+      +----------------------+
 |  api   | ---> | helpdesk_events|     | tickets / drafts /   |
 | FastAPI|      | jobs (pending) | --> | outbound_messages    |
 +--------+      +---------------+      +----------------------+
    ^                    |                        ^
    |                    v                        |
    |            +---------------+                |
    |            |    worker     | --- LLM -----> provider (OpenAI-compatible)
    |            | claim/lease   | --- FTS -----> articles
    |            +---------------+                |
    |                    |                        |
    |                    +--- POST reply_url -----+
    |                         Idempotency-Key
    |
 +--------+     /api/tickets, /api/articles, /api/dev/tickets, /mock/helpdesk/*
 |  web   |
 | React  |  http://localhost:8080 -> nginx -> api:8000
 +--------+
```

## Quick start

Requirements: Docker Desktop (or Docker Engine + Compose v2) and free ports
`8080`, `8000`, and `5433`.

```bash
docker compose up --build
```

- Operator console: http://localhost:8080
- API docs (Swagger): http://localhost:8000/docs
- Health: http://localhost:8000/health
- PostgreSQL is published on host port **5433** because port 5432 is often taken
  by a local installation.

The API container applies Alembic migrations and seeds eight knowledge-base
articles before it becomes healthy. The worker waits for the API.

Stop the stack, keeping data:

```bash
docker compose down
```

Reset everything, including the database volume:

```bash
docker compose down -v
```

Follow logs:

```bash
docker compose logs -f api worker
```

## Demo walkthrough

### 1. Simulate a ticket in the UI

Open http://localhost:8080, expand **Simulate ticket**, and submit the prefilled
form. It calls `POST /api/dev/tickets`, which builds the same payload a webhook
would send and stores it through the real ingestion path.

Without `LLM_API_KEY` the draft appears after 5-30 seconds with
`source=fallback`. The service does perform a real HTTP request to
`LLM_BASE_URL` first; the static draft is the documented fallback.

### 2. Same flow with curl

```bash
curl -i -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "evt_8f2a",
    "ticket_id": "T-1042",
    "customer_email": "user@example.com",
    "message": "I cannot export the report to PDF",
    "reply_url": "http://api:8000/mock/helpdesk/tickets/T-1042/replies"
  }'
```

The response is `202 Accepted` and `duplicate: false`. Send the same request
again: it returns `202` with `duplicate: true` and creates no extra row or job.

Poll the ticket until a draft is ready:

```bash
curl 'http://localhost:8000/api/tickets?status=draft_ready'
```

Approve it (use the ticket's internal `id` from the list):

```bash
curl -X POST http://localhost:8000/api/tickets/<TICKET_ID>/approve
```

The worker delivers the reply to `reply_url`. Check the mock inbox:

```bash
curl http://localhost:8000/mock/helpdesk/replies
```

Calling approve again is a no-op as far as delivery is concerned: the outbound
message and the job are created once, and the mock deduplicates the repeated
`Idempotency-Key`.

### 3. Reject instead of approve

`POST /api/tickets/<id>/reject` moves a `draft_ready` ticket to `rejected` and
never creates an outbound message. `POST /api/tickets/<id>/regenerate` queues a
new draft version and is allowed after a rejection or a generation failure.

If delivery fails terminally, the ticket becomes `send_failed`,
`outbound_messages.status` becomes `failed`, and the UI offers **Retry
delivery** (`POST /api/tickets/<id>/retry-delivery`).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhooks/helpdesk` | Accept a helpdesk ticket (`202`) |
| GET | `/health` | Liveness probe (`200 OK`, no DB access) |
| GET | `/api/tickets?status=&limit=&offset=` | Operator ticket list (`{items, total}`) |
| GET | `/api/tickets/{id}` | Ticket, all drafts, current draft, delivery state |
| POST | `/api/tickets/{id}/approve` | Queue the current draft for delivery |
| POST | `/api/tickets/{id}/reject` | Reject the current draft |
| POST | `/api/tickets/{id}/regenerate` | Queue another draft version |
| POST | `/api/tickets/{id}/retry-delivery` | Requeue a terminally failed send |
| GET | `/api/articles` | Knowledge-base article summaries |
| GET | `/api/articles/{slug}` | Full article body and tags |
| POST | `/api/dev/tickets` | Simulate a webhook (dev only) |
| POST | `/mock/helpdesk/tickets/{ticket_id}/replies` | Mock helpdesk receiver (`201`) |
| GET | `/mock/helpdesk/replies` | Mock inbox for demos |

`approve`, `reject`, `regenerate`, and `retry-delivery` are idempotent. Invalid
state transitions return `409`, unknown tickets return `404`, and validation
errors return `422`. JSON fields are `snake_case`; timestamps are timezone-aware
UTC.

## Configuration

Every variable has a working default, so the stack starts without a `.env` file.
Copy `.env.example` to `.env` to override values. Secrets are never committed.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://app:app@db:5432/app` | PostgreSQL DSN |
| `DB_CONNECT_TIMEOUT_SECONDS` | `2` | Keeps the webhook failure path short |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible API |
| `LLM_API_KEY` | empty | Empty enables the keyless fallback path |
| `LLM_MODEL` | `gpt-4o-mini` | Provider model name |
| `LLM_TIMEOUT_SECONDS` | `10` | Per-request timeout |
| `LLM_SIMULATED_DELAY_MIN/MAX` | `5` / `30` | Fallback delay window, measured from call start |
| `LLM_RATE_LIMIT_PER_MINUTE` | `60` | Service-wide token bucket |
| `WORKER_CONCURRENCY` | `32` | In-flight I/O tasks per worker |
| `JOB_LEASE_SECONDS` | `300` | Lease for a claimed job |
| `JOB_MAX_ATTEMPTS` | `5` | Generation attempt budget |
| `JOB_RETRY_BASE_SECONDS` / `MAX` | `2` / `60` | Job backoff |
| `HELPDESK_TIMEOUT_SECONDS` | `10` | Reply delivery timeout |
| `DELIVERY_MAX_ATTEMPTS` | `5` | Delivery attempt budget |
| `DELIVERY_BACKOFF_BASE_SECONDS` | `2` | 2, 4, 8, 16 ... |
| `DELIVERY_BACKOFF_MAX_SECONDS` | `60` | Backoff cap |
| `DELIVERY_JITTER_SECONDS` | `1` | Random spread added to each retry delay |
| `REPLY_URL_ALLOWED_HOSTS` | `api,localhost,127.0.0.1` | CSV SSRF host allowlist |
| `MOCK_HELPDESK_INTERNAL_URL` | `http://api:8000` | Base URL used by the simulator |
| `ENABLE_DEV_ENDPOINTS` | `true` | Exposes `/api/dev/tickets` |
| `VITE_API_BASE_URL` | empty | Empty means same-origin through nginx |

Provider examples:

```dotenv
# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# OpenRouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=openai/gpt-4o-mini

# Local Ollama
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=llama3.1
```

## How the guarantees work

### Webhook budget

Ingestion is one short transaction with three `INSERT ... ON CONFLICT` writes:
the raw event, an upsert of the ticket that does not overwrite it, and one
generation job. Nothing slow happens before the `202`. If PostgreSQL is
unreachable the endpoint returns `503`, and the helpdesk retries.

### Queue in PostgreSQL

Jobs are claimed with `FOR UPDATE SKIP LOCKED`; the claim commits before any
network I/O. Workers hold a lease, and a reaper returns expired leases to
`pending` or fails them after the attempt budget. A partial unique index keeps
at most one active job of each type per ticket. No in-memory queues and no
`BackgroundTasks` are involved in ticket work.

### LLM rate limit and fallback

The token bucket is one row in `llm_rate_limit`, read and updated under
`SELECT ... FOR UPDATE`, so every worker process shares the 60/minute limit.
Every real provider request spends a token and writes an `llm_calls` audit row.
If no token is available the job is deferred with `run_at = now() + wait`, not
failed.

Without a key (or after a `401`/`403`, network error, or exhausted retries) the
service sleeps until the 5-30 second target measured from the start of the call,
then stores a static draft built from the best retrieved article. The rest of the
system treats it like any other draft.

### Retrieval and citations

`websearch_to_tsquery('simple', ...)` over a weighted `tsvector`, ranked with
`ts_rank_cd`, returning at most three articles. A trigram similarity query is the
fallback for word forms FTS misses (for example Ukrainian text). Citations are
built from the retrieved rows, so the model cannot invent links.

### Moderation and delivery

`approve` locks the ticket row, verifies `draft_ready`, inserts the single
`outbound_messages` row (`ON CONFLICT (ticket_id) DO NOTHING`), inserts one
`send_reply` job, and flips the status to `approved` - all in one transaction.
The idempotency key is deterministic:
`ticket:<external_id>:draft:<draft_id>`.

Delivery POSTs `{ticket_id, message}` to `reply_url`. Only `http`/`https` targets
are allowed, redirects are disabled, and the host must be in
`REPLY_URL_ALLOWED_HOSTS`. A `2xx` marks the outbound row and the ticket as
sent. Retryable failures keep the outbound row `pending` and requeue the job;
permanent failures end in `outbound=failed`, `ticket=send_failed`,
`job=failed`.

Delivery is **at-least-once**. The customer cannot receive the same reply twice
because the receiver deduplicates on the deterministic `Idempotency-Key` (the
included mock does exactly that). Exactly-once without receiver-side
idempotency is impossible; this is the honest limit of the design.

## Local development without Docker

You need PostgreSQL 16, Python 3.12+, and Node 22+.

```bash
# 1. Database (any PostgreSQL 16 instance)
createdb -h localhost -U app app_test

# 2. Backend
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
export DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/app
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload

# 3. Worker (second terminal, same env)
cd backend
python -m app.worker

# 4. Frontend (third terminal)
cd frontend
npm ci
npm run dev                        # http://localhost:5173, proxies to :8000
```

The Vite dev server proxies `/api`, `/mock`, `/health`, `/docs`, and
`/openapi.json` to `http://localhost:8000`.

## Tests

The suite requires PostgreSQL: full-text search, JSONB, and `SKIP LOCKED` are
PostgreSQL features, and SQLite is not an option. Tests apply migrations once per
session, truncate tables between tests, disable the simulated LLM delay, and mock
the provider with `respx`. They never touch the real network.

```bash
cd backend
export TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/app_test

pytest -q
ruff check .
ruff format --check .
mypy app
```

Useful single test:

```bash
pytest tests/test_delivery.py::test_5xx_is_retried_with_the_same_idempotency_key -q
```

Coverage highlights:

- concurrent duplicate webhooks produce one event, one ticket, one job;
- webhook latency stays well below the helpdesk timeout;
- two workers never claim the same job;
- the 61st LLM call in a minute is deferred, not executed;
- keyless fallback still performs a real provider request;
- double approve produces one outbound row and one send job, and concurrent
  approvals converge on the same state;
- reject never creates delivery; regenerate creates a new draft version;
- `5xx` and timeouts retry with the same `Idempotency-Key`;
- redirects are not followed and hosts outside the allowlist are blocked;
- `/health` works without a database.

Frontend checks:

```bash
cd frontend
npm ci
npm run lint
npm run build
```

## CI

`.github/workflows/ci.yml` runs two jobs on every push and pull request:

1. **Backend**: PostgreSQL 16 service, Python 3.12, `pip install -e ".[dev]"`,
   `ruff check`, `ruff format --check`, `mypy app`, `pytest -q`.
2. **Frontend**: Node 22, `npm ci`, `npm run lint`, `npm run build`.

## Repository layout

```text
.
├── AGENTS.md                  agent working agreement
├── PLAN.md                    architecture, data model, milestones
├── README.md
├── test_spec.md               assignment requirements
├── .env.example
├── docker-compose.yml         db + api + worker + web
├── .github/workflows/ci.yml
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/               migrations (only schema source)
│   ├── app/
│   │   ├── api/               health, webhooks, tickets, articles, mock, dev
│   │   ├── services/          ingestion, retrieval, llm, rate_limit,
│   │   │                      generation, moderation, delivery, reply_urls
│   │   ├── jobs.py            claim / lease / requeue / fail / reaper
│   │   ├── worker.py
│   │   └── seed.py
│   └── tests/
└── frontend/
    ├── Dockerfile             Node build -> nginx
    ├── nginx.conf
    └── src/                   React (JS) operator console
```

## Data model

- `helpdesk_events`: raw webhook deliveries, unique `event_id`.
- `tickets`: one row per external ticket id, with lifecycle status and the
  current draft pointer.
- `drafts`: one row per generated version, with citations and provenance
  (`llm` or `fallback`).
- `jobs`: durable queue (`generate_draft`, `send_reply`) with attempts, leases,
  and a terminal failed state.
- `outbound_messages`: the transactional outbox, one reply per ticket, unique
  deterministic idempotency key.
- `articles`: the eight seeded knowledge-base articles and their `tsvector`.
- `llm_rate_limit`: the single-row token bucket.
- `llm_calls`: audit trail of provider calls, including failures and fallbacks.
- `mock_helpdesk_replies`: mock inbox, unique `idempotency_key`.

## Design decisions and limitations

- **No authentication.** The operator API and UI have no login by explicit
  decision for this test assignment. Do not expose them publicly.
- **Local only.** There is no cloud deployment configuration.
- **PostgreSQL only.** No Redis, Celery, pgvector, or SQLite. The queue and rate
  limiter are PostgreSQL tables.
- **Alembic is the schema source.** The application never calls
  `metadata.create_all()`; every schema change is a migration.
- **At-least-once delivery.** Duplicate suppression depends on the receiver
  honoring `Idempotency-Key`. If the worker dies after the helpdesk accepted a
  reply but before the database commit, the retry uses the same key.
- **One reply per ticket.** `outbound_messages.ticket_id` is unique, so a ticket
  cannot deliver two different replies.
- **Drafts in the customer's language.** The prompt asks the model to answer in
  the language of the incoming message; UI text, README, and seed articles are
  English.

## Troubleshooting

- **Port 5432 already in use**: the compose file publishes PostgreSQL on `5433`
  for that reason. Change the mapping if `5433` is also taken.
- **Webhook returns 503**: PostgreSQL is unreachable. The endpoint fails fast so
  the helpdesk can retry instead of timing out.
- **No draft for 5-30 seconds**: expected without a key; the service performs a
  real provider request and then uses the static fallback.
- **Reply is not delivered**: check `outbound_messages.last_error` and the
  worker logs. A host outside `REPLY_URL_ALLOWED_HOSTS`, or a redirect, is
  rejected deliberately.
- **Compose build runs out of disk**: remove unused images with
  `docker system prune`, then rebuild.
- **Reset the database**: `docker compose down -v && docker compose up --build`.

## Commit history

The work is committed per milestone, unsquashed:

1. `docs: add agent guide and implementation plan`
2. `chore: scaffold fastapi, react and docker compose`
3. `feat(db): add schema, migrations and knowledge base seed`
4. `feat(webhook): durable idempotent ticket ingestion`
5. `feat(worker): generate drafts with postgres queue and llm fallback`
6. `feat(moderation): idempotent approval and reply delivery`
7. `feat(ui): operator console for draft moderation`
8. `chore: dockerize, add ci and document usage`
