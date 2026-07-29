.PHONY: help test test-e2e check smoke job jobs worker build build-sandbox serve clean install auto do runs

# Тесты находятся сами: добавили файл tests/test_*.py — он попадёт в прогон.
# Перечисление вручную приводило к тому, что новый тест забывали вписать.
TESTS := $(sort $(filter-out tests/test_e2e.py,$(wildcard tests/test_*.py)))

help:
	@echo "make test          — все тесты (~1087 проверок, ~40 с)"
	@echo "make test-e2e      — сквозной тест на настоящих сокетах"
	@echo "make check         — самопроверка окружения (модель не трогает)"
	@echo "make smoke         — дымовой тест ЖИВОЙ модели: тянет ли инструменты"
	@echo "make do TASK=...   — простой режим: роль выбирается сама"
	@echo "make auto GOAL=... — автономный прогон (HOURS=1, USD=0)"
	@echo "make runs          — история прогонов"
	@echo "make job TASK=...  — поставить задание и забыть (HOURS=, NOTIFY=)"
	@echo "make jobs          — очередь заданий"
	@echo "make worker        — фоновый исполнитель (обычно под systemd)"
	@echo "make serve         — веб-интерфейс (PORT=8081 сменить порт)"
	@echo "make build-sandbox — образ песочницы (для sandbox=docker)"
	@echo "make build         — образ агента"
	@echo "make install       — команда 'agent' в ~/.local/bin"
	@echo "make clean         — убрать порождаемое"

# Прогон останавливается на первом упавшем файле: продолжать по
# сломанному коду смысла нет, а итог должен быть виден сразу.
test:
	@fail=0; total=0; \
	for f in $(TESTS); do \
		out=$$(python3 $$f); code=$$?; \
		echo "$$out"; \
		n=$$(echo "$$out" | sed -n 's/^пройдено: \([0-9]*\).*/\1/p'); \
		total=$$((total + $${n:-0})); \
		if [ $$code -ne 0 ]; then fail=1; break; fi; \
	done; \
	echo "────────────────────────────────────────"; \
	if [ $$fail -eq 0 ]; then echo "ВСЕГО ПРОЙДЕНО: $$total"; \
	else echo "ЕСТЬ ПРОВАЛЫ — см. выше"; fi; \
	exit $$fail

test-e2e:
	@python3 tests/test_e2e.py

check:
	@python3 -m agent --check

smoke:
	@python3 -m agent --smoke $(if $(MODEL),-m $(MODEL),)

do:
	@python3 -m agent --do "$(TASK)"

auto:
	@python3 -m agent --auto -P autonomous --hours $(or $(HOURS),1) \
		$(if $(USD),--max-usd $(USD),) "$(GOAL)"

runs:
	@python3 -m agent --runs

job:
	@python3 -m agent --job "$(TASK)" $(if $(HOURS),--hours $(HOURS),) $(if $(NOTIFY),--notify $(NOTIFY),)

jobs:
	@python3 -m agent --jobs

worker:
	@python3 -m agent.worker

serve:
	@python3 -m agent.server $(if $(PORT),--port $(PORT),) $(if $(HOST),--host $(HOST),)

build-sandbox:
	docker build -f docker/Dockerfile.sandbox -t agent-sandbox:latest .

build: build-sandbox
	docker build -f docker/Dockerfile -t agent-system:latest .

install:
	@mkdir -p $(HOME)/.local/bin
	@printf '#!/usr/bin/env bash\ncd "%s" && exec python3 -m agent "$$@"\n' "$(CURDIR)" \
		> $(HOME)/.local/bin/agent
	@chmod +x $(HOME)/.local/bin/agent
	@echo "Установлено: ~/.local/bin/agent"
	@echo "Убедитесь, что ~/.local/bin в PATH"

# Базу и рабочую папку НЕ трогаем: там память и результаты работы.
# Стереть их случайной командой было бы худшим видом «уборки».
clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -f .agent_render.png
	@echo "убрано порождаемое (база и workspace не тронуты)"
