# A.S.T.R.A. — Architecture

## Overview

A.S.T.R.A. (Autonomous Semantic & Topological Reasoning Agent) is an intelligent agent system designed for long-running autonomous task resolution within isolated projects.

## Core Loop: Plan → Act → Reflect

```
┌─────────────┐
│  Retrieve    │  ← Pull context from semantic + ontology memory
│  Context     │
└──────┬──────┘
       │
┌──────▼──────┐
│   Plan      │  ← Generate/refresh step list (DAG)
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

## Memory Layers

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Semantic (RAG) | PostgreSQL + pgvector | Fast similarity search over facts |
| Ontology | NetworkX → FalkorDB | Entity-relation graph for structured reasoning |
| Dreaming | Background TaskIQ job | Periodic consolidation & deduplication |

## MCP Integration

Tools are loaded dynamically from MCP servers. Each project can have its own set of connected servers.

## Tech Stack

- **LangGraph** — agent orchestration with cycles
- **LiteLLM** — unified LLM API proxy
- **FastAPI** — HTTP API
- **PostgreSQL + pgvector** — vector + relational storage
- **NetworkX** — knowledge graph
- **TaskIQ + Redis** — background task queue
- **Loguru** — structured logging
