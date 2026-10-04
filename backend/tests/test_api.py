from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from shipping_agent.api import create_app
from shipping_agent.agents.registry import AGENTS


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path / "web"))


def demo(client, agent="reconciliation"):
    res = client.post("/api/runs/demo", json={"agent": agent})
    assert res.status_code == 200, res.text
    return res.json()


def all_decisions(run, decision=None):
    key = decision or run["options"][0]["key"]
    return {"reviewer": "budi", "decisions": [{"finding_id": i["id"], "decision": key} for i in run["items"]]}


def test_dashboard_and_agent_list(client):
    assert "Shipping Ops Agent" in client.get("/").text
    agents = client.get("/api/agents").json()
    assert [a["name"] for a in agents] == list(AGENTS)
    assert all(a["options"][0]["key"] == "approve" for a in agents)


@pytest.mark.parametrize("name", list(AGENTS))
def test_demo_review_and_download_for_every_agent(client, name):
    run = demo(client, name)
    assert run["status"] == "awaiting_review" and run["agent"] == name and run["files"] == []
    done = client.post(f"/api/runs/{run['run_id']}/decisions", json=all_decisions(run, run["options"][-1]["key"])).json()
    assert done["status"] == "done"
    assert any(n.endswith(".xlsx") for n in done["files"]) and "audit_log.jsonl" in done["files"]
    for name_ in done["files"]:
        assert client.get(f"/api/runs/{run['run_id']}/files/{name_}").status_code == 200


def test_unknown_agent_demo_is_404(client):
    assert client.post("/api/runs/demo", json={"agent": "nope"}).status_code == 404


def test_partial_duplicate_and_foreign_decisions_are_rejected(client):
    run = demo(client)
    url = f"/api/runs/{run['run_id']}/decisions"
    body = all_decisions(run)
    body["decisions"] = body["decisions"][:-1]
    res = client.post(url, json=body)
    assert res.status_code == 422 and res.json()["detail"]["missing"] == [run["items"][-1]["id"]]
    body = all_decisions(run)
    body["decisions"].append(body["decisions"][0])
    assert client.post(url, json=body).status_code == 422
    res = client.post(url, json=all_decisions(run, "ask_vendor"))  # option of another agent
    assert res.status_code == 422 and res.json()["detail"]["invalid"] == ["ask_vendor"]
    assert client.get(f"/api/runs/{run['run_id']}").json()["status"] == "awaiting_review"


def test_cannot_review_twice(client):
    run = demo(client)
    url = f"/api/runs/{run['run_id']}/decisions"
    assert client.post(url, json=all_decisions(run)).status_code == 200
    assert client.post(url, json=all_decisions(run)).status_code == 409


def test_upload_is_routed_by_coordinator(client, tmp_path):
    folder = AGENTS["data_entry"].demo(tmp_path / "src")
    files = [("files", (p.name, p.read_bytes())) for p in sorted(folder.iterdir())]
    run = client.post("/api/runs", files=files, data={"request": "", "label": "tagihan"}).json()
    assert run["agent"] == "data_entry" and run["status"] == "awaiting_review"


def test_upload_with_agent_override(client, tmp_path):
    folder = AGENTS["email"].demo(tmp_path / "src")
    files = [("files", (p.name, p.read_bytes())) for p in sorted(folder.iterdir())]
    run = client.post("/api/runs", files=files, data={"agent": "email", "request": "apa saja"}).json()
    assert run["agent"] == "email" and "dipilih" in run["route_reason"].lower()


def test_upload_rejects_unsupported_type(client):
    assert client.post("/api/runs", files=[("files", ("manifest.exe", b"MZ"))]).status_code == 415


def test_upload_without_manifest_is_422(client):
    res = client.post("/api/runs", files=[("files", ("container_list.csv", b"CNTR NO\nCSQU3054383\n"))],
                      data={"request": "cek manifest dan container list"})
    assert res.status_code == 422


def test_ask_routes_without_running_anything(client):
    run = client.post("/api/ask", data={"request": "buatkan quotation rate"}).json()
    assert run["status"] == "answered" and run["route"] == "pricing"


