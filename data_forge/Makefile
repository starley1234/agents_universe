.PHONY: help test check serve clean docker-build docker-up docker-down docker-logs

help:
	@echo "make test         — все тесты (513 проверок, нужен PostgreSQL/pgserver)"
	@echo "make check        — самопроверка окружения (импорт, конфиг)"
	@echo "make serve        — поднять HTTP API/дашборд на localhost:8200 (нужен DB_DSN)"
	@echo "make clean        — убрать временные файлы"
	@echo "make docker-build — собрать прод-образ (docker compose build)"
	@echo "make docker-up    — поднять app+PostgreSQL в Docker Compose"
	@echo "make docker-down  — остановить контейнеры (данные PostgreSQL сохраняются)"
	@echo "make docker-logs  — логи контейнера app"

test:
	@python3 tests/test_config.py
	@python3 tests/test_store.py
	@python3 tests/test_quality_engine.py
	@python3 tests/test_mdm_matching.py
	@python3 tests/test_ontology_model.py
	@python3 tests/test_ontology_actions.py
	@python3 tests/test_process_orchestrator.py
	@python3 tests/test_copilot.py
	@python3 tests/test_connector_files.py
	@python3 tests/test_connector_sql.py
	@python3 tests/test_connector_onec_odata.py
	@python3 tests/test_connector_factory.py
	@python3 tests/test_pipeline.py
	@python3 tests/test_api.py
	@python3 tests/test_prod_lifespan.py

check:
	@python3 -c "import dataforge.config, dataforge.db.store, dataforge.quality.engine, \
	dataforge.mdm.matching, dataforge.ontology.model, dataforge.ontology.actions, \
	dataforge.pipeline.ingest, dataforge.pipeline.orchestrator, dataforge.copilot.assistant, \
	dataforge.api.server; \
	print('DataForge: импорт модулей — ок')"

serve:
	@python3 -m dataforge.server

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "очищено"

# --- Docker Compose (прод-развёртывание, см. README.md за подробностями) ---
# Требуют .env с POSTGRES_PASSWORD и FORGE_API_TOKEN (см. README.md "Быстрый
# старт (продакшен, Docker Compose)"). НЕ проверялись в этой сессии реальным
# docker compose (Docker недоступен в среде разработки) — см. README.md за
# честной оговоркой о том, что именно проверено, а что нет.
docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app
