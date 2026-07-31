.PHONY: help test check serve benchmark build-pkg clean install

help:
	@echo "make test      — запустить все тесты (27 модулей, 239+ проверок)"
	@echo "make check     — самопроверка окружения и синтаксиса"
	@echo "make benchmark — запустить бенчмарк умного поиска (120 запросов)"
	@echo "make serve     — запустить HTTP API и Web UI на localhost:8090"
	@echo "make build-pkg — собрать дистрибутив пакета (wheel/sdist) в dist/"
	@echo "make clean     — удалить временные файлы"

test:
	@python3 tests/test_all.py

check:
	@python3 -m compileall -q . && echo "синтаксис: ок"
	@python3 -c "import sys; sys.path.insert(0, '..'); import agent_toolkit; print('импорт: ок (версия', agent_toolkit.__version__, ')')"

benchmark:
	@PYTHONPATH=.. python3 -m agent_toolkit benchmark

build-pkg:
	@mkdir -p dist
	@python3 -m pip wheel -q -w dist . --no-deps
	@echo "✔ Пакет успешно собран в папку dist/:"
	@ls -lh dist/

serve:
	@PYTHONPATH=.. python3 -m agent_toolkit.api --port 8090

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -rf .pytest_cache build dist *.egg-info
	@echo "очищено"
