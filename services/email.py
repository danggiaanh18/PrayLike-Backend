"""Async email sender backed by SMTP settings."""

import os
from email.message import EmailMessage
from typing import Optional

import aiosmtplib

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None

if load_dotenv:
    load_dotenv()


def env_flag(key: str) -> Optional[bool]:
    """Parse boolean-ish environment variable; returns None if unset."""
    value = os.getenv(key)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM")

_env_use_tls = env_flag("SMTP_USE_TLS")
if _env_use_tls is None:
    SMTP_USE_TLS = SMTP_PORT == 465
else:
    SMTP_USE_TLS = _env_use_tls

_env_starttls = env_flag("SMTP_STARTTLS")
if _env_starttls is None:
    SMTP_STARTTLS = not SMTP_USE_TLS
else:
    SMTP_STARTTLS = _env_starttls and not SMTP_USE_TLS


async def send_email(
    to_email: str, subject: str, text: str, html: Optional[str] = None
) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text)  # 純文字供舊式客戶端使用
    if html:
        msg.add_alternative(html, subtype="html")

    send_kwargs = dict(
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USERNAME,
        password=SMTP_PASSWORD,
    )
    if SMTP_USE_TLS:
        send_kwargs["use_tls"] = True
    elif SMTP_STARTTLS:
        send_kwargs["start_tls"] = True

    await aiosmtplib.send(msg, **send_kwargs)


__all__ = [
    "SMTP_FROM",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_STARTTLS",
    "SMTP_USE_TLS",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "send_email",
]
