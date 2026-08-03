.PHONY: up down logs test lint

up:                              ## Запуск (docker compose)
	docker compose up -d --build

down:                            ## Остановка
	docker compose down

logs:                            ## Логи
	docker compose logs -f app

test:                            ## Тесты
	python -m pytest tests/ -v

lint:                            ## Лint
	python -m py_compile src/main.py && echo "OK"
	python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('src/**/*.py',recursive=True)]" && echo "Syntax OK"
