"""Email notification sender via SMTP."""

from __future__ import annotations

import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from loguru import logger

from astra.config import settings


class EmailNotifier:
    """Sends structured reports via SMTP."""

    async def send(self, to: str, subject: str, body_html: str, body_text: str = "") -> None:
        """Send an HTML email with optional plain-text fallback."""
        if not settings.mail_server:
            logger.warning("Mail server not configured, skipping email to {}", to)
            return

        msg = MIMEMultipart("alternative")
        msg["From"] = settings.mail_from_address
        msg["To"] = to
        msg["Subject"] = subject

        if body_text:
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=settings.mail_server,
                port=settings.mail_port,
                username=settings.mail_username,
                password=settings.mail_password,
                use_tls=settings.smtp_use_ssl,
            )
            logger.info("✉️  Email sent to {}: {}", to, subject[:60])
        except Exception as exc:
            logger.error("Failed to send email to {}: {}", to, exc)
            raise

    async def send_milestone_report(
        self,
        to: str,
        project_name: str,
        milestone_title: str,
        content: str,
    ) -> None:
        """Send a formatted milestone report."""
        subject = f"[A.S.T.R.A.] {project_name}: {milestone_title}"
        html = f"""
        <h2>🎯 Milestone Reached</h2>
        <p><strong>Project:</strong> {project_name}</p>
        <p><strong>Milestone:</strong> {milestone_title}</p>
        <hr/>
        <div>{content}</div>
        <hr/>
        <p style="color:#888;font-size:12px">Sent by A.S.T.R.A.</p>
        """
        await self.send(to, subject, html, body_text=f"{milestone_title}\n\n{content}")


email_notifier = EmailNotifier()
