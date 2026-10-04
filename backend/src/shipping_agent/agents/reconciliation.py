"""Agent Rekonsiliasi: manifest vs container list vs proforma loading vs stacking list."""

from __future__ import annotations

from pathlib import Path

from ..config import DEFAULT_CONFIG, ReconConfig
from ..loader import load_customer_master, load_voyage_folder
from ..rules import reconcile
from ..sample_data import generate
from .base import (
    Agent,
    AgentResult,
    DecisionOption,
    Kpi,
    ReviewDecision,
    ReviewItem,
    find_file,
    rank,
    severity_counts,
    stamp,
)

DOC_LABEL = {"manifest": "Manifest", "container_list": "Container list",
             "proforma_loading": "Proforma loading", "stacking_list": "Stacking list"}


def _group(rule: str) -> str:
    if rule.startswith("missing_in_"):
        return "Kontainer tidak lengkap"
    if rule in {"lookalike_container", "invalid_check_digit", "duplicate_in_document"}:
        return "Nomor kontainer salah"
    if rule.startswith("npwp_"):
        return "NPWP"
    return "Data muatan berbeda"


class ReconciliationAgent(Agent):
    name = "reconciliation"
    label = "Agent Rekonsiliasi"
    description = "Mencocokkan manifest, container list, proforma loading, dan stacking list per voyage."
    keywords = ("cocok", "rekonsiliasi", "reconcile", "selisih", "cek manifest", "manifest", "container list",
                "proforma", "npwp", "verifikasi", "periksa data", "mismatch", "stacking list")
    file_hints = ("manifest", "container_list", "proforma", "stacking_list")
    demo_request = "Tolong cek manifest voyage GA123, cocokkan dengan container list, proforma dan stacking list"
    decision_options = [
        DecisionOption(key="approve", label="Setujui", tone="approve"),
        DecisionOption(key="reject", label="Tolak", tone="reject"),
        DecisionOption(key="ask_pol", label="Tanya POL", tone="ask"),
    ]

    def __init__(self, config: ReconConfig = DEFAULT_CONFIG) -> None:
        self.config = config

    def run(self, folder: Path) -> AgentResult:
        docs = load_voyage_folder(folder)
        master = find_file(folder, "master", "customer", "pelanggan")
        findings = reconcile(docs, load_customer_master(master), self.config)
        items = rank([
            ReviewItem(subject=f.container_no, group=_group(f.rule), kind=f.rule, severity=f.severity,
                       title=f.message, sources=f.sources, expected=f.expected, actual=f.actual,
                       suggestion=f.suggestion)
            for f in findings
        ])
        all_ids = set().union(*(set(d.frame["container_no"]) for d in docs.values()))
        notes = [f"{DOC_LABEL[n]}: kolom tidak dipakai {d.unmapped_columns}" for n, d in docs.items() if d.unmapped_columns]
        notes += [f"Dokumen tidak ditemukan: {DOC_LABEL[m]} (dilewati)" for m in DOC_LABEL if m not in docs]
        if master is None:
            notes.append("Master pelanggan tidak ada: cek NPWP terdaftar dilewati")
        high = [i for i in items if i.severity.value == "HIGH"][:3]
        facts = (f"Voyage {folder.name}: {len(all_ids)} kontainer diperiksa di {len(docs)} dokumen. "
                 f"Ditemukan {len(items)} temuan ({severity_counts(items)}).")
        if high:
            facts += "\nPrioritas sebelum closing: " + "; ".join(f"{i.subject} ({i.title})" for i in high) + "."
        return AgentResult(
            agent=self.name, title=f"Voyage {folder.name}", subject_label="Kontainer",
            kpis=[
                Kpi(label="Kontainer diperiksa", value=str(len(all_ids)), detail="gabungan semua dokumen"),
                Kpi(label="Dokumen dibaca", value=str(len(docs)),
                    detail=" · ".join(f"{DOC_LABEL[k]} {len(d.frame)}" for k, d in docs.items())),
                Kpi(label="Temuan", value=str(len(items)), detail=severity_counts(items)),
            ],
            items=items, facts=facts, notes=notes, context={"voyage": folder.name},
        )

    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        voyage, ts = result.context["voyage"], stamp()
        outputs = {"report": str(self.write_review_excel(out_dir / f"rekonsiliasi_{voyage}_{ts}.xlsx", result, decisions))}
        ask = [i for i in result.items if decisions.get(i.id) and decisions[i.id].decision == "ask_pol"]
        if ask:
            path = out_dir / f"draft_email_POL_{voyage}_{ts}.txt"
            path.write_text("\n".join([
                f"Subject: Konfirmasi data voyage {voyage} sebelum closing", "", "Yth. Tim POL,", "",
                f"Mohon konfirmasi data berikut untuk voyage {voyage}:", "",
                *(f"- {i.subject}: {i.title}" + (f" (catatan: {decisions[i.id].note})" if decisions[i.id].note else "")
                  for i in ask),
                "", "Terima kasih.", "", "[DRAFT dibuat oleh agent - kirim setelah dicek staf]",
            ]), encoding="utf-8")
            outputs["draft_email"] = str(path)
        return outputs

    def demo(self, base: Path) -> Path:
        return generate(base)
