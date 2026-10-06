"""Email notifications. Does nothing (silently) until SMTP_USER, SMTP_APP_PASSWORD and NOTIFY_EMAIL are set."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def configured() -> bool:
    return all(os.environ.get(k, "").strip() for k in ("SMTP_USER", "SMTP_APP_PASSWORD", "NOTIFY_EMAIL"))


def send(subject: str, body: str) -> str:
    """Returns '' on success or when email isn't set up; otherwise a short error (never raises)."""
    if not configured():
        return ""
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"].strip()
    msg["To"] = os.environ["NOTIFY_EMAIL"].strip()
    msg["Subject"] = subject[:200]
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"].strip(), os.environ["SMTP_APP_PASSWORD"].replace(" ", ""))
            s.send_message(msg)
        return ""
    except (smtplib.SMTPException, OSError) as e:
        return f"email not sent ({e.__class__.__name__})"
