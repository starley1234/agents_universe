# A.S.T.R.A.

**Autonomous Semantic & Topological Reasoning Agent**

An intelligent agent system for long-running autonomous task resolution with hybrid memory (RAG + Knowledge Graph), MCP tool integration, and plan-act-reflect architecture.

## Features

- **Hybrid Memory** — semantic vector search (pgvector) + dynamic knowledge graph (NetworkX)
- **Memory Dreaming** — background consolidation that turns raw facts into structured ontology
- **MCP Integration** — plug-and-play external tools via Model Context Protocol
- **Plan-Act-Reflect** — hierarchical planning with circuit breaker to prevent loops
- **Project Isolation** — each project has its own knowledge base and tool set
- **Multi-channel Notifications** — email and Telegram milestone reports
- **LiteLLM Gateway** — switch between OpenRouter, local LM Studio, Ollama, etc.

## Quick Start

### 1. Clone and configure

```bash
cd a_s_t_r_a
cp .env.example .env
# Edit .env with your API keys and database URL
```

### 2. Start infrastructure

```bash
docker compose up -d postgres redis
```

### 3. Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,redis]"
PYTHONPATH=src uvicorn astra.main:app --reload --port 8101
```

### 4. Run tests

```bash
PYTHONPATH=src pytest -v
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/health/ready` | Readiness probe (DB check) |
| POST | `/projects/` | Create a new project |
| GET | `/projects/` | List all projects |
| GET | `/projects/{id}` | Get project details |
| POST | `/agents/run` | Start an agent session |
| GET | `/docs` | Swagger UI (dev only) |

## Architecture

```
┌─────────────┐
│  Retrieve    │  ← Pull context from semantic + ontology memory
│  Context     │
└──────┬──────┘
       │
┌──────▼──────┐
│   Plan      │  ← Generate step list via LLM
└──────┬──────┘
       │
┌──────▼──────┐
│   Act       │  ← Execute current step via LLM + MCP tools
└──────┬──────┘
       │
┌──────▼──────┐
│  Reflect    │  ← Evaluate progress, detect repetition
└──────┬──────┘
       │
┌──────▼──────────┐
│ Circuit Breaker │  ← Halt if entropy too low / too many loops
└──────┬──────────┘
       │
       └──→ Continue / Done / Halt
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Logic & State** | LangGraph + Pydantic |
| **LLM Gateway** | LiteLLM |
| **Connectivity** | MCP Python SDK |
| **Database** | PostgreSQL + pgvector |
| **Graph Engine** | NetworkX |
| **Backend API** | FastAPI |
| **Background Tasks** | TaskIQ + Redis |
| **Logging** | Loguru |

## Project Structure

```
a_s_t_r_a/
├── src/astra/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings from .env
│   ├── core/                # Agent logic (LangGraph)
│   │   ├── agent.py         # Plan-Act-Reflect graph
│   │   ├── state.py         # TypedDict state definition
│   │   ├── planner.py       # Hierarchical task planner
│   │   ├── circuit_breaker.py
│   │   └── reflector.py
│   ├── memory/              # Memory subsystem
│   │   ├── semantic.py      # Vector RAG (pgvector)
│   │   ├── ontology.py      # Knowledge graph (NetworkX)
│   │   ├── dreaming.py      # Background consolidation
│   │   └── project_memory.py
│   ├── mcp/                 # MCP integration
│   │   ├── client.py        # SSE client
│   │   ├── tool_registry.py # Dynamic tool routing
│   │   └── servers.py
│   ├── llm/                 # LLM gateway
│   │   ├── gateway.py       # LiteLLM wrapper
│   │   └── embeddings.py
│   ├── db/                  # Database
│   │   ├── engine.py
│   │   ├── models.py
│   │   └── repositories.py
│   ├── api/routes/          # FastAPI routes
│   ├── tasks/               # Background tasks (TaskIQ)
│   ├── notifications/       # Email & Telegram
│   └── utils/               # Logging
├── tests/
├── docs/
├── alembic/
├── Dockerfile               # Multi-stage production build
├── docker-compose.yml
└── pyproject.toml
```

## License

MIT
