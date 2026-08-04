from pathlib import Path
from app.services.workspace import safe_path

class FileSystemTools:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def list_dir(self, relative: str = ".") -> dict:
        path = safe_path(self.workspace, relative)
        return {"entries": [p.name + ("/" if p.is_dir() else "") for p in path.iterdir()]}

    def read_file(self, relative: str) -> dict:
        path = safe_path(self.workspace, relative)
        return {"path": relative, "content": path.read_text(encoding="utf-8")}

    def write_file(self, relative: str, content: str) -> dict:
        path = safe_path(self.workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": relative, "bytes": len(content.encode("utf-8"))}

    def append_file(self, relative: str, content: str) -> dict:
        path = safe_path(self.workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
        return {"path": relative, "bytes": len(content.encode("utf-8"))}
