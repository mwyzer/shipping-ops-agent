"""Pulls new mail from a real inbox via IMAP so staff don't have to export
.eml files by hand. Connects read-only: `select(..., readonly=True)` makes
imaplib reject any flag/delete operation, so it can only ever look.
"""

from __future__ import annotations

import imaplib
import json
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .config import ImapConfig

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_since(days: int) -> str:
    d = (datetime.now() - timedelta(days=days)).date()
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def fetch_new_messages(cfg: ImapConfig, seen_ids: set[str]) -> list[tuple[str, bytes]]:
    """Search messages since cfg.fetch_days ago; return (message_id, raw_rfc822)
    for any not already in seen_ids. Caller decides what counts as "seen"."""
    conn = imaplib.IMAP4_SSL(cfg.host, cfg.port) if cfg.use_ssl else imaplib.IMAP4(cfg.host, cfg.port)
    try:
        if not cfg.use_ssl:
            conn.starttls()
        conn.login(cfg.username, cfg.password)
        conn.select(cfg.folder, readonly=True)
        status, data = conn.search(None, "SINCE", _imap_since(cfg.fetch_days))
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH gagal: {status}")
        found: list[tuple[str, bytes]] = []
        for num in data[0].split():
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            # imaplib returns "tuples of message part envelope and data"; find the data half.
            raw = next((part[1] for part in msg_data if isinstance(part, tuple) and len(part) == 2), None)
            if raw is None:
                continue
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            message_id = str(msg["message-id"] or "") or f"<no-id-{num.decode()}@{cfg.host}>"
            if message_id in seen_ids:
                continue
            found.append((message_id, raw))
        return found
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def read_fetched_ids(work_dir: Path) -> set[str]:
    path = work_dir / "imap_fetched.jsonl"
    if not path.is_file():
        return set()
    return {json.loads(line)["message_id"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_fetched_ids(work_dir: Path, message_ids: list[str]) -> None:
    path = work_dir / "imap_fetched.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for mid in message_ids:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "message_id": mid}) + "\n")
