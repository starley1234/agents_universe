.PHONY: help test check serve clean

help:
	@echo "make test    — все тесты (182 проверки, нужен PostgreSQL/pgserver)"
	@echo "make check   — самопроверка окружения (импорт, конфиг)"
	@echo "make serve   — поднять HTTP API/дашборд на localhost:8100 (нужен DB_DSN)"
	@echo "make clean   — убрать временные файлы"

test:
	@python3 tests/test_config.py
	@python3 tests/test_store.py
	@python3 tests/test_procurement_agent.py
	@python3 tests/test_onec_adapter.py
	@python3 tests/test_api.py

check:
	@python3 -c "import app.config, app.db.store, app.agents.procurement, app.api.server; \
	print('ERP AI: импорт модулей — ок')"

serve:
	@python3 -m app.server

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "очищено"
