from __future__ import annotations

import base64
import logging
import os
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

LOG = logging.getLogger(__name__)
TOKEN_SCOPE = "offline_access https://outlook.office365.com/SMTP.Send"


def _get_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None


def _has_smtp_config() -> bool:
    required = [
        "ALERT_EMAIL_TO",
        "SMTP_SERVER",
        "SMTP_USER",
        "SMTP_FROM",
        "SMTP_TENANT_ID",
        "SMTP_CLIENT_ID",
        "SMTP_CLIENT_SECRET",
        "SMTP_REFRESH_TOKEN",
    ]
    return all(_env(name) for name in required)


def send_alert_email(subject: str, body: str) -> None:
    """Send an optional worker alert email when SMTP OAuth settings are provided."""

    if not _has_smtp_config():
        LOG.debug("Alert email skipped because SMTP settings are incomplete.")
        return

    token_url = f"https://login.microsoftonline.com/{_env('SMTP_TENANT_ID')}/oauth2/v2.0/token"
    payload = {
        "client_id": _env("SMTP_CLIENT_ID"),
        "client_secret": _env("SMTP_CLIENT_SECRET"),
        "refresh_token": _env("SMTP_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
        "scope": TOKEN_SCOPE,
    }

    try:
        response = requests.post(
            token_url,
            data=payload,
            timeout=15,
            verify=_get_bool("SMTP_VERIFY_TLS", True),
        )
        response.raise_for_status()
        access_token = response.json()["access_token"]

        message = MIMEMultipart()
        message["From"] = _env("SMTP_FROM")
        message["To"] = _env("ALERT_EMAIL_TO")
        message["Subject"] = Header(subject, "utf-8")
        message.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP(
            _env("SMTP_SERVER"),
            _get_int("SMTP_PORT", 587),
            timeout=15,
        )
        server.starttls()
        server.ehlo()
        oauth2_string = f"user={_env('SMTP_USER')}\x01auth=Bearer {access_token}\x01\x01"
        server.docmd(
            "AUTH",
            "XOAUTH2 " + base64.b64encode(oauth2_string.encode("utf-8")).decode("utf-8"),
        )
        server.sendmail(
            _env("SMTP_FROM"),
            [_env("ALERT_EMAIL_TO")],
            message.as_string(),
        )
        server.quit()
        LOG.info("Alert email sent: %s", subject)
    except Exception as exc:  # pragma: no cover - defensive logging path
        LOG.error("Failed to send alert email: %s", exc)
