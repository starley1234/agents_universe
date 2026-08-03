.PHONY: install dev run run-mock test lint format docker-up docker-down docker-dev docker-full migrate migrate-new dreaming clean setup

install:
	pip install -e .

dev:
	pip install -e ".[dev,redis]"

setup:
	cp -n .env.example .env || true
	mkdir -p workspace logs src/astra/static
	@echo ".env ready, workspace ready"

run:
	PYTHONPATH=src uvicorn astra.main:app --reload --port 8101

run-mock:
	ENVIRONMENT=development DATABASE_URL=sqlite+aiosqlite:///./workspace/astra.db LLM_DEFAULT_PROVIDER=mock \
	PYTHONPATH=src uvicorn astra.main:app --reload --port 8101

test:
	ENVIRONMENT=development DATABASE_URL=sqlite+aiosqlite:///./test.db LLM_DEFAULT_PROVIDER=mock \
	PYTHONPATH=src pytest -v

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff format src/ tests/

docker-up:
	docker compose up -d postgres redis

docker-down:
	docker compose down

docker-dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

docker-full:
	docker compose up -d --build

docker-logs:
	docker logs -f astra_app

migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate -m "$(msg)"

dreaming:
	PYTHONPATH=src python -m astra.tasks.dreaming_task

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache logs/*.log test.db workspace/astra.db workspace/*.db-shm workspace/*.db-wal
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
