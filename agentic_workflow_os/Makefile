.PHONY: help test check demo serve run inbox clean install lint

help:
	@echo "make test    — все тесты (493 проверки, ~6 с, без сети и ключей)"
	@echo "make check   — самопроверка окружения"
	@echo "make demo    — демо-прогон на stub-модели: среда в работе без ключей"
	@echo "make serve   — HTTP API + веб-консоль на localhost:8070"
	@echo "make inbox   — что ждёт решения человека"
	@echo "make install — поставить команду 'awos' в ~/.local/bin"
	@echo "make clean   — убрать временные файлы и локальную базу"

test:
	@python3 tests/test_config.py
	@python3 tests/test_store.py
	@python3 tests/test_workflow.py
	@python3 tests/test_tools.py
	@python3 tests/test_roles.py
	@python3 tests/test_engine.py
	@python3 tests/test_api.py
	@python3 tests/test_cli.py

check:
	@python3 -m awos check

demo:
	@python3 -m awos demo $(or $(WF),research_brief)

serve:
	@python3 -m awos serve

inbox:
	@python3 -m awos inbox

run:
	@python3 -m awos run $(WF) --goal "$(GOAL)"

install:
	@mkdir -p $(HOME)/.local/bin
	@printf '#!/usr/bin/env bash\ncd "%s" && exec python3 -m awos "$$@"\n' "$(CURDIR)" \
		> $(HOME)/.local/bin/awos
	@chmod +x $(HOME)/.local/bin/awos
	@echo "Установлено: ~/.local/bin/awos"
	@echo "Убедитесь, что ~/.local/bin в PATH"

lint:
	@python3 -m compileall -q awos tests && echo "синтаксис: ок"

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -f awos.db awos.db-wal awos.db-shm
	@echo "очищено"
