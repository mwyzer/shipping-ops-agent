"""mailer.send_eml talks to SMTP only through smtplib, so we swap in a fake
SMTP client to check headers and calls without touching a real network."""

from email.message import EmailMessage

import pytest

from shipping_agent import mailer
from shipping_agent.config import SmtpConfig, smtp_config


class FakeSmtp:
    instances: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSmtp.instances.clear()


def _write_eml(path, **headers):
    msg = EmailMessage()
    for key, value in headers.items():
        msg[key] = value
    msg["X-Draft-By"] = "shipping-ops-agent (belum dikirim)"
    msg.set_content("Halo, ini draft.")
    path.write_bytes(bytes(msg))


CFG = SmtpConfig(host="smtp.example.com", port=587, username="ops@yourline.example",
                  password="secret", sender="ops@yourline.example", use_tls=True)


def test_send_eml_logs_in_and_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    path = tmp_path / "draft.eml"
    _write_eml(path, To="customer@example.com", Subject="Re: Hello", From="ops@yourline.example")

    mailer.send_eml(path, CFG)

    smtp = FakeSmtp.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.started_tls
    assert smtp.logged_in == ("ops@yourline.example", "secret")
    assert smtp.sent["To"] == "customer@example.com"
    assert smtp.sent["X-Draft-By"] is None  # draft marker must not leak into the real send


def test_send_eml_fills_missing_from(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    path = tmp_path / "draft.eml"
    _write_eml(path, To="customer@example.com", Subject="Re: Hello")

    mailer.send_eml(path, CFG)

    assert FakeSmtp.instances[0].sent["From"] == "ops@yourline.example"


def test_smtp_config_disabled_without_credentials(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    assert smtp_config() is None


def test_smtp_config_reads_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "ops@yourline.example")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.delenv("SMTP_FROM", raising=False)

    cfg = smtp_config()

    assert cfg is not None
    assert cfg.sender == "ops@yourline.example"  # defaults to SMTP_USER
    assert cfg.port == 587
    assert cfg.use_tls is True
