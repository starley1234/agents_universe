# 🤖 Agent Toolkit + LM Studio

Руководство по подключению **163 инструментов agent_toolkit** к LM Studio через OpenAI-compatible API с поддержкой **Function Calling**.

---

## Что такое LM Studio

[LM Studio](https://lmstudio.ai/) — десктопное приложение для локального запуска LLM-моделей. Предоставляет OpenAI-совместимый API на `http://localhost:1234/v1`, что позволяет использовать его как drop-in замену OpenAI для Function Calling.

---

## Быстрый старт

### 1. Установите LM Studio и загрузите модель

1. Скачайте [LM Studio](https://lmstudio.ai/) и установите.
2. Загрузите модель **с поддержкой tool calling / function calling**:

| Модель | Размер | Рекомендация |
|--------|--------|-------------|
| **Qwen 2.5 7B/14B Instruct** | 4–8 GB | ⭐ Лучший Function Calling |
| **Llama 3.1 8B Instruct** | 5 GB | ⭐ Хороший Function Calling |
| **Mistral 7B Instruct v0.3** | 4 GB | ✅ Работает |
| **Hermes 2 Pro (NousResearch)** | 4–8 GB | ✅ Специально обучен tools |
| **unsloth/gemma-4-12b-it** | 7 GB | ✅ Рекомендуется в config.py |

> ⚠️ **Важно**: не все модели поддерживают tool calling! Ищите пометку "tool calling", "function calling" или "structured output" в описании модели.

3. В LM Studio:
   - Откройте вкладку **"Local Server"** (иконка `< >` слева)
   - Выберите загруженную модель
   - Нажмите **"Start Server"**
   - Убедитесь, что порт — **1234**

### 2. Установите зависимости

```bash
cd agent_toolkit

# Создать виртуальное окружение
python3 -m venv ../.venv
source ../.venv/bin/activate

# Установить агент_toolkit со всеми зависимостями
pip install -e .[all]

# Установить OpenAI SDK (для Function Calling)
pip install openai
```

### 3. Настройте .env

Скопируйте файл `.env` (уже создан) или создайте свой:

```bash
# Основные параметры для LM Studio
LOCAL_LLM_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=local-model
LOCAL_LLM_API_KEY=lm-studio
```

### 4. Проверьте подключение

```bash
# Проверить, что LM Studio доступен и получить список моделей
python lmstudio_demo.py --check-connection

# Если LM Studio на другом хосте/порту:
python lmstudio_demo.py --check-connection --base-url http://192.168.1.100:1234/v1
```

### 5. Запустите демонстрацию

```bash
# Базовый запуск — расчёт прочности балки
python lmstudio_demo.py

# Свой промпт
python lmstudio_demo.py --prompt "рассчитай антенну Яги на 433 МГц с 5 элементами"

# Ограничить количество инструментов (для слабых моделей)
python lmstudio_demo.py --tools-limit 20

# CAD-задача
python lmstudio_demo.py --prompt "сгенерируй 3D модель шестерни с модулем 2 и 20 зубьями"

# Интерактивно
python lmstudio_demo.py --prompt "посчитай число Рейнольдса для скорости 20 м/с и длины 0.5 м"
```

---

## Как это работает

```
┌──────────────┐    ┌───────────────────┐    ┌──────────────┐
│  LM Studio   │    │   agent_toolkit   │    │    Польз.    │
│  (LLM API)   │◄──►│   (реестр 163     │◄──►│   (промпт)   │
│  :1234/v1    │    │   инструментов)   │    │              │
└──────────────┘    └───────────────────┘    └──────────────┘

Цикл Function Calling:
  1. Пользователь → agent_toolkit: промпт
  2. agent_toolkit → LM Studio: messages + tools[]
  3. LM Studio → agent_toolkit: tool_calls (имя + аргументы)
  4. agent_toolkit: выполняет инструмент через реестр
  5. agent_toolkit → LM Studio: результат tool
  6. LM Studio → Пользователь: финальный ответ
```

---

## Тестирование инструментов без LLM

Перед подключением LM Studio проверьте работоспособность инструментов:

```bash
# Запустить все 31 безопасный тест
python lmstudio_test_tools.py

# Тестировать только физику
python lmstudio_test_tools.py --skill physics

# Тестировать один инструмент
python lmstudio_test_tools.py --tool physics.calc_strength

# Интерактивный режим
python lmstudio_test_tools.py --interactive
```

---

## Альтернативные способы подключения

### A. HTTP REST API сервер

```bash
# Запустить сервер на порту 8090
python -m agent_toolkit serve --port 8090

# Откройте в браузере:
#   http://localhost:8090/ui    — Визуальный каталог
#   http://localhost:8090/docs  — Swagger UI
```

### B. MCP (Model Context Protocol)

LM Studio может выступать как MCP-клиент:

```bash
# MCP-эндпоинт доступен по адресу:
# POST http://localhost:8090/api/mcp/rpc
# Методы: initialize, tools/list, tools/call
```

### C. Python SDK напрямую

```python
from agent_toolkit import build_default_registry

reg = build_default_registry()

# Умный поиск инструмента
hits = reg.search("рассчитать антенну яги", limit=1)
print(hits[0][0].name)  # -> physics.calc_yagi_uda_antenna

# Выполнение
result = reg.execute("physics.calc_yagi_uda_antenna", freq_mhz=433.92, elements_count=5)
print(result)
```

---

## Советы по моделям для LM Studio

### Рекомендуемые настройки LM Studio

- **Temperature**: 0.1–0.3 (для точных tool calls)
- **Max Tokens**: 2048+
- **Context Length**: 8192+ (важно при большом количестве tools)
- **GPU Offload**: максимум слоёв на GPU

### Если модель не вызывает инструменты

1. Убедитесь, что модель поддерживает tool calling
2. Попробуйте `--tools-limit 15` — меньше инструментов = проще модели
3. Используйте более мощный system prompt:
   ```
   python lmstudio_demo.py --system-prompt "You are a helpful assistant with access to engineering tools. ALWAYS use the appropriate tool when calculations are needed. Never guess numerical results."
   ```

---

## Устранение неполадок

| Проблема | Решение |
|----------|---------|
| `Connection refused` | LM Studio не запущен → Start Server |
| `Model not found` | Проверьте `--model` и имя модели в LM Studio |
| Модель не вызывает tools | Используйте модель с поддержкой tool calling |
| `ModuleNotFoundError` | `pip install openai` |
| Медленная работа | Увеличьте GPU offload в LM Studio |
| Ошибка параметров | `python lmstudio_test_tools.py --tool <name>` |
