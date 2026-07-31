.PHONY: install dev run test lint docker-up docker-down migrate migrate-new dreaming clean

install:
	pip install -e .

dev:
	pip install -e ".[dev,redis]"

run:
	PYTHONPATH=src uvicorn astra.main:app --reload --port 8101

test:
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

docker-full:
	docker compose up -d --build

migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate -m "$(msg)"

dreaming:
	python -m astra.tasks.dreaming_task

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache logs/*.log
	find . -name "*.pyc" -delete
