# Production deployment (Docker & Kubernetes)

1. Copy `.env.production.example` to `.env`, generate unique values for
`POSTGRES_PASSWORD` and `MAOS_API_TOKEN`, then add provider/MCP/mail/vision secrets.
Never commit `.env`.
2. Start the production stack:
   ```bash
   docker compose up -d --build
   ```
   This launches 3 services:
   - **`db`** (`pgvector/pgvector:pg16`) — shared database for agents, memory, and ontology.
   - **`maos`** (`python -m maos`) — hardened HTTP API and Web Dashboard server on port 8090. If `MAOS_AUTO_SEED=true` (default), it automatically seeds demo agents (`coder`, `writer`, `analyst`) with their skills on first boot.
   - **`maos-maintenance`** (`python -m maos.maintenance_runner`) — background "Deep Thinking" worker that runs every `MAOS_MAINTENANCE_INTERVAL_SECONDS` to distill conversations, deduplicate memory quanta, synthesize duplicate ontology nodes, and extract entities/relations from dialogs.

3. Check service health and maintenance logs:
   ```bash
   docker compose ps
   docker compose logs -f maos-maintenance
   curl http://127.0.0.1:8090/health
   ```

## Production Hardening

Both `maos` and `maos-maintenance` run as non-root user `maos` (UID 10001) with:
- **`read_only: true`** root filesystem (with a 256MB `/tmp` tmpfs);
- **`cap_drop: ["ALL"]`** and **`security_opt: ["no-new-privileges:true"]`**;
- Isolated Docker volumes for PostgreSQL data (`maos_postgres`), agent workspaces (`maos_workspace`), and workflow artifacts (`maos_artifacts`).

The API port is bound to `127.0.0.1` deliberately. Publish it through an HTTPS
reverse proxy (Caddy/nginx/Traefik) and require `MAOS_API_TOKEN` for external access.
PostgreSQL is reachable only from the private Compose network.

