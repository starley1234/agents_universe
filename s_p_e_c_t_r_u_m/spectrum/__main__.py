"""CLI entrypoint: python -m spectrum."""

from __future__ import annotations

import sys


def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "demo":
            from .demo import run_demo
            run_demo()
        elif cmd == "serve":
            from .config import settings
            cfg = settings()
            import uvicorn
            from .api.app import create_app
            uvicorn.run(create_app, host="0.0.0.0", port=cfg.app_port)
        elif cmd == "cli":
            from .ui.app import run_cli
            run_cli()
        elif cmd == "check":
            from .config import settings
            s = settings()
            print(f"✅ SPECTRUM config OK")
            print(f"   Port: {s.app_port}")
            print(f"   Vector store: {s.vector_store}")
            print(f"   LLM provider: {s.llm_profile.model or 'fake/offline'}")
            print(f"   Chunk size: {s.chunk_size}")
            print(f"   VLM: {'enabled' if s.use_vlm else 'disabled'}")
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python -m spectrum [demo|serve|cli|check]")
    else:
        from .ui.app import run_cli
        run_cli()


if __name__ == "__main__":
    main()
