"""Email reports via SMTP (aiosmtplib)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import get_settings

log = logging.getLogger(__name__)
_cfg = get_settings()


async def _send(to: str, subject: str, html: str, text: str | None = None) -> bool:
    if not _cfg.SMTP_USER or not _cfg.SMTP_PASSWORD:
        log.warning("SMTP not configured — skip")
        return False
    try:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["From"] = _cfg.SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        if text:
            msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        await aiosmtplib.send(msg, hostname=_cfg.SMTP_HOST, port=_cfg.SMTP_PORT,
                              username=_cfg.SMTP_USER, password=_cfg.SMTP_PASSWORD,
                              use_tls=_cfg.SMTP_USE_SSL)
        log.info("Email → %s: %s", to, subject)
        return True
    except Exception as e:
        log.error("Email failed: %s", e)
        return False


async def send_progress(to: str, title: str, task_id: str, progress: float,
                        current_step: str, done: int, total: int,
                        quality: float, iteration: int) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return await _send(to, f"[Agent] {title} ({progress:.0f}%)", f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
<h2 style="color:#2563eb">🤖 Progress Report</h2>
<p><b>Task:</b> {title}<br><b>ID:</b> {task_id[:8]}<br><b>Time:</b> {now}</p>
<div style="background:#f3f4f6;padding:15px;border-radius:8px">
<h3 style="margin-top:0">Progress: {progress:.0f}%</h3>
<div style="background:#e5e7eb;border-radius:4px;height:20px;overflow:hidden">
<div style="background:#2563eb;height:100%;width:{progress}%;border-radius:4px;
color:white;text-align:center;font-size:12px;line-height:20px">{progress:.0f}%</div></div>
<p>Steps: {done}/{total} · Quality: {quality:.2f} · Iteration: {iteration}</p></div>
<div style="background:#fef3c7;padding:10px;border-radius:8px;margin-top:10px">
<b>Current:</b> {current_step}</div>
<p style="color:#6b7280;font-size:12px;margin-top:20px">LangGraph Autonomous Agent</p>
</body></html>""")


async def send_completion(to: str, title: str, task_id: str, result: str,
                          quality: float, iterations: int,
                          duration: float | None = None) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dur = f"{duration / 60:.1f} min" if duration else "N/A"
    return await _send(to, f"[Agent] ✅ {title}", f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
<h2 style="color:#16a34a">✅ Task Completed</h2>
<p><b>Task:</b> {title}<br><b>ID:</b> {task_id[:8]}<br><b>Time:</b> {now}<br>
<b>Duration:</b> {dur}<br><b>Quality:</b> {quality:.2f}<br><b>Iterations:</b> {iterations}</p>
<div style="background:#f0fdf4;padding:15px;border-radius:8px">
<h3 style="margin-top:0">Result</h3>
<div style="white-space:pre-wrap">{result[:5000]}</div></div>
<p style="color:#6b7280;font-size:12px;margin-top:20px">LangGraph Autonomous Agent</p>
</body></html>""")
