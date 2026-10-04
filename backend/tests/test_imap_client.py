"""fetch_new_messages talks to the mailbox only through imaplib, so we swap
in a fake IMAP4_SSL to check search/fetch/dedup without a real network."""

from email.message import EmailMessage

import pytest

from shipping_agent import imap_client
from shipping_agent.config import ImapConfig


def _raw(**headers) -> bytes:
    msg = EmailMessage()
    for key, value in headers.items():
        msg[key] = value
    msg.set_content("Halo, ini email.")
    return bytes(msg)


class FakeImap:
    """Canned responses keyed by message number; select must be readonly."""
    messages: dict[bytes, bytes] = {}
    select_calls: list[tuple[str, bool]] = []

    def __init__(self, host, port):
        self.host, self.port = host, port

    def login(self, user, password):
        return "OK", [b"LOGIN completed"]

    def select(self, folder, readonly=False):
        FakeImap.select_calls.append((folder, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(sorted(FakeImap.messages))]

    def fetch(self, num, parts):
        raw = FakeImap.messages.get(num)
        if raw is None:
            return "NO", [None]
        return "OK", [(b"%s (RFC822 {%d}" % (num, len(raw)), raw)]

    def logout(self):
        return "BYE", [b"logout"]


CFG = ImapConfig(host="imap.example.com", port=993, username="ops@yourline.example",
                  password="secret", folder="INBOX", use_ssl=True, fetch_days=7)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeImap.messages = {}
    FakeImap.select_calls = []
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", FakeImap)


def test_fetch_new_messages_returns_unseen_only():
    FakeImap.messages = {
        b"1": _raw(**{"Message-ID": "<a@example.com>", "Subject": "Hello"}),
        b"2": _raw(**{"Message-ID": "<b@example.com>", "Subject": "World"}),
    }

    found = imap_client.fetch_new_messages(CFG, seen_ids={"<a@example.com>"})

    assert [mid for mid, _ in found] == ["<b@example.com>"]


def test_fetch_new_messages_selects_readonly():
    FakeImap.messages = {b"1": _raw(**{"Message-ID": "<a@example.com>"})}

    imap_client.fetch_new_messages(CFG, seen_ids=set())

    assert FakeImap.select_calls == [("INBOX", True)]


def test_fetch_new_messages_returns_nothing_when_all_seen():
    FakeImap.messages = {b"1": _raw(**{"Message-ID": "<a@example.com>"})}

    found = imap_client.fetch_new_messages(CFG, seen_ids={"<a@example.com>"})

    assert found == []


def test_fetched_ids_roundtrip(tmp_path):
    assert imap_client.read_fetched_ids(tmp_path) == set()

    imap_client.append_fetched_ids(tmp_path, ["<a@example.com>", "<b@example.com>"])

    assert imap_client.read_fetched_ids(tmp_path) == {"<a@example.com>", "<b@example.com>"}
