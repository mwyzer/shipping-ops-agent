"""Each specialist agent finds the issues injected into its demo data, and
finalize only acts on what the reviewer decided."""

import csv
import zipfile

import pytest

from shipping_agent.agents.base import ReviewDecision
from shipping_agent.agents.registry import AGENTS
from shipping_agent.parsing import parse_amount, parse_date, transposed

EXPECTED_KINDS = {
    "reconciliation": {"missing_in_manifest", "lookalike_container", "weight_mismatch", "pod_mismatch",
                       "seal_no_mismatch", "npwp_invalid_length", "npwp_not_registered", "duplicate_in_document",
                       "missing_in_proforma_loading"},
    "data_entry": {"missing_field", "duplicate_invoice", "bad_date", "vendor_unknown", "vendor_alias",
                   "lookalike_invoice", "payment_amount_mismatch", "payment_without_invoice", "ready"},
    "reporting": {"duplicate_booking", "bad_qty", "swapped_columns", "revenue_outlier", "missing_field", "report_draft"},
    "email": {"mail_complaint", "mail_hq", "mail_booking", "mail_rate", "mail_schedule", "mail_arrival",
              "mail_documentation", "mail_billing", "mail_newsletter", "noa_no_contact", "noa_address_changed", "noa_ready"},
    "pricing": {"quotation", "no_rate", "missing_variables"},
    "schedule": {"etd_change", "port_omitted", "overbooked", "near_full", "low_util"},
}


def decide(result, choose=lambda item: "approve"):
    return {i.id: ReviewDecision(finding_id=i.id, decision=choose(i), reviewer="tester") for i in result.items}


@pytest.fixture(params=list(AGENTS))
def agent_run(request, tmp_path):
    agent = AGENTS[request.param]
    result = agent.run(agent.demo(tmp_path / "data"))
    return agent, result, tmp_path / "out"


def test_demo_finds_every_injected_issue(agent_run):
    agent, result, _ = agent_run
    kinds = {i.kind for i in result.items}
    assert EXPECTED_KINDS[agent.name] <= kinds, EXPECTED_KINDS[agent.name] - kinds
    assert [i.id for i in result.items] == [f"F{n:03d}" for n in range(1, len(result.items) + 1)]
    assert result.kpis and result.facts


def test_finalize_writes_report_from_decisions(agent_run):
    agent, result, out = agent_run
    out.mkdir()
    outputs = agent.finalize(result, decide(result), out)
    assert outputs["report"].endswith(".xlsx")
    for path in outputs.values():
        assert (out / path.split("/")[-1]).exists()


def test_decision_options_are_unique(agent_run):
    agent, _, _ = agent_run
    keys = [o.key for o in agent.decision_options]
    assert keys[0] == "approve" and len(keys) == len(set(keys))


# --------------------------------------------------------- agent specifics ---
def test_data_entry_imports_only_approved_rows(tmp_path):
    agent = AGENTS["data_entry"]
    result = agent.run(agent.demo(tmp_path / "d"))
    rejected = next(i for i in result.items if i.kind == "ready")
    choose = lambda i: "reject" if i.id == rejected.id else "approve"  # noqa: E731
    outputs = agent.finalize(result, decide(result, choose), tmp_path)
    rows = list(csv.DictReader(open(outputs["import_csv"], encoding="utf-8")))
    numbers = [r["invoice_no"] for r in rows]
    assert rejected.subject not in numbers
    assert len(numbers) == len(set(numbers)), "duplicate invoice must not be imported twice"
    assert all(r["amount"] for r in rows), "rows without amount must be excluded"
    fuzzy = next(i for i in result.items if i.kind == "vendor_alias")
    assert any(r["vendor"] == fuzzy.expected for r in rows), "approved vendor fix must be applied"


def test_data_entry_ask_vendor_creates_draft_and_excludes(tmp_path):
    agent = AGENTS["data_entry"]
    result = agent.run(agent.demo(tmp_path / "d"))
    unknown = next(i for i in result.items if i.kind == "vendor_unknown")
    outputs = agent.finalize(result, decide(result, lambda i: "ask_vendor" if i.id == unknown.id else "approve"), tmp_path)
    assert unknown.subject not in open(outputs["import_csv"], encoding="utf-8").read()
    assert "Kepada:" in open(outputs["draft_email"], encoding="utf-8").read()


