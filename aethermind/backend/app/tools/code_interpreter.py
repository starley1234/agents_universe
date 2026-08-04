import subprocess
from pathlib import Path

class CodeInterpreter:
    def __init__(self, workspace: Path, timeout_seconds: int = 30):
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds

    def run_python(self, code: str) -> dict:
        script = self.workspace / "code" / "run.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(code, encoding="utf-8")
        cmd = [
            "docker", "run", "--rm", "--network", "none", "--cpus", "1", "--memory", "512m",
            "-v", f"{self.workspace}:/workspace", "-w", "/workspace", "python:3.11-slim",
            "python", "code/run.py",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_seconds)
            return {"exit_code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}
        except FileNotFoundError:
            # Developer fallback for machines without Docker. Production should disable this.
            result = subprocess.run(["python", str(script)], capture_output=True, text=True, timeout=self.timeout_seconds, cwd=self.workspace)
            return {"exit_code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:], "fallback": "local-python"}
        except subprocess.TimeoutExpired:
            return {"exit_code": 124, "stdout": "", "stderr": "execution timeout"}
