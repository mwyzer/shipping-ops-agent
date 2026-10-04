"""Shared contract for every specialist agent.

An agent turns a folder of input files into `ReviewItem`s. A person decides on
each item, then the agent's `finalize` writes outputs from the decisions only.
The graph, API and dashboard work with this contract, so they need no changes
when an agent is added.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, Field

from ..schemas import Severity

SEV_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


class ReviewItem(BaseModel):
    id: str = ""
    subject: str = Field(description="What the item is about: container, invoice no, email, quotation...")
    group: str = Field(description="Human label used to group items in the dashboard chart")
    kind: str = Field(description="Machine name of the rule or item type")
    severity: Severity
    title: str
    detail: str = ""
    sources: list[str] = Field(default_factory=list)
    expected: str | None = None
    actual: str | None = None
    suggestion: str
    draft: str | None = Field(default=None, description="Draft text (email, quotation) shown to the reviewer")
    data: dict[str, Any] = Field(default_factory=dict, description="Agent payload used by finalize")


class Kpi(BaseModel):
    label: str
    value: str
    detail: str = ""


class DecisionOption(BaseModel):
    key: str
    label: str
    tone: str = Field(description="approve | reject | ask: colour hint for the dashboard")


class AgentResult(BaseModel):
    agent: str
    title: str
    subject_label: str = "Subjek"
    kpis: list[Kpi]
    items: list[ReviewItem]
    facts: str = Field(description="Template briefing built from computed numbers")
    notes: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class ReviewDecision(BaseModel):
    finding_id: str
    decision: str
    reviewer: str
    note: str = ""


def rank(items: list[ReviewItem]) -> list[ReviewItem]:
    ordered = sorted(items, key=lambda i: (SEV_ORDER[i.severity], i.group, i.subject, i.kind))
    return [i.model_copy(update={"id": f"F{n + 1:03d}"}) for n, i in enumerate(ordered)]


def severity_counts(items: list[ReviewItem]) -> str:
    c = {s: sum(1 for i in items if i.severity == s) for s in Severity}
    return f"{c[Severity.HIGH]} tinggi · {c[Severity.MEDIUM]} sedang · {c[Severity.LOW]} rendah"


def safe_filename(text: str, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:limit] or "item"


class Agent(ABC):
    name: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    keywords: ClassVar[tuple[str, ...]]
    file_hints: ClassVar[tuple[str, ...]] = ()
    demo_request: ClassVar[str]
    decision_options: ClassVar[list[DecisionOption]] = [
        DecisionOption(key="approve", label="Setujui", tone="approve"),
        DecisionOption(key="reject", label="Tolak", tone="reject"),
    ]

    @abstractmethod
    def run(self, folder: Path) -> AgentResult: ...

    @abstractmethod
    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        """Write outputs. Only items with an explicit decision may affect them."""

    @abstractmethod
    def demo(self, base: Path) -> Path:
        """Create a fictional sample folder with known issues; return the folder."""

    # ------------------------------------------------------------- helpers ---
    def option_keys(self) -> set[str]:
        return {o.key for o in self.decision_options}

    def option_label(self, key: str) -> str:
        return next((o.label for o in self.decision_options if o.key == key), key)

    def write_review_excel(self, path: Path, result: AgentResult, decisions: dict[str, ReviewDecision],
                           extra_sheets: dict[str, pd.DataFrame] | None = None) -> Path:
        rows = []
        for item in result.items:
            d = decisions.get(item.id)
            rows.append({
                "ID": item.id, result.subject_label: item.subject, "Urgensi": item.severity.value,
                "Kelompok": item.group, "Masalah": item.title, "Detail": item.detail,
                "Sumber": ", ".join(item.sources), "Seharusnya": item.expected, "Tertulis": item.actual,
                "Usulan agent": item.suggestion,
                "Keputusan": self.option_label(d.decision) if d else "Belum diputuskan",
                "Reviewer": d.reviewer if d else "", "Catatan": d.note if d else "",
            })
        summary = pd.DataFrame(
            [{"Item": k.label, "Nilai": k.value, "Keterangan": k.detail} for k in result.kpis]
            + [{"Item": "Ringkasan", "Nilai": result.facts, "Keterangan": ""}]
        )
        with pd.ExcelWriter(path, engine="openpyxl") as xw:
            summary.to_excel(xw, sheet_name="Ringkasan", index=False)
            pd.DataFrame(rows).to_excel(xw, sheet_name="Review", index=False)
            for name, frame in (extra_sheets or {}).items():
                frame.to_excel(xw, sheet_name=name[:31], index=False)
            for ws in xw.book.worksheets:
                for col in ws.columns:
                    width = max(len(str(c.value or "")) for c in col)
                    ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
        return path


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def append_audit(out_dir: Path, agent: str, title: str, decisions: list[ReviewDecision]) -> Path:
    path = out_dir / "audit_log.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for d in decisions:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "agent": agent,
                                 "run": title, **d.model_dump()}, ensure_ascii=False) + "\n")
    return path


def read_any(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str)


def find_file(folder: Path, *hints: str, suffixes: tuple[str, ...] = (".csv", ".xlsx", ".xls")) -> Path | None:
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in suffixes and any(h in path.stem.lower() for h in hints):
            return path
    return None
