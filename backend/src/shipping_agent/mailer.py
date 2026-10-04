"""Sends a drafted .eml file over SMTP. Nothing here decides what gets sent;
callers only hand it files that already passed human review in `finalize()`.
"""

from __future__ import annotations

import smtplib
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .config import SmtpConfig


def send_eml(path: Path, cfg: SmtpConfig) -> str:
    """Parse the .eml at `path` and send it via SMTP+STARTTLS. Returns the Message-ID sent."""
    msg = BytesParser(policy=policy.default).parse(path.open("rb"))
    if not msg["From"]:
        msg["From"] = cfg.sender
    del msg["X-Draft-By"]
    with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
        if cfg.use_tls:
            smtp.starttls()
        smtp.login(cfg.username, cfg.password)
        smtp.send_message(msg)
    return str(msg["Message-ID"] or "")
