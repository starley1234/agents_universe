.PHONY: help test check serve install clean

help:
	@echo "make test    — все тесты (274 проверки, нужен PostgreSQL/pgserver)"
	@echo "make check   — самопроверка окружения (импорт, конфиг)"
	@echo "make serve   — поднять HTTP API/дашборд на localhost:8090"
	@echo "make maintenance — один цикл фонового обслуживания вручную"
	@echo "make clean   — убрать временные файлы"

test:
	@python3 tests/test_config_llm.py
	@python3 tests/test_memory_store.py
	@python3 tests/test_router.py
	@python3 tests/test_context.py
	@python3 tests/test_orchestrator.py
	@python3 tests/test_chain.py
	@python3 tests/test_maintenance.py
	@python3 tests/test_maintenance_runner.py
	@python3 tests/test_tts.py
	@python3 tests/test_api.py

check:
	@python3 -c "import maos.config, maos.memory.store, maos.orchestrator.service; \
	print('MAOS: импорт модулей — ок')"

serve:
	@python3 -m maos

maintenance:
	@python3 -m maos.maintenance_runner --once

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "очищено"
