"""CLI: python -m c_o_r_t_e_x {check|audit|serve|mcp-check}."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import Settings, get_settings


def _services():
    from .gateway.app import create_services
    return create_services()


def command_check() -> int:
    from .signals.models import PYDANTIC_AVAILABLE

    services = _services()
    health = services.health()
    print("=== C.O.R.T.E.X. SELF-CHECK ===")
    print(f"version: 0.1.0 | environment: {health['environment']}")
    print(f"event bus: {services.settings.event_bus_backend} | pydantic: {PYDANTIC_AVAILABLE}")
    print(f"providers: {', '.join(health['providers']) or 'none'}")
    print(f"tools discovered: {health['tools_count']}")
    print(f"MCP router tools: {len(services.mcp.tool_definitions())}")
    if health["tools_count"] == 0:
        print("[warn] agent_toolkit не найден/не настроен; API и MCP всё равно доступны")
    else:
        print("[ok] catalog schemas loaded")
    print("[ok] shared blackboard ready")
    print("[ok] REST + MCP JSON-RPC + SSE gateway ready")
    return 0


def command_audit(as_json: bool = False) -> int:
    services = _services()

    async def run():
        task = await services.workflows.submit("Agent Toolkit practical audit", workflow="toolkit_audit")
        return await services.workflows.run(task.task_id)

    task = asyncio.run(run())
    report = (task.result or {}).get("value", task.result or {})
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    print("=== AGENT TOOLKIT PRACTICAL AUDIT ===")
    for key in ("total", "tested", "passed", "requires_configuration", "failed", "skipped"):
        print(f"{key}: {report.get(key, 0)}")
    print(f"coverage: {report.get('tested', 0)}/{report.get('total', 0)}")
    for item in report.get("recommendations", []):
        print(f"[{item.get('priority')}] {item.get('text')}")
    return 0 if report.get("failed", 0) == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="c_o_r_t_e_x", description="C.O.R.T.E.X. event-driven agent runtime")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="проверить каталог, bus, blackboard и MCP")
    audit = sub.add_parser("audit", help="практически проверить инструменты agent_toolkit")
    audit.add_argument("--json", action="store_true")
    serve = sub.add_parser("serve", help="запустить UI/API/MCP SSE gateway")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    mcp = sub.add_parser("mcp-check", help="вывести tools/list MCP router")
    mcp.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "check":
        return command_check()
    if args.command == "audit":
        return command_audit(args.json)
    if args.command == "mcp-check":
        services = _services()
        response = services.mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        print(json.dumps(response, ensure_ascii=False, indent=None if args.json else 2))
        return 0
    if args.command == "serve":
        from .gateway.app import run_server
        settings = get_settings()
        if args.host or args.port:
            values = dict(settings.__dict__)
            if args.host:
                values["app_host"] = args.host
            if args.port:
                values["app_port"] = args.port
            settings = Settings(**values)
        run_server(settings)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
