"""OpenAI-compatible Vision вызов; endpoint/key задаются вызывающим workflow."""
from __future__ import annotations
import base64, json, mimetypes, urllib.request
from pathlib import Path
def analyze_images(paths: list[str|Path], prompt: str, *, base_url: str, api_key: str, model: str) -> str:
    content=[{"type":"text","text":prompt}]
    for raw in paths:
        p=Path(raw); mime=mimetypes.guess_type(p.name)[0] or "image/jpeg"
        content.append({"type":"image_url","image_url":{"url":f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"}})
    req=urllib.request.Request(base_url.rstrip("/")+"/chat/completions", data=json.dumps({"model":model,"messages":[{"role":"user","content":content}],"response_format":{"type":"json_object"}}).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer "+api_key}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r: return json.loads(r.read())["choices"][0]["message"]["content"]
