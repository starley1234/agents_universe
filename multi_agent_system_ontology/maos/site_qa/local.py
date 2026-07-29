"""Детерминированный QA статического сайта без браузерной зависимости."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any
REF = re.compile(r'(?:href|src)=["\']([^"\']+)["\']', re.I)
def check_site(root: str | Path) -> dict[str, Any]:
    root=Path(root).resolve(); pages=sorted(root.rglob("*.html")); errors=[]
    for page in pages:
        text=page.read_text(encoding="utf-8", errors="replace")
        if "<title" not in text.lower(): errors.append(f"{page.relative_to(root)}: нет <title>")
        for ref in REF.findall(text):
            if ref.startswith(("#","mailto:","tel:","http:","https:","data:")): continue
            target=(page.parent/ref.split("#",1)[0]).resolve()
            if root not in target.parents and target != root: errors.append(f"{page.relative_to(root)}: путь вне сайта {ref}")
            elif not target.exists(): errors.append(f"{page.relative_to(root)}: отсутствует {ref}")
    return {"pages": len(pages), "ok": not errors, "errors": errors}
