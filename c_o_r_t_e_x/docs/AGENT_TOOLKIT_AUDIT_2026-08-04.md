# Agent Toolkit — practical baseline audit

Дата прогона: **2026-08-04 (UTC)**

Runner: `C.O.R.T.E.X. workflows.toolkit_audit` → native
`agent_toolkit.core.diagnostics.ProductionTester`

Режим: локальный workspace fixtures, без внешнего network и без production secrets.

## Сводка

| Метрика | Значение |
|---|---:|
| Обнаружено инструментов | **176** |
| Практически вызвано | **176** |
| Успешно | **163** |
| Требует настройки | **13** |
| Непредвиденных ошибок | **0** |
| Покрытие | **100%** |

`requires_configuration` не является ошибкой реализации: инструмент был вызван,
но корректно сообщил об отсутствии внешнего ресурса/реквизитов. Такие записи не
должны автоматически считаться рабочими в production policy.

## Инструменты, которым нужна конфигурация

- `files.create_archive`
- `files.run_script`
- `templates.render_markdown`
- `agent.call_subagent`
- `text.regex_replace`
- `code.apply_patch`
- `cad.fea_static`
- `site.add_page`
- `site.edit_content`
- `mcp.call_remote_tool`
- `s3.upload_file`
- `s3.download_file`

В зависимости от deployment конкретный набор может измениться: audit всегда
сохраняет `preview`, hint и latency в JSON-отчёте и не полагается на эту
статическую таблицу.

## Рекомендации

### Высокий приоритет — нет блокирующих runtime ошибок

1. Оставить `failed_tools=0` как release gate; любой новый `failed` должен
   открывать issue и временно отключать маршрут через policy profile.
2. Добавить regression tests на каждый новый tool schema и на классификацию
   `requires_configuration`/`failed`, чтобы ошибка реквизитов не маскировала баг.

### Средний приоритет — конфигурация

1. `S3`: задать endpoint/bucket/access key/secret в secret manager и проверить
   upload/download в staging, не в production workspace.
2. `mcp.call_remote_tool`: задать `MCP_AGENT_TOOLKIT`, проверить `tools/list`,
   timeout, auth и circuit breaker; remote failure не должен блокировать bus.
3. `cad.fea_static`: установить/проверить mesh + solver dependency и добавить
   эталонный STL с ожидаемым диапазоном результата.
4. `agent.call_subagent`: задать MCP/LLM provider и лимиты рекурсии.
5. `code.apply_patch`, `text.regex_replace`, `site.*`, `files.run_script` и
   `templates.render_markdown`: формализовать sandbox path policy и безопасные
   test fixtures; side effects разрешать только в отдельном workspace.

### Архитектурный приоритет

1. Запускать этот audit по расписанию после изменения registry и публиковать
   `toolkit.audit.completed` в Redis/NATS.
2. Разделить результаты на `local deterministic`, `external configured` и
   `HITL-required`, а не только на pass/fail.
3. Для каждой внешней интеграции включить timeout, retry budget и circuit
   breaker; перед `dangerous` вызовами использовать HITL Gateway.
4. Сравнивать `duration_ms` между релизами и добавить SLO: например p95
   latency для read-only tools.

## Повторный запуск

```bash
cd c_o_r_t_e_x
make audit
make audit  # после заполнения нужных .env реквизитов
python -m c_o_r_t_e_x audit --json > workspace/agent_toolkit_audit.json
```

Прогон не коммитит секреты или результаты в Git: JSON сохраняется в ignored
`workspace/`, а в репозитории остаётся только этот baseline и код повторяемого
workflow.
