"""Agent Tarif: draft quotations from contract rates, public tariff and HQ surcharges.

Every number in a quotation comes from a table the company maintains; the agent
only picks the right row, adds the surcharges valid on the shipment date and
checks margin and competitor position. Final price is always a person's call.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..parsing import blank, map_columns, norm_company, parse_amount, parse_date
from ..schemas import Severity
from .base import Agent, AgentResult, DecisionOption, Kpi, ReviewDecision, ReviewItem, find_file, rank, read_any, stamp

MARGIN_FLOOR_PCT = 8.0
COMPETITOR_GAP_PCT = 10.0
COMPETITOR_MAX_AGE_DAYS = 30
RECENT_SURCHARGE_DAYS = 14

LANE = {"pol": ["pol", "origin", "asal"], "pod": ["pod", "destination", "tujuan"],
        "container_type": ["containertype", "type", "tipe", "size"]}
TABLES = {
    "requests": ({"request_id": ["requestid", "id", "noreq"], "customer": ["customer", "pelanggan", "shipper"],
                  "qty": ["qty", "jumlah", "box"], "ship_date": ["shipdate", "etd", "tanggalmuat", "cargoready"], **LANE},
                 ("request", "permintaan", "inquiry")),
    "contracts": ({"customer": ["customer", "pelanggan"], "rate_usd": ["rateusd", "rate", "oceanfreight"],
                   "valid_from": ["validfrom", "berlakudari"], "valid_to": ["validto", "berlakusampai"], **LANE},
                  ("contract", "kontrak")),
    "tariff": ({"rate_usd": ["rateusd", "rate", "oceanfreight"], **LANE}, ("tariff", "tarif_umum", "public")),
    "surcharges": ({"code": ["code", "kode", "surcharge"], "amount_usd": ["amountusd", "amount", "nominal"],
                    "effective_from": ["effectivefrom", "berlaku", "efektif"], "source": ["source", "sumber"],
                    "pod": ["pod", "destination"], "container_type": ["containertype", "type"]},
                   ("surcharge",)),
    "costs": ({"cost_usd": ["costusd", "cost", "biaya"], **LANE}, ("cost", "biaya")),
    "competitors": ({"carrier": ["carrier", "pelayaran", "kompetitor"], "rate_usd": ["rateusd", "allin", "rate"],
                     "observed_at": ["observedat", "tanggal", "date"], "source": ["source", "sumber"], **LANE},
                    ("competitor", "kompetitor")),
}


def _load(folder: Path) -> tuple[dict[str, pd.DataFrame], list[str]]:
    out, notes = {}, []
    for name, (aliases, hints) in TABLES.items():
        path = find_file(folder, *hints)
        if path is None:
            out[name] = pd.DataFrame(columns=list(aliases))
            if name != "requests":
                notes.append(f"Tabel {name} tidak ada (dilewati)")
            continue
        frame, _ = map_columns(read_any(path), aliases)
        for col in aliases:
            if col not in frame:
                frame[col] = None
        for col in ("pol", "pod", "container_type"):
            if col in frame:
                frame[col] = frame[col].map(lambda v: "ALL" if blank(v) else str(v).strip().upper())
        out[name] = frame
    return out, notes


def _match(frame: pd.DataFrame, pol: str, pod: str, ctype: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[(frame["pol"] == pol) & (frame["pod"] == pod) & (frame["container_type"] == ctype)]


class PricingAgent(Agent):
    name = "pricing"
    label = "Agent Tarif"
    description = "Menyusun draft quotation dari tarif kontrak, tarif umum dan surcharge HQ, lalu mengecek margin dan kompetitor."
    keywords = ("tarif", "rate", "quotation", "harga", "penawaran", "surcharge", "kompetitor", "competitor", "quote")
    file_hints = ("request", "contract", "kontrak", "tariff", "surcharge", "competitor")
    demo_request = "Siapkan quotation untuk permintaan rate pelanggan minggu ini"
    decision_options = [
        DecisionOption(key="approve", label="Setujui harga", tone="approve"),
        DecisionOption(key="reject", label="Tolak", tone="reject"),
        DecisionOption(key="ask_hq", label="Tanya atasan/HQ", tone="ask"),
    ]

    def run(self, folder: Path) -> AgentResult:
        t, notes = _load(folder)
        if t["requests"].empty:
            raise FileNotFoundError("Tidak ada tabel permintaan quotation (nama file mengandung 'request' atau 'permintaan')")
        today = date.today()
        items: list[ReviewItem] = []
        quotes = []
        for _, req in t["requests"].iterrows():
            rid = str(req["request_id"]) if not blank(req["request_id"]) else f"REQ{len(quotes) + 1}"
            pol, pod, ctype = req["pol"], req["pod"], req["container_type"]
            qty = parse_amount(req["qty"])
            ship = parse_date(req["ship_date"])
            issues: list[tuple[Severity, str]] = []
            missing = [label for label, ok in (("customer", not blank(req["customer"])), ("POL", pol != "ALL"),
                                               ("POD", pod != "ALL"), ("tipe kontainer", ctype != "ALL"),
                                               ("qty", qty is not None), ("tanggal muat", ship is not None)) if not ok]
            if missing:
                items.append(ReviewItem(
                    subject=rid, group="Variabel belum lengkap", kind="missing_variables", severity=Severity.HIGH,
                    title=f"{req['customer'] or '-'}: {', '.join(missing)} belum diisi",
                    suggestion="Lengkapi permintaan sebelum dihitung", data={"request_id": rid}))
                continue

            source, base = None, None
            contracts = _match(t["contracts"], pol, pod, ctype)
            contracts = contracts[contracts["customer"].map(norm_company) == norm_company(req["customer"])] if len(contracts) else contracts
            valid = expired = None
            for _, c in contracts.iterrows():
                vf, vt = parse_date(c["valid_from"]), parse_date(c["valid_to"])
                if vf and vt and vf <= ship <= vt:
                    valid = c
                elif vt and vt < ship:
                    expired = c
            if valid is not None:
                source, base = f"Kontrak s/d {parse_date(valid['valid_to']):%d/%m/%Y}", parse_amount(valid["rate_usd"])
            else:
                tariff = _match(t["tariff"], pol, pod, ctype)
                if len(tariff):
                    source, base = "Tarif umum", parse_amount(tariff.iloc[0]["rate_usd"])
                if expired is not None:
                    issues.append((Severity.MEDIUM, f"Kontrak berakhir {parse_date(expired['valid_to']):%d/%m/%Y}; "
                                                    f"tarif kontrak lama USD {parse_amount(expired['rate_usd']):,.0f} tidak dipakai"))
            if base is None:
                items.append(ReviewItem(
                    subject=rid, group="Tarif tidak ada", kind="no_rate", severity=Severity.HIGH,
                    title=f"{req['customer']}: tidak ada tarif {pol}-{pod} {ctype}",
                    suggestion="Minta tarif ke HQ/pricing", data={"request_id": rid}))
                continue

            lines = [("Ocean freight", base, source)]
            for _, sc in t["surcharges"].iterrows():
                eff = parse_date(sc["effective_from"])
                if sc["pod"] in {"ALL", pod} and sc["container_type"] in {"ALL", ctype} and eff and eff <= ship:
                    amt = parse_amount(sc["amount_usd"]) or 0
                    lines.append((str(sc["code"]), amt, f"berlaku {eff:%d/%m/%Y}" + (f", {sc['source']}" if not blank(sc["source"]) else "")))
                    if (today - eff).days <= RECENT_SURCHARGE_DAYS or eff > today:
                        issues.append((Severity.LOW, f"{sc['code']} USD {amt:,.0f} baru berlaku {eff:%d/%m/%Y} "
                                                     f"({sc['source'] if not blank(sc['source']) else 'update HQ'}); sudah dihitung"))
            all_in = sum(a for _, a, _ in lines)

            cost = _match(t["costs"], pol, pod, ctype)
            margin_pct = None
            if len(cost):
                c = parse_amount(cost.iloc[0]["cost_usd"])
                margin_pct = (all_in - c) / all_in * 100 if all_in else None
                if margin_pct is not None and margin_pct < MARGIN_FLOOR_PCT:
                    issues.append((Severity.HIGH, f"Margin {margin_pct:.1f}% di bawah batas {MARGIN_FLOOR_PCT:.0f}% (biaya USD {c:,.0f})"))
            else:
                issues.append((Severity.MEDIUM, "Biaya rute tidak ada: margin tidak bisa dicek"))

            comp = _match(t["competitors"], pol, pod, ctype)
            comp_note = "tidak ada data"
            if len(comp):
                comp = comp.assign(obs=comp["observed_at"].map(parse_date), r=comp["rate_usd"].map(parse_amount))
                fresh = comp[comp["obs"].map(lambda d: d is not None and (today - d).days <= COMPETITOR_MAX_AGE_DAYS)]
                if len(fresh):
                    best = fresh.loc[fresh["r"].idxmin()]
                    gap = (all_in - best["r"]) / best["r"] * 100
                    comp_note = f"{best['carrier']} USD {best['r']:,.0f} ({gap:+.0f}%)"
                    if gap > COMPETITOR_GAP_PCT:
                        issues.append((Severity.MEDIUM, f"{gap:.0f}% lebih mahal dari kompetitor termurah ({best['carrier']} USD {best['r']:,.0f}, "
                                                        f"dicatat {best['obs']:%d/%m})"))
                else:
                    issues.append((Severity.LOW, f"Data kompetitor lebih dari {COMPETITOR_MAX_AGE_DAYS} hari; perbarui"))

            sev = min((s for s, _ in issues), key=lambda s: ["HIGH", "MEDIUM", "LOW"].index(s.value), default=Severity.LOW)
            total = all_in * qty
            breakdown = "\n".join(f"  {name:<14} USD {amt:>8,.0f}   ({note})" for name, amt, note in lines)
            draft = (f"QUOTATION {rid}\nKepada : {req['customer']}\nRute   : {pol} - {pod}\nTipe   : {qty:.0f} x {ctype}\n"
                     f"Muat   : {ship:%d/%m/%Y}\n\nPer kontainer:\n{breakdown}\n  {'All-in':<14} USD {all_in:>8,.0f}\n\n"
                     f"Total {qty:.0f} x {ctype}: USD {total:,.0f}\nBerlaku untuk muat s/d {ship + timedelta(days=14):%d/%m/%Y}. "
                     "Harga belum termasuk biaya lokal tujuan.\n\n[DRAFT - harga final ditetapkan staf]")
            quote = {"request_id": rid, "customer": req["customer"], "lane": f"{pol}-{pod}", "container_type": ctype,
                     "qty": qty, "ship_date": ship.isoformat(), "rate_source": source, "all_in_usd": all_in,
                     "total_usd": total, "margin_pct": round(margin_pct, 1) if margin_pct is not None else None,
                     "competitor": comp_note, "lines": [{"item": n, "usd": a, "note": no} for n, a, no in lines]}
            quotes.append(quote)
            group = ("Margin rendah" if any("Margin" in m for s, m in issues if s == Severity.HIGH)
                     else "Lebih mahal dari kompetitor" if any("kompetitor termurah" in m for _, m in issues)
                     else "Kontrak kedaluwarsa" if any("Kontrak berakhir" in m for _, m in issues)
                     else "Siap dikirim")
            items.append(ReviewItem(
                subject=rid, group=group, kind="quotation", severity=sev,
                title=f"{req['customer']} · {qty:.0f}x{ctype} {pol}-{pod} · USD {all_in:,.0f}/box",
                detail=" | ".join(m for _, m in issues) or f"Sumber: {source}; kompetitor: {comp_note}",
                sources=[source], expected=None, actual=f"margin {margin_pct:.1f}%" if margin_pct is not None else None,
                suggestion="Kirim quotation" if sev == Severity.LOW else "Tinjau harga sebelum dikirim",
                draft=draft, data={"quote": quote}))

        items = rank(items)
        ready = [q for q in quotes]
        avg_margin = [q["margin_pct"] for q in quotes if q["margin_pct"] is not None]
        facts = (f"{len(t['requests'])} permintaan diproses: {len(quotes)} draft quotation, "
                 f"{len(items) - len(quotes)} tidak bisa dihitung. "
                 f"{sum(1 for i in items if i.severity == Severity.HIGH)} butuh keputusan harga.")
        return AgentResult(
            agent=self.name, title=f"Quotation {date.today():%d/%m/%Y}", subject_label="Request",
            kpis=[
                Kpi(label="Permintaan", value=str(len(t["requests"])), detail="baris di tabel request"),
                Kpi(label="Draft quotation", value=str(len(ready)), detail=f"total USD {sum(q['total_usd'] for q in quotes):,.0f}"),
                Kpi(label="Rata-rata margin", value=f"{sum(avg_margin) / len(avg_margin):.1f}%" if avg_margin else "-",
                    detail=f"batas minimum {MARGIN_FLOOR_PCT:.0f}%"),
                Kpi(label="Surcharge aktif", value=str(len(t["surcharges"])), detail="dari tabel update HQ"),
            ],
            items=items, facts=facts, notes=notes, context={},
        )

    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        ts = stamp()
        approved = [i for i in result.items if "quote" in i.data and decisions.get(i.id) and decisions[i.id].decision == "approve"]
        rows = [{k: v for k, v in i.data["quote"].items() if k != "lines"} | {"catatan": decisions[i.id].note} for i in approved]
        lines = [{"request_id": i.data["quote"]["request_id"], **ln} for i in approved for ln in i.data["quote"]["lines"]]
        outputs = {"report": str(self.write_review_excel(out_dir / f"quotation_{ts}.xlsx", result, decisions, {
            "Disetujui": pd.DataFrame(rows), "Rincian biaya": pd.DataFrame(lines)}))}
        if approved:
            path = out_dir / f"draft_quotation_{ts}.txt"
            path.write_text(("\n\n" + "=" * 50 + "\n\n").join(i.draft for i in approved), encoding="utf-8")
            outputs["quotations"] = str(path)
        ask = [i for i in result.items if decisions.get(i.id) and decisions[i.id].decision == "ask_hq"]
        if ask:
            path = out_dir / f"draft_email_HQ_{ts}.txt"
            path.write_text("\n".join(["Subject: Persetujuan harga quotation", "", "Dear pricing/HQ,", "",
                                       "Mohon arahan untuk quotation berikut:", "",
                                       *(f"- {i.subject} {i.title}: {i.detail}" + (f" (catatan: {decisions[i.id].note})" if decisions[i.id].note else "")
                                         for i in ask),
                                       "", "Terima kasih.", "", "[DRAFT dibuat oleh agent - kirim setelah dicek staf]"]), encoding="utf-8")
            outputs["draft_email"] = str(path)
        return outputs

    def demo(self, base: Path) -> Path:
        out = base / "pricing"
        out.mkdir(parents=True, exist_ok=True)
        today = date.today()
        iso = lambda d: d.isoformat()  # noqa: E731
        ship = today + timedelta(days=10)
        pd.DataFrame([
            {"Request ID": "Q-1001", "Customer": "PT Sinar Bahari Logistik", "POL": "IDTPP", "POD": "SGSIN", "Type": "20GP", "Qty": 10, "Ship Date": iso(ship)},
            {"Request ID": "Q-1002", "Customer": "PT Nusantara Agro Ekspor", "POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Qty": 4, "Ship Date": iso(ship)},
            {"Request ID": "Q-1003", "Customer": "CV Maju Jaya Furniture", "POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Qty": 6, "Ship Date": iso(ship)},
            {"Request ID": "Q-1004", "Customer": "Global Trading Pte Ltd", "POL": "IDTPP", "POD": "SGSIN", "Type": "20GP", "Qty": 10, "Ship Date": iso(ship + timedelta(days=20))},
            {"Request ID": "Q-1005", "Customer": "PT Karya Tekstil Indonesia", "POL": "IDTPP", "POD": "VNSGN", "Type": "40GP", "Qty": 3, "Ship Date": iso(ship)},
            {"Request ID": "Q-1006", "Customer": "PT Mega Kopi Nusantara", "POL": "IDTPP", "POD": "THLCH", "Type": "20GP", "Qty": 8, "Ship Date": iso(ship)},
            {"Request ID": "Q-1007", "Customer": "PT Lautan Rempah Abadi", "POL": "IDTPP", "POD": "MYPKG", "Type": "", "Qty": 2, "Ship Date": iso(ship)},
            {"Request ID": "Q-1008", "Customer": "PT Sinar Bahari Logistik", "POL": "IDTPP", "POD": "AEJEA", "Type": "40HC", "Qty": 2, "Ship Date": iso(ship)},
        ]).to_csv(out / "quote_requests.csv", index=False)
        pd.DataFrame([
            {"Customer": "PT Sinar Bahari Logistik", "POL": "IDTPP", "POD": "SGSIN", "Type": "20GP", "Rate USD": 210, "Valid From": iso(today - timedelta(days=60)), "Valid To": iso(today + timedelta(days=60))},
            {"Customer": "PT Nusantara Agro Ekspor", "POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Rate USD": 640, "Valid From": iso(today - timedelta(days=120)), "Valid To": iso(today - timedelta(days=5))},
            {"Customer": "CV Maju Jaya Furniture", "POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Rate USD": 590, "Valid From": iso(today - timedelta(days=30)), "Valid To": iso(today + timedelta(days=90))},
        ]).to_excel(out / "rate_contract.xlsx", index=False)
        pd.DataFrame([
            {"POL": "IDTPP", "POD": "SGSIN", "Type": "20GP", "Rate USD": 260},
            {"POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Rate USD": 720},
            {"POL": "IDTPP", "POD": "VNSGN", "Type": "40GP", "Rate USD": 520},
            {"POL": "IDTPP", "POD": "THLCH", "Type": "20GP", "Rate USD": 310},
            {"POL": "IDTPP", "POD": "MYPKG", "Type": "20GP", "Rate USD": 190},
        ]).to_csv(out / "tariff_public.csv", index=False)
        pd.DataFrame([
            {"Code": "BAF", "POD": "", "Type": "20GP", "Amount USD": 45, "Effective From": iso(today - timedelta(days=90)), "Source": "HQ circular 2026-07"},
            {"Code": "BAF", "POD": "", "Type": "40GP", "Amount USD": 90, "Effective From": iso(today - timedelta(days=90)), "Source": "HQ circular 2026-07"},
            {"Code": "BAF", "POD": "", "Type": "40HC", "Amount USD": 90, "Effective From": iso(today - timedelta(days=90)), "Source": "HQ circular 2026-07"},
            {"Code": "PSS", "POD": "CNSHA", "Type": "", "Amount USD": 150, "Effective From": iso(today + timedelta(days=5)), "Source": f"Email HQ {today - timedelta(days=2):%d/%m}"},
            {"Code": "EBS", "POD": "THLCH", "Type": "", "Amount USD": 35, "Effective From": iso(today - timedelta(days=3)), "Source": f"Email HQ {today - timedelta(days=3):%d/%m}"},
        ]).to_csv(out / "surcharge_update.csv", index=False)
        pd.DataFrame([
            {"POL": "IDTPP", "POD": "SGSIN", "Type": "20GP", "Cost USD": 175},
            {"POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Cost USD": 780},
            {"POL": "IDTPP", "POD": "VNSGN", "Type": "40GP", "Cost USD": 455},
            {"POL": "IDTPP", "POD": "THLCH", "Type": "20GP", "Cost USD": 250},
        ]).to_csv(out / "cost_lane.csv", index=False)
        pd.DataFrame([
            {"Carrier": "Liner A", "POL": "IDTPP", "POD": "SGSIN", "Type": "20GP", "Rate USD": 240, "Observed At": iso(today - timedelta(days=4)), "Source": "telepon sales"},
            {"Carrier": "Liner B", "POL": "IDTPP", "POD": "CNSHA", "Type": "40HC", "Rate USD": 760, "Observed At": iso(today - timedelta(days=6)), "Source": "portal"},
            {"Carrier": "Liner C", "POL": "IDTPP", "POD": "VNSGN", "Type": "40GP", "Rate USD": 520, "Observed At": iso(today - timedelta(days=50)), "Source": "situs publik"},
            {"Carrier": "Liner A", "POL": "IDTPP", "POD": "THLCH", "Type": "20GP", "Rate USD": 300, "Observed At": iso(today - timedelta(days=2)), "Source": "telepon sales"},
        ]).to_csv(out / "competitor_rates.csv", index=False)
        return out
