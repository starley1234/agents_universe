.PHONY: install dev test run run-mock lint format clean

install:
	pip install -r requirements.txt

dev:
	pip install -e . || pip install -r requirements.txt

test:
	PYTHONPATH=. pytest -v

run:
	PYTHONPATH=. uvicorn src.main:app --host 0.0.0.0 --port 8117 --reload

run-mock:
	ENVIRONMENT=development DATABASE_URL=sqlite+aiosqlite:///./workspace/nexus_twin.db LLM_ACTIVE_PROVIDER=mock \
	PYTHONPATH=. uvicorn src.main:app --host 0.0.0.0 --port 8117 --reload

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-dev:
	docker compose -f docker-compose.dev.yml up -d --build

docker-logs:
	docker compose logs -f app

lint:
	python -m py_compile src/**/*.py && echo "Syntax OK"

clean:
	rm -rf __pycache__ .pytest_cache workspace/*.db workspace/*.db-shm workspace/*.db-wal
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
