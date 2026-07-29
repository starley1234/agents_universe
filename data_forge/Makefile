.PHONY: help test check serve clean

help:
	@echo "make test    — все тесты (398 проверок, нужен PostgreSQL/pgserver)"
	@echo "make check   — самопроверка окружения (импорт, конфиг)"
	@echo "make serve   — поднять HTTP API/дашборд на localhost:8200 (нужен DB_DSN)"
	@echo "make clean   — убрать временные файлы"

test:
	@python3 tests/test_config.py
	@python3 tests/test_store.py
	@python3 tests/test_quality_engine.py
	@python3 tests/test_mdm_matching.py
	@python3 tests/test_ontology_model.py
	@python3 tests/test_ontology_actions.py
	@python3 tests/test_connector_files.py
	@python3 tests/test_connector_sql.py
	@python3 tests/test_connector_onec_odata.py
	@python3 tests/test_connector_factory.py
	@python3 tests/test_pipeline.py
	@python3 tests/test_api.py

check:
	@python3 -c "import dataforge.config, dataforge.db.store, dataforge.quality.engine, \
	dataforge.mdm.matching, dataforge.ontology.model, dataforge.ontology.actions, \
	dataforge.pipeline.ingest, dataforge.api.server; \
	print('DataForge: импорт модулей — ок')"

serve:
	@python3 -m dataforge.server

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "очищено"
