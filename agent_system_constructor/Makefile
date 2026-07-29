VENV ?= .venv
PY   ?= $(VENV)/bin/python
BIN  ?= $(VENV)/bin

.PHONY: help venv install test test-fast run-all demo serve doctor docker docker-run clean

help:
	@echo "install     поставить зависимости в .venv"
	@echo "test        все проверки"
	@echo "serve       веб-интерфейс на http://127.0.0.1:8080"
	@echo "doctor      самопроверка окружения"
	@echo "demo        прогнать все пайплайны, отчёты в out/"
	@echo "docker      собрать образ"
	@echo "clean       убрать мусор сборки"

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install -qU pip

install: venv
	$(PY) -m pip install -qe ".[dev]"

test:
	$(PY) -m pytest tests -q

test-fast:
	$(PY) -m pytest tests -q -x --ff

run-all:
	$(BIN)/aconstructor run-all

demo:
	$(BIN)/aconstructor run-all --out out
	@echo "отчёты в out/"

serve:
	$(BIN)/aconstructor serve

doctor:
	$(BIN)/aconstructor doctor

docker:
	docker build -f docker/Dockerfile -t aconstructor:latest .

docker-run:
	docker compose up -d
	@echo "интерфейс: http://127.0.0.1:8080/"

clean:
	rm -rf out .pytest_cache aconstructor.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
