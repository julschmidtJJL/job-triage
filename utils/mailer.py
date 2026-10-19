"""Send the weekly report by email over Gmail SMTP with an app password.

An app password, not OAuth, because the job runs headless on GitHub Actions.

    MAIL_USER           sending Gmail address
    MAIL_APP_PASSWORD   its 16-letter app password (spaces are ignored)
    MAIL_TO             recipient; defaults to MAIL_USER
    SMTP_HOST/SMTP_PORT optional, default smtp.gmail.com:587
"""
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

SMTP_TIMEOUT = 30


def mail_configured() -> bool:
    return bool(os.environ.get("MAIL_USER", "").strip() and os.environ.get("MAIL_APP_PASSWORD", "").strip())


def send_mail(subject: str, text: str, html: str) -> tuple[bool, str]:
    """Returns (sent, message). Never raises and never echoes the password."""
    user = os.environ.get("MAIL_USER", "").strip()
    pw = os.environ.get("MAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    if not user or not pw:
        return False, "MAIL_USER or MAIL_APP_PASSWORD not set"
    if not re.fullmatch(r"[a-z]{16}", pw):
        # Gmail answers a malformed password exactly like a wrong one or a rate
        # limit; checking the shape first saves a day of guessing.
        return False, "MAIL_APP_PASSWORD is not 16 lowercase letters; not attempting a login"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="jobhunt.local")
    msg["X-Jobhunt-Report"] = "weekly"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pw)
            s.send_message(msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e).replace(pw, '<redacted>')}"
    return True, f"sent to {to}"