def test_reporting_publishes_only_when_draft_approved(tmp_path):
    agent = AGENTS["reporting"]
    result = agent.run(agent.demo(tmp_path / "d"))
    draft = next(i for i in result.items if i.kind == "report_draft")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    held = agent.finalize(result, decide(result, lambda i: "reject" if i.id == draft.id else "approve"), tmp_path / "a")
    assert "report_md" not in held
    published = agent.finalize(result, decide(result), tmp_path / "b")
    assert "TEU" in open(published["report_md"], encoding="utf-8").read()


def test_reporting_swapped_columns_fix_is_applied(tmp_path):
    agent = AGENTS["reporting"]
    result = agent.run(agent.demo(tmp_path / "d"))
    swapped = next(i for i in result.items if i.kind == "swapped_columns")
    import pandas as pd

    data = pd.DataFrame(result.context["rows"])
    rows = agent._rows(data, [i for i in result.items if i.kind != "report_draft"], decide(result))
    fixed = rows[rows["booking_no"] == swapped.subject].iloc[0]
    assert fixed["pod"] == swapped.data["fix"]["pod_n"] and fixed["container_type"] == swapped.data["fix"]["ctype"]


def test_email_drafts_are_eml_and_never_for_rejected(tmp_path):
    agent = AGENTS["email"]
    result = agent.run(agent.demo(tmp_path / "d"))
    complaint = next(i for i in result.items if i.kind == "mail_complaint")
    assert complaint.severity.value == "HIGH" and "TGHU3904879" in complaint.draft
    outputs = agent.finalize(result, decide(result, lambda i: "reject" if i.id == complaint.id else "approve"), tmp_path)
    names = zipfile.ZipFile(outputs["drafts_zip"]).namelist()
    assert names and all(n.endswith(".eml") for n in names)
    assert not any(n.startswith(complaint.id) for n in names)


def test_pricing_quote_adds_surcharges_and_flags_low_margin(tmp_path):
    agent = AGENTS["pricing"]
    result = agent.run(agent.demo(tmp_path / "d"))
    quotes = {i.subject: i for i in result.items}
    low = quotes["Q-1003"]
    assert low.severity.value == "HIGH" and "Margin" in low.detail
    q = low.data["quote"]
    assert q["all_in_usd"] == sum(line["usd"] for line in q["lines"])
    assert "PSS" in low.draft, "surcharge from the latest HQ email must be in the quotation"
    assert "Kontrak berakhir" in quotes["Q-1002"].detail


def test_schedule_closing_follows_etd(tmp_path):
    agent = AGENTS["schedule"]
    result = agent.run(agent.demo(tmp_path / "d"))
    earlier = next(i for i in result.items if i.kind == "etd_change" and i.group == "ETD maju")
    assert earlier.severity.value == "HIGH"
    over = next(i for i in result.items if i.kind == "overbooked")
    assert over.data["roll"], "overbooking must propose bookings to roll"
    outputs = agent.finalize(result, decide(result), tmp_path)
    assert "Closing CY" in open(outputs["notices"], encoding="utf-8").read()


# ------------------------------------------------------------------ parsing ---
@pytest.mark.parametrize("raw, value", [("Rp 12.500.000,00", 12_500_000), ("12,500,000.50", 12_500_000.5),
                                        ("4.250.000", 4_250_000), ("USD 1,250", 1250), ("dua", None), ("", None)])
def test_parse_amount(raw, value):
    assert parse_amount(raw) == value


@pytest.mark.parametrize("raw, ok", [("12/09/2026", True), ("2026-09-12", True), ("12 September 2026", True),
                                     ("5-Okt-2026", True), ("31/02/2026", False), ("kemarin", False)])
def test_parse_date(raw, ok):
    assert (parse_date(raw) is not None) == ok


def test_transposed_only_flags_swaps_not_sequences():
    assert transposed("INV0118", "INV0181")
    assert not transposed("INV0118", "INV0119")
