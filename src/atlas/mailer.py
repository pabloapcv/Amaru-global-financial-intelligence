"""Outbound email. Uses SMTP when ATLAS_SMTP_HOST is set; otherwise logs the message for the operator."""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("atlas.mailer")


def configured() -> bool:
    return bool(os.getenv("ATLAS_SMTP_HOST"))


def send(to: str, subject: str, body: str, *, html: str | None = None, headers: dict[str, str] | None = None,
         from_name: str | None = None) -> bool:
    """Send an email (plain text, optionally with an HTML alternative); returns True if handed to SMTP, False if only logged."""
    if not configured():
        log.warning("SMTP not configured; email to %s not sent. Subject: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    sender = os.getenv("ATLAS_SMTP_FROM", "ATLAS <no-reply@localhost>")
    if from_name:  # keep the configured address, show the publication's name
        from email.utils import formataddr, parseaddr

        sender = formataddr((from_name, parseaddr(sender)[1]))
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port = os.environ["ATLAS_SMTP_HOST"], int(os.getenv("ATLAS_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.getenv("ATLAS_SMTP_STARTTLS", "true").lower() == "true":
            smtp.starttls()
        user = os.getenv("ATLAS_SMTP_USER")
        if user:
            smtp.login(user, os.getenv("ATLAS_SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return True
