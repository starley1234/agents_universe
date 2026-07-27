.PHONY: help test test-e2e check build build-sandbox run serve clean install

help:
	@echo "make test          — все тесты (217 проверок)"
	@echo "make auto GOAL=... — автономный прогон на час"
	@echo "make test-e2e      — сквозной тест на реальных сокетах"
	@echo "make check         — самопроверка окружения"
	@echo "make build-sandbox — собрать образ песочницы (нужен для sandbox=docker)"
	@echo "make build         — собрать образ агента"
	@echo "make serve         — поднять HTTP API на localhost:8080"
	@echo "make install       — поставить команду 'agent' в ~/.local/bin"
	@echo "make clean         — убрать временные файлы"

test:
	@python3 tests/test_all.py
	@python3 tests/test_auto.py
	@python3 tests/test_mcp.py
	@python3 tests/test_retry.py

test-e2e:
	@python3 tests/test_e2e.py

check:
	@python3 -m agent --check

build-sandbox:
	docker build -f docker/Dockerfile.sandbox -t agent-sandbox:latest .

build: build-sandbox
	docker build -f docker/Dockerfile -t agent-system:latest .

serve:
	@python3 -m agent.server

install:
	@mkdir -p $(HOME)/.local/bin
	@printf '#!/usr/bin/env bash\ncd "%s" && exec python3 -m agent "$$@"\n' "$(CURDIR)" \
		> $(HOME)/.local/bin/agent
	@chmod +x $(HOME)/.local/bin/agent
	@echo "Установлено: ~/.local/bin/agent"
	@echo "Убедитесь, что ~/.local/bin в PATH"

auto:
	@python3 -m agent --auto -P autonomous --hours $(or $(HOURS),1) "$(GOAL)"

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -f .agent_render.png
	@echo "очищено"
