"""Agent tools — built-in utilities exposed to the LLM."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, tool

from src.config import get_settings

log = logging.getLogger(__name__)


# ─── Built-in tools ─────────────────────────────────────────────────────
@tool
def web_search(query: str) -> str:
    """Search the web for information. Uses MCP search server if configured."""
    return f"[web_search] MCP search not configured. Query was: {query}"


@tool
def read_file(file_path: str) -> str:
    """Read a file from the workspace."""
    s = get_settings()
    full = os.path.normpath(os.path.join(s.WORKSPACE_PATH, file_path))
    if not full.startswith(os.path.normpath(s.WORKSPACE_PATH)):
        return "Error: path traversal blocked"
    try:
        return open(full, "r", encoding="utf-8").read()[:50_000]
    except FileNotFoundError:
        return f"Not found: {file_path}"
    except Exception as e:
        return f"Error: {e}"


@tool
def write_file(file_path: str, content: str) -> str:
    """Write content to a file in the workspace."""
    s = get_settings()
    full = os.path.normpath(os.path.join(s.WORKSPACE_PATH, file_path))
    if not full.startswith(os.path.normpath(s.WORKSPACE_PATH)):
        return "Error: path traversal blocked"
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written: {file_path} ({len(content)} chars)"
    except Exception as e:
        return f"Error: {e}"


@tool
def list_directory(dir_path: str = ".") -> str:
    """List files in a workspace subdirectory."""
    s = get_settings()
    full = os.path.normpath(os.path.join(s.WORKSPACE_PATH, dir_path))
    if not full.startswith(os.path.normpath(s.WORKSPACE_PATH)):
        return "Error: path traversal blocked"
    try:
        entries = []
        for name in sorted(os.listdir(full))[:100]:
            p = os.path.join(full, name)
            tag = "📁" if os.path.isdir(p) else f"📄 {os.path.getsize(p)}B"
            entries.append(f"{tag} {name}")
        return "\n".join(entries) or "(empty)"
    except Exception as e:
        return f"Error: {e}"


@tool
def execute_code(code: str, language: str = "python") -> str:
    """Execute code in a subprocess. Supported: python, bash."""
    cmds = {"python": ["python3"], "bash": ["bash"]}
    exts = {"python": ".py", "bash": ".sh"}
    if language not in cmds:
        return f"Unsupported: {language}"
    try:
        fd, path = tempfile.mkstemp(suffix=exts[language], dir="/tmp")
        with os.fdopen(fd, "w") as f:
            f.write(code)
        r = subprocess.run(cmds[language] + [path], capture_output=True,
                           text=True, timeout=60, cwd="/tmp")
        os.unlink(path)
        out = (r.stdout + ("\nSTDERR:\n" + r.stderr if r.stderr else ""))
        if r.returncode:
            out += f"\nexit={r.returncode}"
        return out[:10_000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "Timeout (60s)"
    except Exception as e:
        return f"Error: {e}"


@tool
def ask_user(question: str) -> str:
    """Ask the user a clarifying question when the task is ambiguous."""
    return f"[QUESTION]: {question}"


async def get_all_tools() -> list[BaseTool]:
    tools: list[BaseTool] = [web_search, read_file, write_file, list_directory, execute_code, ask_user]
    # MCP tools
    try:
        from src.mcp.manager import get_mcp_tools
        tools.extend(await get_mcp_tools())
    except Exception as e:
        log.debug("MCP tools unavailable: %s", e)
    return tools


async def run_tool_calls(calls: list[dict[str, Any]], tools: list[BaseTool]) -> list[dict]:
    """Execute tool_calls from an LLM response."""
    tmap = {t.name: t for t in tools}
    out = []
    for c in calls:
        name, args, cid = c.get("name", ""), c.get("args", {}), c.get("id", "")
        t = tmap.get(name)
        if not t:
            out.append({"tool": name, "id": cid, "result": f"Unknown tool: {name}", "ok": False})
            continue
        try:
            r = await t.ainvoke(args)
            out.append({"tool": name, "id": cid, "result": str(r)[:2000], "ok": True})
        except Exception as e:
            out.append({"tool": name, "id": cid, "result": f"Error: {e}", "ok": False})
    return out
