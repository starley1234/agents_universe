# A.S.T.R.A. Architecture v0.2.0 — Detailed

## Overview Diagram

```
User Goal
   │
   ▼
[ Web UI: Playground / API: /api/agents/run ]
   │
   ▼
[ Project Isolation ] — ProjectRepo verifies project exists
   │
   ▼
[ Session Creation ] — AgentSession(status=running) saved to DB
   │
   ▼
[ LangGraph: agent_graph.ainvoke(initial_state) ]
   │
   ├─► retrieve_context: semantic_memory.search + ontology_store.get_subgraph_text
   ├─► plan: llm_gateway.chat(PLANNER_SYSTEM) → JSON array of steps
   │        Preserves completed_steps, only generates if empty
   │
   └─► LOOP (up to len(plan) iterations):
        act: 
          - semantic_memory.search(step) for extra context
          - tool_registry.get_tools_for_project → OpenAI tools format
          - llm_gateway.chat with tools, up to 5 tool-call rounds
          - tool_registry.call_tool routing by name
        reflect:
          - llm_gateway.chat(REFLECTOR_SYSTEM) → {progress, repeating, diversity}
          - fallback heuristic: md5 hash repetition detection
        circuit_breaker:
          - if repetition >=3 or entropy <0.15 → halt
        router:
          - halt → END
          - done (index >= len(plan)-1) → END
          - continue → advance → act
   │
   ▼
[ Memory Store ] — Embed result, save to MemoryChunk (PG or SQLite), commit
   │
   ▼
[ Session Finish ] — status=completed/halted/error, result saved
   │
   ▼
Return RunResponse to user + Toast in UI
```

## Storage Layers

### Projects table
```
Project(id, name, description, created_at, updated_at)
  ├─► memory_chunks (1-N)
  └─► agent_sessions (1-N) ─► milestones
```

### Memory Chunks
- **Postgres**: `id UUID, project_id UUID FK, text TEXT, embedding VECTOR(dim), metadata_json TEXT, consolidated BOOL, created_at`
  Search via `ORDER BY embedding <=> :query_vec LIMIT k` (pgvector)
- **SQLite fallback**: Same but `embedding_json TEXT` (JSON-encoded list). Search computes cosine similarity in Python over up to 500 recent chunks.

### SemanticMemory class
- `store(text, project_id, metadata)`: embed via `embedding_service.embed` (LiteLLM or deterministic fake), then `MemoryChunkRepo.add`. Retry 3 times on SQLite locked, else in-memory fallback dict.
- `search(query, project_id, top_k)`: try embedding similarity → text LIKE → in-memory.
- `list_recent`: for UI memory page.

### OntologyStore
- In-memory dict `project_id → DiGraph`
- Methods: `add_entity`, `add_relation`, `query_neighbors(BFS depth)`, `get_subgraph_text`, `save(path)`, `load(path)`
- Persistence: `workspace/{project_id}/ontology.json` (node_link_data)
- Used for context compression: instead of full history, inject subgraph text (few hops around keyword)

### Dreaming (Consolidation)
- Trigger: manually `python -m astra.tasks.dreaming_task` or scheduled via TaskIQ/cron
- Reads unconsolidated chunks (limit 50), calls LLM with CONSOLIDATION_PROMPT → JSON entities/relations
- Updates ontology_store and marks chunks consolidated
- Saves ontology to file

## LLM Gateway

File: `llm/gateway.py`

- `LLMProvider`: local, openrouter, mock
- `_lc_to_litellm`: LangChain BaseMessage → OpenAI dict
- `_convert_tools_for_litellm`: MCP format → OpenAI function format
- `chat(messages, tools, temperature, max_tokens, retries)`: resolves model/url/key from settings, calls `litellm.acompletion`, parses tool_calls back to LangChain format
- Retry: exponential backoff 0.5*2^attempt + jitter
- Mock mode: deterministic responses based on prompt detection (planner→JSON array, reflector→JSON progress, consolidation→entities/relations, else generic)

### Embeddings

- `aembedding` via LiteLLM, model `openai/{embedding_model}`
- Fallback deterministic fake embedding: hash text → seed → random uniform -1..1 → normalize

## MCP Integration

- `MCPClient`: wraps `mcp` SDK `sse_client` + `ClientSession`. Methods `connect() → bool`, `list_tools() → [{name, description, input_schema}]`, `call_tool(name, args) → str`, `disconnect()`
- `ToolRegistry`: holds `_clients: name→MCPClient`, `_tool_to_server: tool_name→server_name`. `init_global_servers()` reads `MCP_*_URL` from settings, connects, registers tools. `get_tools_for_project()` returns all tools in OpenAI format (project-specific filtering TODO). `call_tool()` routes to correct server.
- Failure tolerant: if MCP server unavailable, logs warning and continues without its tools.

## Web Layer

- FastAPI app with lifespan: setup_logging, init_db (tolerant), init MCP, ensure workspace
- Templates: Jinja2, path `src/astra/templates`, uses modern `TemplateResponse(request, name, context)` API
- Static: `src/astra/static` mounted at `/static`
- Routes:
  - HTML: `/`, `/ui/projects`, `/ui/projects/{id}`, `/ui/projects/{id}/playground`, `/ui/projects/{id}/graph`, `/ui/projects/{id}/memory`, `/ui/settings`
  - JSON: `/api/stats`, `/api/data/projects`, `/api/projects/{id}/sessions`, `/api/projects/{id}/memory`, `/api/projects/{id}/graph`, `/api/mcp/servers`, `/api/config`, `/api/health/full`
- Frontend: Tailwind CDN + Alpine.js, no build step, toast system via custom event
- Playground JS fetches `/api/agents/run` POST, shows running spinner, result, history

## API Layer

- `projects` router: CRUD (POST, GET list, GET by id, PATCH update, DELETE)
- `agents` router: POST `/run` synchronous, GET `/sessions/{id}`
- `health` router: `/health` liveness, `/health/ready` DB check
- Deps: `db_session` yields async session from `get_session`

## Background Tasks

- `tasks/worker.py`: tries `taskiq_redis.RedisStreamBroker`, fallback to `InMemoryBroker` if Redis unavailable
- `tasks/dreaming_task.py`: `run_dreaming_cycle()` iterates all projects, calls `consolidate_project`

## Error Handling

- Global exception handler returns 500 with detail (dev) or generic (prod)
- DB init tolerant: if fails, web UI still runs degraded
- MCP init tolerant
- LLM gateway retries 2 times + mock fallback
- Semantic memory has 3-tier fallback: embedding search → text search → in-memory

## Scalability Notes for 16GB VRAM

- LLM and embeddings externalized via LiteLLM → VRAM free for small local critic model (Phi-3) or local fallback inference
- Graph compression: only inject relevant subgraph (depth 1-2) instead of full history
- Async engine: plan/reflect and tool execution separated, GPU can be used for embedding while LLM call in flight
- SQLite mode allows running without Postgres/Redis for edge devices

## Future Improvements (Production)

- Streaming: replace sync `/run` with SSE that emits `step_start`, `tool_call`, `reflection`, `result` events
- Auth: JWT middleware, project ownership
- Queue: move agent execution to TaskIQ/ARQ worker, return job_id, poll status
- Metrics: Prometheus middleware, token usage tracking
- Graph DB: replace NetworkX with FalkorDB for persistent large graphs
- Prompt registry: versioned prompts in `prompts/` + Langfuse tracing
- Evaluation harness: golden tasks, success metrics
