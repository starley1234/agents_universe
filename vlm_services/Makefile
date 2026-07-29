VENV ?= .venv
PY   ?= $(VENV)/bin/python
BIN  ?= $(VENV)/bin

.PHONY: help venv install test serve demo doctor docker clean

help:
	@echo "install   поставить зависимости в .venv"
	@echo "test      все проверки"
	@echo "serve     интерфейс на http://127.0.0.1:8081"
	@echo "demo      прогнать все 12 сервисов на демо-данных"
	@echo "doctor    самопроверка окружения"
	@echo "docker    собрать образ"

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install -qU pip

install: venv
	$(PY) -m pip install -qe ".[dev]"

test:
	$(PY) -m pytest tests -q

serve:
	$(BIN)/vlm serve

demo:
	$(BIN)/vlm demo-all --out out
	@echo "отчёты в out/"

doctor:
	$(BIN)/vlm doctor

docker:
	docker build -f docker/Dockerfile -t vlm-services:latest .

clean:
	rm -rf out .pytest_cache vlm_services.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
