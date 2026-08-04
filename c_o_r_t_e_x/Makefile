.PHONY: help test check audit serve mcp-check clean

help:
	@echo "make test       — unit tests без внешних сервисов"
	@echo "make check      — self-check runtime, catalog, API and MCP"
	@echo "make audit      — практический аудит всех agent_toolkit инструментов"
	@echo "make serve      — UI/API/MCP SSE на 0.0.0.0:8117"
	@echo "make mcp-check  — JSON-RPC tools/list"

check:
	@PYTHONPATH=.. python3 -m c_o_r_t_e_x check

test:
	@PYTHONPATH=.. python3 -m unittest discover -s tests -p 'test_*.py' -v

audit:
	@PYTHONPATH=.. python3 -m c_o_r_t_e_x audit

mcp-check:
	@PYTHONPATH=.. python3 -m c_o_r_t_e_x mcp-check --json

serve:
	@PYTHONPATH=.. python3 -m c_o_r_t_e_x serve

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@find . -name '*.pyc' -delete
	@rm -rf .pytest_cache build dist *.egg-info
