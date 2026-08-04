.PHONY: help test check install serve ui clean demo docker-build docker-up docker-down

help:
	@echo "make test         — все тесты"
	@echo "make check        — самопроверка окружения"
	@echo "make install       — установить зависимости"
	@echo "make serve         — поднять REST API на порту 8118"
	@echo "make ui            — запустить Chainlit UI"
	@echo "make cli           — интерактивный CLI-режим"
	@echo "make demo          — демонстрация на тестовых данных"
	@echo "make clean         — убрать временные файлы"
	@echo "make docker-build  — собрать Docker-образ"
	@echo "make docker-up     — запустить через Docker Compose"
	@echo "make docker-down   — остановить контейнеры"

test:
	@python3 tests/test_config.py
	@python3 tests/test_ingestor.py
	@python3 tests/test_chunker.py
	@python3 tests/test_vector.py
	@python3 tests/test_graph.py
	@python3 tests/test_pipeline.py
	@python3 tests/test_rag.py
	@python3 tests/test_agent.py
	@python3 tests/test_api.py

check:
	@python3 -c "from spectrum import settings; s = settings(); print(f'✅ SPECTRUM config OK: port={s.app_port}, vector={s.vector_store}, llm={s.llm_profile.model or \"fake\"}')"

install:
	pip install -r requirements.txt

serve:
	@echo "Starting SPECTRUM API on port 8118..."
	@python3 -m uvicorn spectrum.api.app:create_app --factory --host 0.0.0.0 --port 8118 --reload

ui:
	@echo "Starting SPECTRUM UI..."
	@chainlit run spectrum/ui/app.py -w --port 8118

cli:
	@python3 -m spectrum.ui.app

demo:
	@python3 -m spectrum.demo

clean:
	@rm -rf data/chroma data/__pycache__ knowledge_base
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
