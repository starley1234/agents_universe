# Production deployment (Docker)

1. Copy `.env.production.example` to `.env`, generate unique values for
`POSTGRES_PASSWORD` and `MAOS_API_TOKEN`, then add provider/MCP/mail secrets.
Never commit `.env`.
2. Start: `docker compose up -d --build`.
3. Check: `docker compose ps` and `curl http://127.0.0.1:8090/health`.

The API port is bound to `127.0.0.1` deliberately. Publish it through an HTTPS
reverse proxy (Caddy/nginx/Traefik) and retain `MAOS_API_TOKEN`. PostgreSQL has
no host port and is reachable only from the compose network.

Back up the `maos_postgres` and `maos_artifacts` volumes. For multi-host or
larger production deployments replace the artifact volume with S3/MinIO and
use managed PostgreSQL with pgvector.
