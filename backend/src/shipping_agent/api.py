"""FastAPI backend for the review dashboard (all agents).

The LangGraph thread id IS the run id. A run pauses at `human_review`
(interrupt); `POST /api/runs/{id}/decisions` resumes it with `Command(resume=...)`.
Run state is read back from the checkpointer, so the API keeps no copy of it.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from .agents.base import DecisionOption, Kpi, ReviewDecision, ReviewItem, append_sent_log, read_sent_ids
from .agents.registry import AGENTS
from .config import imap_config, smtp_config
from .graph import build_graph
from .imap_client import append_fetched_ids, fetch_new_messages, read_fetched_ids
from .mailer import send_eml

WEB_DIR = Path(__file__).parent / "web"
ALLOWED_SUFFIXES = {".xlsx", ".xls", ".csv", ".txt", ".eml"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FILES = 200


class DecisionIn(BaseModel):
    finding_id: str
    decision: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=500)


class DecisionsIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)
    decisions: list[DecisionIn]


class DemoIn(BaseModel):
    agent: str = "reconciliation"


class FetchInboxIn(BaseModel):
    label: str = "Inbox IMAP"


# ------------------------------------------------------------ responses ---
# Typed so /openapi.json describes them; the Next.js client is generated from it.
RunStatus = Literal["awaiting_review", "done", "answered", "error"]


class AgentInfo(BaseModel):
    name: str
    label: str
    description: str
    demo_request: str
    options: list[DecisionOption]


class RunListItem(BaseModel):
    run_id: str
    label: str
    created_at: str
    status: RunStatus
    agent: str | None
    agent_label: str | None
    items: int


class RunView(BaseModel):
    run_id: str
    label: str
    request: str
    created_at: str
    status: RunStatus
    route: str | None
    route_reason: str | None
    answer: str | None
    agent: str | None
    agent_label: str | None
    title: str | None
    subject_label: str
    kpis: list[Kpi]
    items: list[ReviewItem]
    notes: list[str]
    narrative: str | None
    options: list[DecisionOption]
    decisions: list[ReviewDecision]
    files: list[str]
    sent: list[str]
    log: list[str]


class SendResult(BaseModel):
    sent: list[str]
    failed: list[str]
    skipped: list[str]


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120] or "file"


def create_app(work_dir: Path | str = "output/web") -> FastAPI:
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    graph = build_graph()
    runs: dict[str, dict[str, Any]] = {}  # run index only; state lives in the checkpointer

    app = FastAPI(title="Shipping Ops Agent", version="0.4.0",
                  description="Koordinator + 6 agent spesialis dengan review staf")
    origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _view(run_id: str) -> dict[str, Any]:
        if run_id not in runs:
            raise HTTPException(404, "Run tidak ditemukan")
        snap = graph.get_state(_config(run_id))
        v = snap.values or {}
        interrupts = [i for t in snap.tasks for i in t.interrupts]
        result = v.get("result") or {}
        agent = AGENTS.get(result.get("agent") or v.get("route") or "")
        status = ("awaiting_review" if interrupts else "done" if v.get("outputs")
                  else "answered" if v.get("answer") else "error")
        sent = sorted(read_sent_ids(Path(v["output_dir"]))) if status == "done" and v.get("output_dir") else []
        return {
            "run_id": run_id, **runs[run_id], "status": status,
            "route": v.get("route"), "route_reason": v.get("route_reason"), "answer": v.get("answer"),
            "agent": agent.name if agent else None, "agent_label": agent.label if agent else None,
            "title": result.get("title"), "subject_label": result.get("subject_label", "Subjek"),
            "kpis": result.get("kpis", []), "items": result.get("items", []), "notes": result.get("notes", []),
            "narrative": v.get("narrative"),
            "options": [o.model_dump() for o in agent.decision_options] if agent else [],
            "decisions": v.get("decisions", []),
            "files": sorted(Path(p).name for p in (v.get("outputs") or {}).values()),
            "sent": sent,
            "log": v.get("log", []),
        }

    def _start(run_id: str, request: str, folder: Path | None, label: str, agent: str | None = None) -> dict:
        runs[run_id] = {"label": label, "request": request, "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            graph.invoke({"request": request, "folder": str(folder) if folder else "",
                          "output_dir": str(work / run_id / "output"), "agent_override": agent, "log": []},
                         _config(run_id))
        except (FileNotFoundError, ValueError, KeyError) as exc:
            runs.pop(run_id, None)
            raise HTTPException(422, f"Agent tidak bisa membaca file: {exc}") from exc
        return _view(run_id)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/agents", response_model=list[AgentInfo])
    def agents() -> list[dict]:
        return [{"name": a.name, "label": a.label, "description": a.description, "demo_request": a.demo_request,
                 "options": [o.model_dump() for o in a.decision_options]} for a in AGENTS.values()]

    @app.get("/api/runs", response_model=list[RunListItem])
    def list_runs() -> list[dict]:
        out = []
        for run_id in reversed(list(runs)):
            v = _view(run_id)
            out.append({k: v[k] for k in ("run_id", "label", "created_at", "status", "agent", "agent_label")}
                       | {"items": len(v["items"])})
        return out

    @app.post("/api/runs/demo", response_model=RunView)
    def start_demo(body: DemoIn) -> dict:
        if body.agent not in AGENTS:
            raise HTTPException(404, f"Agent tidak dikenal: {body.agent}")
        agent = AGENTS[body.agent]
        run_id = uuid.uuid4().hex[:12]
        folder = agent.demo(work / run_id / "data")
        return _start(run_id, agent.demo_request, folder, f"Demo · {agent.label}")

    @app.post("/api/runs", response_model=RunView)
    async def start_upload(
        files: list[UploadFile] = File(..., description="Input files (.xlsx/.xls/.csv/.txt/.eml)"),
        request: str = Form("", description="What should be done; the coordinator picks the agent"),
        agent: str = Form("", description="Optional: skip the coordinator and use this agent"),
        label: str = Form("Upload"),
    ) -> dict:
        if agent and agent not in AGENTS:
            raise HTTPException(404, f"Agent tidak dikenal: {agent}")
        if len(files) > MAX_FILES:
            raise HTTPException(413, f"Maksimal {MAX_FILES} file per run")
        run_id = uuid.uuid4().hex[:12]
        folder = work / run_id / "data" / _safe_name(label)
        folder.mkdir(parents=True, exist_ok=True)
        for upload in files:
            name = _safe_name(upload.filename or "")
            if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                raise HTTPException(415, f"Format tidak didukung: {name}")
            content = await upload.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"File terlalu besar: {name}")
            (folder / name).write_bytes(content)
        return _start(run_id, request, folder, _safe_name(label), agent or None)

    @app.post("/api/runs/fetch-inbox", response_model=RunView)
    def fetch_inbox(body: FetchInboxIn = FetchInboxIn()) -> dict:
        cfg = imap_config()
        if cfg is None:
            raise HTTPException(422, "IMAP belum dikonfigurasi di server")
        try:
            messages = fetch_new_messages(cfg, read_fetched_ids(work))
        except Exception as exc:
            raise HTTPException(502, f"Gagal mengambil inbox: {exc}") from exc
        if not messages:
            raise HTTPException(422, "Tidak ada email baru di inbox")
        run_id = uuid.uuid4().hex[:12]
        folder = work / run_id / "data" / "inbox"
        folder.mkdir(parents=True, exist_ok=True)
        for i, (_, raw) in enumerate(messages, start=1):
            (folder / f"mail_{i:03d}.eml").write_bytes(raw)
        view = _start(run_id, f"{len(messages)} email baru dari inbox", folder, _safe_name(body.label), "email")
        append_fetched_ids(work, [message_id for message_id, _ in messages])
        return view

    @app.post("/api/ask", response_model=RunView)
    def ask(request: str = Form(...)) -> dict:
        """Coordinator only, no files: shows which agent would handle the request."""
        return _start(uuid.uuid4().hex[:12], request, None, "Permintaan teks")

    @app.get("/api/runs/{run_id}", response_model=RunView)
    def get_run(run_id: str) -> dict:
        return _view(run_id)

    @app.post("/api/runs/{run_id}/decisions", response_model=RunView)
    def submit_decisions(run_id: str, body: DecisionsIn) -> dict:
        view = _view(run_id)
        if view["status"] != "awaiting_review":
            raise HTTPException(409, "Run ini tidak sedang menunggu review")
        allowed = {o["key"] for o in view["options"]}
        bad = sorted({d.decision for d in body.decisions} - allowed)
        if bad:
            raise HTTPException(422, {"message": "Keputusan tidak dikenal", "invalid": bad, "allowed": sorted(allowed)})
        expected = {i["id"] for i in view["items"]}
        given = [d.finding_id for d in body.decisions]
        if len(given) != len(set(given)):
            raise HTTPException(422, "Ada item yang diputuskan dua kali")
        if set(given) != expected:
            raise HTTPException(422, {"message": "Semua item harus diputuskan",
                                      "missing": sorted(expected - set(given)), "unknown": sorted(set(given) - expected)})
        graph.invoke(Command(resume=[{"finding_id": d.finding_id, "decision": d.decision,
                                      "reviewer": body.reviewer.strip(), "note": d.note} for d in body.decisions]),
                     _config(run_id))
        return _view(run_id)

    @app.post("/api/runs/{run_id}/send", response_model=SendResult)
    def send_emails(run_id: str) -> dict:
        view = _view(run_id)
        if view["status"] != "done":
            raise HTTPException(409, "Run belum selesai direview")
        if view["agent"] != "email":
            raise HTTPException(409, "Pengiriman hanya tersedia untuk Agent Email")
        cfg = smtp_config()
        if cfg is None:
            raise HTTPException(422, "SMTP belum dikonfigurasi di server")
        values = graph.get_state(_config(run_id)).values or {}
        drafts_dir = (values.get("outputs") or {}).get("drafts_dir")
        if not drafts_dir or not Path(drafts_dir).is_dir():
            raise HTTPException(422, "Tidak ada draft untuk dikirim")
        out_dir = Path(values["output_dir"])
        already = read_sent_ids(out_dir)
        sent, failed, skipped, log_entries = [], [], [], []
        for path in sorted(Path(drafts_dir).glob("*.eml")):
            item_id = path.stem.split("_", 1)[0]
            if item_id in already:
                skipped.append(item_id)
                continue
            try:
                send_eml(path, cfg)
                sent.append(item_id)
                log_entries.append({"item_id": item_id, "status": "sent"})
            except Exception as exc:
                failed.append(item_id)
                log_entries.append({"item_id": item_id, "status": "failed", "error": str(exc)})
        if log_entries:
            append_sent_log(out_dir, log_entries)
        return {"sent": sent, "failed": failed, "skipped": skipped}

    @app.get("/api/runs/{run_id}/files/{name}")
    def download(run_id: str, name: str) -> FileResponse:
        values = graph.get_state(_config(run_id)).values if run_id in runs else {}
        for path in (values.get("outputs") or {}).values():
            if Path(path).name == name and Path(path).is_file():
                return FileResponse(path, filename=name)
        raise HTTPException(404, "File tidak ditemukan")

    return app
