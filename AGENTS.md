# AGENTS.md

## Project

AI-first helpdesk service: receives tickets via webhook, drafts replies from a knowledge base with an LLM, lets an operator approve/reject, then delivers the reply through the helpdesk API.

- Source spec: `test_spec.md`
- Agreed implementation plan and milestones: `PLAN.md`
- Stack: FastAPI + async SQLAlchemy 2.0 + Alembic + PostgreSQL; React (JavaScript, `.jsx`, **no TypeScript**) + Vite + Tailwind; Docker Compose.
- Deployment is local-only by decision. Do not add cloud/deploy configs.

If `backend/` does not exist yet, the repo is at bootstrap; follow `PLAN.md` milestone 1. Commands below are the contract to implement.

## Non-negotiable behavior

- `POST /webhooks/helpdesk` must return 2xx in under 2 seconds and must never call the LLM, knowledge base, or helpdesk inline. Persist the event, ticket, and generation job, then return `202`.
- Ingestion is at-least-once: duplicate and concurrent deliveries are normal. Deduplicate with a unique `event_id`, one ticket per `ticket_id`, and at most one active generation job per ticket. Never use `BackgroundTasks` or in-memory queues for ticket work.
- Accepted tickets must not be lost. Jobs live in PostgreSQL, are claimed with `FOR UPDATE SKIP LOCKED`, have leases, retries, and a terminal failed state visible in the UI. If the DB is unavailable, return `503` so the helpdesk retries.
- The LLM is an OpenAI-compatible HTTP API. Enforce 60 requests/minute for the whole service in PostgreSQL (token bucket), not per process. A call takes 5-30 seconds. Without or with an invalid key, still make the real HTTP request, then use the static fallback after a 5-30 second total delay.
- Reply delivery is at-least-once with a deterministic `Idempotency-Key`; the mock helpdesk deduplicates it. Never send the same reply twice from the customer's perspective. Keep one outbound row per ticket.
- `/health` returns `200 OK` without a DB dependency.
- Operator UI/API has no auth (explicit decision). Do not add login/JWT.
- UI, README, and seed articles are English. LLM drafts are in the customer's language.

## Design constraints

- PostgreSQL only. No Redis, Celery, pgvector, or SQLite. The job queue and rate limiter stay in Postgres.
- Alembic revisions are the only schema source. Every model change needs a revision; never call `metadata.create_all()` at app or test startup.
- Citations come from FTS retrieval, not from model output. Do not trust model-generated links.
- Outbound `reply_url` calls must be SSRF-safe: http/https only, no redirects, optional host allowlist.
- API JSON uses `snake_case`; timestamps are timezone-aware UTC.
- Commit history is submitted unsquashed. Make one logical commit per milestone; do not squash.

## Planned layout

```text
backend/app/          FastAPI app, models, services, worker, seed
backend/alembic/      migrations
backend/tests/        pytest suite
frontend/src/         React (JS) operator UI
frontend/nginx.conf   static serving + reverse proxy
docker-compose.yml    db + api + worker + web
PLAN.md               architecture, API, data model, milestones
```

## Commands (after milestone 1)

```bash
# Full stack; UI at http://localhost:8080, API docs at http://localhost:8000/docs
docker compose up --build
docker compose down -v

# Logs / focused service
docker compose logs -f api worker

# Backend
cd backend
alembic upgrade head
python -m app.seed
pytest -q
ruff check . && ruff format --check . && mypy app

# Single test
pytest tests/test_webhook.py::test_duplicate_event_is_ignored -q

# Frontend
cd frontend
npm ci
npm run dev
npm run build
```

## Tests

- Tests require PostgreSQL: they exercise FTS, `JSONB`, and `SKIP LOCKED`. SQLite is not an option.
- Set `LLM_SIMULATED_DELAY_MIN=0` and `LLM_SIMULATED_DELAY_MAX=0`; mock the provider with `respx`. Tests must never hit a real provider or network.
- `TEST_DATABASE_URL` points at a disposable Postgres database. Apply migrations once per session and truncate between tests.
- Critical cases: concurrent duplicate webhooks, webhook latency, job claiming, rate-limit refusal/deferral, keyless fallback (request was attempted), approve/reject idempotency, delivery retry with the same idempotency key, and no second delivery after a timeout.

## Repo gotchas

- `README.md` is currently a UTF-16LE placeholder containing `# Test-Project`; rewrite it as UTF-8 when writing the real README.
- `test_spec.md` is the source of truth for requirements. If code and prose disagree, fix the code.