def test_unknown_run_and_file_are_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    run = demo(client)
    assert client.get(f"/api/runs/{run['run_id']}/files/../../etc/passwd").status_code == 404


def test_run_list_is_newest_first(client):
    first, second = demo(client), demo(client, "pricing")
    runs = client.get("/api/runs").json()
    assert [r["run_id"] for r in runs[:2]] == [second["run_id"], first["run_id"]]
    assert runs[0]["agent"] == "pricing"


class _FakeSmtp:
    """Stands in for smtplib.SMTP so /send can be exercised without a real network."""
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        _FakeSmtp.sent.append(msg)


@pytest.fixture()
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "ops@yourline.example")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setattr("shipping_agent.mailer.smtplib.SMTP", _FakeSmtp)
    _FakeSmtp.sent = []
    return _FakeSmtp


def test_send_requires_smtp_config(client, monkeypatch):
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    run = demo(client, "email")
    client.post(f"/api/runs/{run['run_id']}/decisions", json=all_decisions(run))
    assert client.post(f"/api/runs/{run['run_id']}/send").status_code == 422


def test_send_requires_email_agent(client, smtp_env):
    run = demo(client, "reconciliation")
    client.post(f"/api/runs/{run['run_id']}/decisions", json=all_decisions(run))
    assert client.post(f"/api/runs/{run['run_id']}/send").status_code == 409


def test_send_approved_drafts_and_is_idempotent(client, smtp_env):
    run = demo(client, "email")
    done = client.post(f"/api/runs/{run['run_id']}/decisions", json=all_decisions(run)).json()
    assert any(n.startswith("draft_email_") and n.endswith(".zip") for n in done["files"])

    first = client.post(f"/api/runs/{run['run_id']}/send").json()
    assert first["sent"] and not first["failed"]
    assert len(smtp_env.sent) == len(first["sent"])

    second = client.post(f"/api/runs/{run['run_id']}/send").json()
    assert second["sent"] == [] and set(second["skipped"]) == set(first["sent"])
    assert len(smtp_env.sent) == len(first["sent"])  # nothing re-sent

    view = client.get(f"/api/runs/{run['run_id']}").json()
    assert set(view["sent"]) == set(first["sent"])


class _FakeImap:
    """Stands in for imaplib.IMAP4_SSL so /fetch-inbox can be exercised without a real network."""
    messages: dict = {}

    def __init__(self, host, port):
        pass

    def login(self, user, password):
        return "OK", [b"LOGIN completed"]

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(sorted(_FakeImap.messages))]

    def fetch(self, num, parts):
        raw = _FakeImap.messages.get(num)
        if raw is None:
            return "NO", [None]
        return "OK", [(b"%s (RFC822 {%d}" % (num, len(raw)), raw)]

    def logout(self):
        return "BYE", [b"logout"]


def _eml_bytes(message_id, subject):
    msg = EmailMessage()
    msg["Message-ID"], msg["Subject"] = message_id, subject
    msg["From"], msg["To"] = "customer@example.com", "ops@yourline.example"
    msg.set_content("Mohon info jadwal kapal.")
    return bytes(msg)


@pytest.fixture()
def imap_env(monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "ops@yourline.example")
    monkeypatch.setenv("IMAP_PASSWORD", "secret")
    monkeypatch.setattr("shipping_agent.imap_client.imaplib.IMAP4_SSL", _FakeImap)
    _FakeImap.messages = {
        b"1": _eml_bytes("<a@example.com>", "Jadwal kapal minggu ini"),
        b"2": _eml_bytes("<b@example.com>", "Request booking 2x20GP"),
    }
    return _FakeImap


def test_fetch_inbox_requires_imap_config(client, monkeypatch):
    for var in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    assert client.post("/api/runs/fetch-inbox", json={}).status_code == 422


def test_fetch_inbox_creates_email_run_and_dedupes(client, imap_env):
    run = client.post("/api/runs/fetch-inbox", json={}).json()
    assert run["agent"] == "email" and run["status"] == "awaiting_review"
    assert len(run["items"]) >= 1

    again = client.post("/api/runs/fetch-inbox", json={})
    assert again.status_code == 422  # same two messages, already fetched above
