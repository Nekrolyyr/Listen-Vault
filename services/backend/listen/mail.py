"""Sending the verification email through the SMTP server in the settings (see docs/multi-user-plan.md)."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import Settings


class MailError(RuntimeError):
    """The message could not be sent. The text is for the logs, not for the user."""


def send(settings: Settings, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            server.starttls(context=context)
        with server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"sending to the SMTP server failed: {e}") from e
