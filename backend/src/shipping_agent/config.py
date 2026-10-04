"""Business rules that staff can tune without touching the engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReconConfig:
    # Gross weight difference allowed between documents, in kg AND in percent.
    # A row is flagged only when it exceeds BOTH, so small rounding is ignored.
    weight_tolerance_kg: float = 500.0
    weight_tolerance_pct: float = 2.0
    # Indonesian NPWP has 16 digits since the 2024 NIK/NPWP integration.
    npwp_length: int = 16
    # Container IDs that differ by at most this many characters are flagged as
    # "look-alike" (typo or prefix confusion), e.g. MSKU1234565 vs MSKU1234556.
    lookalike_max_distance: int = 1
    # Columns that must agree across all documents for the same container.
    compare_fields: tuple[str, ...] = field(default=("pod", "seal_no", "size_type"))


DEFAULT_CONFIG = ReconConfig()


def llm_model() -> str | None:
    """LLM is optional. Returns the model id only if an API key is also present."""
    model = os.getenv("LLM_MODEL", "").strip()
    if model and os.getenv("ANTHROPIC_API_KEY"):
        return model
    return None


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_tls: bool = True


def smtp_config() -> SmtpConfig | None:
    """Sending is optional. Returns config only if host + credentials are all present."""
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not (host and username and password):
        return None
    return SmtpConfig(
        host=host, port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        username=username, password=password,
        sender=os.getenv("SMTP_FROM", "").strip() or username,
        use_tls=os.getenv("SMTP_USE_TLS", "true").strip().lower() not in {"0", "false", "no"},
    )


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    username: str
    password: str
    folder: str = "INBOX"
    use_ssl: bool = True
    fetch_days: int = 7


def imap_config() -> ImapConfig | None:
    """Fetching is optional. Returns config only if host + credentials are all present."""
    host = os.getenv("IMAP_HOST", "").strip()
    username = os.getenv("IMAP_USER", "").strip()
    password = os.getenv("IMAP_PASSWORD", "").strip()
    if not (host and username and password):
        return None
    return ImapConfig(
        host=host, port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        username=username, password=password,
        folder=os.getenv("IMAP_FOLDER", "").strip() or "INBOX",
        use_ssl=os.getenv("IMAP_USE_SSL", "true").strip().lower() not in {"0", "false", "no"},
        fetch_days=int(os.getenv("IMAP_FETCH_DAYS", "7").strip() or 7),
    )
