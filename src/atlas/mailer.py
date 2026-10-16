"""Outbound email. Uses SMTP when ATLAS_SMTP_HOST is set; otherwise logs the message for the operator."""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("atlas.mailer")


def configured() -> bool:
    return bool(os.getenv("ATLAS_SMTP_HOST"))


def send(to: str, subject: str, body: str) -> bool:
    """Send an email; returns True if handed to SMTP, False if only logged."""
    if not configured():
        log.warning("SMTP not configured; email to %s not sent. Subject: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = os.getenv("ATLAS_SMTP_FROM", "ATLAS <no-reply@localhost>")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    host, port = os.environ["ATLAS_SMTP_HOST"], int(os.getenv("ATLAS_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.getenv("ATLAS_SMTP_STARTTLS", "true").lower() == "true":
            smtp.starttls()
        user = os.getenv("ATLAS_SMTP_USER")
        if user:
            smtp.login(user, os.getenv("ATLAS_SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return True
