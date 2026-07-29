.PHONY: help install test check init demo load migrate backup up down logs serve clean lint

help:
	@echo "make install — поставить зависимости (psycopg + pgserver для тестов)"
	@echo "make test    — все тесты (638 проверок, ~16 с, embedded PostgreSQL)"
	@echo "make check   — самопроверка окружения"
	@echo "make init    — создать схему БД и загрузить справочники АП"
	@echo "make load FILE=... — загрузить документ (PDF/Word/Excel)"
	@echo "make migrate — применить миграции схемы БД"
	@echo "make backup  — резервная копия базы (pg_dump)"
	@echo "make up      — поднять в Docker (см. QUICKSTART.md)"
	@echo "make demo    — сквозной сценарий на временной базе, без настройки"
	@echo "make serve   — рабочее место инженера на localhost:8090"
	@echo "make clean   — убрать временные файлы"
	@echo ""
	@echo "Нужен PostgreSQL 14+ с pgvector и переменная SAPS_DB_DSN."

install:
	@python3 -m pip install -r requirements.txt

test:
	@python3 tests/test_ingest.py
	@python3 tests/test_db.py
	@python3 tests/test_agents.py
	@python3 tests/test_teamcenter.py
	@python3 tests/test_config_export.py
	@python3 tests/test_api.py
	@python3 tests/test_pdf_autoload.py
	@python3 tests/test_prod.py

check:
	@python3 -m saps check

init:
	@python3 -m saps init --rules --indexes

demo:
	@python3 tools/demo.py

load:
	@python3 -m saps load "$(FILE)"

migrate:
	@python3 -m saps migrate

backup:
	@python3 -m saps backup

up:
	@docker compose up -d --build
	@docker compose run --rm saps migrate
	@echo "Готово: http://127.0.0.1:8090/dashboard"

down:
	@docker compose down

logs:
	@docker compose logs -f saps

serve:
	@python3 -m saps serve

lint:
	@python3 -m compileall -q saps tests && echo "синтаксис: ок"

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -rf workdir
	@echo "очищено"
