# AetherMind

AetherMind is an autonomous iterative-agent runtime: FastAPI control plane, Celery workers, PostgreSQL/pgvector persistence, Redis queues, Docker sandbox tooling, and a Next.js Mission Control UI.

## What is implemented

- FastAPI backend with task lifecycle API.
- Celery worker that executes one persisted agent iteration at a time and re-queues long-running tasks.
- LangGraph-compatible agent loop: plan, execute, observe, reflect, summarize, persist, route.
- PostgreSQL models for tasks, snapshots, events, artifacts, and vector memory.
- Workspace per task with file tools and a Docker-backed Python code interpreter.
- Budget guardrails, confidence scoring, low-confidence escalation rules, pause/resume/intervene/rollback.
- SSE live event stream for UI.
- Next.js + Tailwind Mission Control dashboard.
- Docker Compose for local development.
- Tests for state routing and guardrails.

## Quick start

```bash
cd aethermind
cp .env.example .env
docker compose up --build
```

Open:

- Frontend: http://localhost:8127
- API: http://localhost:8128/docs
- API health: http://localhost:8128/api/health

The frontend calls relative `/api/*` paths. In Docker these requests are handled by a Next.js runtime proxy route and forwarded to `BACKEND_INTERNAL_URL=http://api:8128`, so the browser never needs to reach container-internal hostnames.

## Local backend without Docker

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.main:app --host 0.0.0.0 --port 8128
```

## Core flow

1. Create a task via API/UI.
2. Backend persists `tasks` row and enqueues `run_agent_iteration`.
3. Worker loads latest state and executes exactly one iteration.
4. Worker writes `task_events`, `task_snapshots`, artifacts and updated task state.
5. If still runnable, worker re-queues the task.
6. UI receives realtime updates through SSE.

## Troubleshooting

If the UI shows `Backend connection problem`:

```bash
cd aethermind
docker compose ps
docker compose logs --tail=200 api frontend worker
curl http://localhost:8128/api/health
curl http://localhost:8127/api/health
```

Expected health response:

```json
{"status":"ok","project":"AetherMind"}
```

If `localhost:8128/docs` is unavailable, inspect the `api` logs first; the API container runs migrations before starting Uvicorn.

## Safety model

Dangerous operations are blocked by guardrails and converted to `AWAITING_USER` requests. Real secrets are not stored in the repository; use `.env` or a secret manager.
