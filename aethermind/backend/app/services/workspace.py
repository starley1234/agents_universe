from pathlib import Path
from uuid import UUID
from app.config import settings

def task_workspace(task_id: UUID | str) -> Path:
    path = Path(settings.workspace_path).resolve() / "tasks" / str(task_id)
    for sub in ["artifacts", "code", "data", "logs"]:
        (path / sub).mkdir(parents=True, exist_ok=True)
    scratch = path / "scratchpad.md"
    if not scratch.exists():
        scratch.write_text("# Scratchpad\n\n", encoding="utf-8")
    return path

def safe_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    if not str(candidate).startswith(str(workspace.resolve())):
        raise ValueError("Path escapes task workspace")
    return candidate
