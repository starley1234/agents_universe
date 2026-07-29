VENV ?= .venv
PY   ?= $(VENV)/bin/python

.PHONY: venv install test run-all demo clean

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install -qU pip

install: venv
	$(PY) -m pip install -qe ".[dev]"

test:
	$(PY) -m pytest tests -q

run-all:
	$(PY) -m aconstructor run-all

demo:
	$(PY) -m aconstructor run-all --out out
	@echo "отчёты в out/"

clean:
	rm -rf out .pytest_cache **/__pycache__
