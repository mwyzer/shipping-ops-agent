"""Agent Laporan: weekly lifting report from booking data, with data checks first.

The report numbers are computed with pandas from the rows a person approved.
Rows with missing data (e.g. CBF not yet sent by CS) are held and listed,
instead of silently producing a wrong total.
"""

from __future__ import annotations

import random
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..parsing import blank, map_columns, norm_id, parse_amount, parse_date
from ..schemas import Severity
from .base import Agent, AgentResult, DecisionOption, Kpi, ReviewDecision, ReviewItem, rank, read_any, stamp

ALIASES = {
    "booking_no": ["bookingno", "booking", "nobooking", "bkgno"],
    "etd": ["etd", "tanggalberangkat", "sailingdate", "tglmuat"],
    "customer": ["customer", "shipper", "pelanggan"],
    "pol": ["pol", "portofloading"],
    "pod": ["pod", "destination", "portofdischarge", "tujuan"],
    "container_type": ["containertype", "type", "sizetype", "tipe", "size"],
    "qty": ["qty", "quantity", "jumlah", "units", "box"],
    "revenue_usd": ["revenueusd", "revenue", "freightusd", "freight"],
    "cbf": ["cbf", "cbfno", "nocbf", "cbfdata"],
}
REQUIRED = ["booking_no", "etd", "customer", "pod", "container_type", "qty", "cbf"]
LABEL = {"booking_no": "No. booking", "etd": "ETD", "customer": "Customer", "pod": "POD",
         "container_type": "Tipe kontainer", "qty": "Qty", "cbf": "CBF", "revenue_usd": "Revenue"}
TEU = {"20GP": 1, "20RF": 1, "40GP": 2, "40HC": 2, "40RF": 2, "45HC": 2.25}
LOCODE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}$")


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build_report(rows: pd.DataFrame, week: str) -> dict[str, pd.DataFrame | dict]:
    cur = rows[rows["week"] == week]
    prev_week = sorted(w for w in rows["week"].unique() if w < week)
    prev = rows[rows["week"] == prev_week[-1]] if prev_week else rows.iloc[0:0]
    teu, teu_prev = cur["teu"].sum(), prev["teu"].sum()
    rev, rev_prev = cur["revenue_usd"].sum(), prev["revenue_usd"].sum()
    pct = lambda a, b: (a - b) / b * 100 if b else None  # noqa: E731
    by = lambda col: (cur.groupby(col, as_index=False)  # noqa: E731
                      .agg(booking=("booking_no", "count"), box=("qty", "sum"), teu=("teu", "sum"), revenue_usd=("revenue_usd", "sum"))
                      .sort_values("teu", ascending=False))
    return {
        "headline": {"week": week, "prev_week": prev_week[-1] if prev_week else None, "booking": int(len(cur)),
                     "teu": float(teu), "teu_prev": float(teu_prev), "teu_change_pct": pct(teu, teu_prev),
                     "revenue_usd": float(rev), "revenue_prev": float(rev_prev), "revenue_change_pct": pct(rev, rev_prev)},
        "pod": by("pod"), "customer": by("customer"), "type": by("container_type"),
    }


def report_markdown(rep: dict, held: int, flagged: int) -> str:
    h = rep["headline"]
    chg = lambda v: "n/a" if v is None else f"{v:+.1f}%"  # noqa: E731
    lines = [
        f"# Weekly lifting report {h['week']}", "",
        f"- Booking: {h['booking']}",
        f"- Volume: {h['teu']:,.0f} TEU ({chg(h['teu_change_pct'])} vs {h['prev_week'] or '-'}: {h['teu_prev']:,.0f} TEU)",
        f"- Revenue: USD {h['revenue_usd']:,.0f} ({chg(h['revenue_change_pct'])})",
        "", "## Top POD", "", "| POD | Booking | TEU | Revenue USD |", "|---|---:|---:|---:|",
        *(f"| {r.pod} | {r.booking} | {r.teu:,.0f} | {r.revenue_usd:,.0f} |" for r in rep["pod"].head(5).itertuples()),
        "", "## Top customer", "", "| Customer | Booking | TEU | Revenue USD |", "|---|---:|---:|---:|",
        *(f"| {r.customer} | {r.booking} | {r.teu:,.0f} | {r.revenue_usd:,.0f} |" for r in rep["customer"].head(5).itertuples()),
        "",
    ]
    if held:
        lines.append(f"Catatan: {held} booking ditahan (data belum lengkap) dan belum masuk angka di atas.")
    if flagged:
        lines.append(f"Catatan: {flagged} booking bertanda (*) memiliki angka di luar kebiasaan dan sudah dicek reviewer.")
    return "\n".join(lines)


class ReportingAgent(Agent):
    name = "reporting"
    label = "Agent Laporan"
    description = "Mengecek kelengkapan data booking lalu menyusun weekly lifting report (TEU, revenue, per POD dan customer)."
    keywords = ("laporan", "report", "summary", "rekap", "weekly", "mingguan", "bulanan", "monthly", "lifting", "dashboard", "outport")
    file_hints = ("lifting", "booking", "shipment", "report")
    demo_request = "Buatkan weekly lifting report minggu ini dari data booking"
    decision_options = [
        DecisionOption(key="approve", label="Setujui usulan", tone="approve"),
        DecisionOption(key="reject", label="Pakai apa adanya", tone="reject"),
        DecisionOption(key="ask_cs", label="Tanya CS", tone="ask"),
    ]

    def _load(self, folder: Path) -> tuple[pd.DataFrame, list[str]]:
        frames, notes = [], []
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in {".csv", ".xlsx", ".xls"} and any(h in path.stem.lower() for h in self.file_hints):
                frame, unmapped = map_columns(read_any(path), ALIASES)
                if unmapped:
                    notes.append(f"{path.name}: kolom tidak dipakai {unmapped}")
                frame["source"] = [f"{path.name} baris {i + 2}" for i in range(len(frame))]
                frames.append(frame)
        if not frames:
            raise FileNotFoundError("Tidak ada data booking (nama file harus mengandung 'lifting', 'booking' atau 'shipment')")
        data = pd.concat(frames, ignore_index=True)
        for col in ALIASES:
            if col not in data:
                data[col] = None
                notes.append(f"Kolom {LABEL.get(col, col)} tidak ada di file")
        data["rid"] = [f"R{i + 1:03d}" for i in range(len(data))]
        return data, notes

    def run(self, folder: Path) -> AgentResult:
        data, notes = self._load(folder)
        items: list[ReviewItem] = []
        hold: set[str] = set()

        def add(row, **kw) -> None:
            kw.setdefault("subject", str(row["booking_no"]) if not blank(row["booking_no"]) else row["source"])
            kw.setdefault("sources", [row["source"]])
            kw.setdefault("data", {})["rid"] = row["rid"]
            items.append(ReviewItem(**kw))

        data["etd_date"] = data["etd"].map(parse_date)
        data["qty_num"] = data["qty"].map(parse_amount)
        data["rev_num"] = data["revenue_usd"].map(parse_amount)
        data["ctype"] = data["container_type"].map(lambda v: "" if blank(v) else str(v).strip().upper())
        data["pod_n"] = data["pod"].map(lambda v: "" if blank(v) else str(v).strip().upper())

        for _, row in data.iterrows():
            # wrong column: POD and container type swapped is the classic monthly-report mistake
            if row["ctype"] and row["pod_n"] and LOCODE.match(row["ctype"]) and row["pod_n"] in TEU:
                add(row, group="Salah kolom", kind="swapped_columns", severity=Severity.HIGH,
                    title=f"POD dan tipe kontainer tertukar ({row['pod_n']} / {row['ctype']})",
                    expected=f"POD {row['ctype']}, tipe {row['pod_n']}", actual=f"POD {row['pod_n']}, tipe {row['ctype']}",
                    suggestion="Tukar kembali kedua kolom", data={"fix": {"pod_n": row["ctype"], "ctype": row["pod_n"]}})
                continue
            missing = [LABEL[c] for c in REQUIRED if blank(row[c])]
            if missing:
                hold.add(row["rid"])
                cbf_only = missing == ["CBF"]
                add(row, group="Menunggu data CS" if cbf_only else "Data wajib kosong", kind="missing_field",
                    severity=Severity.MEDIUM if cbf_only else Severity.HIGH,
                    title=f"Data belum lengkap: {', '.join(missing)}",
                    suggestion="Tahan dari laporan minggu ini dan cantumkan di daftar tunggu", data={"hold": True})
                continue
            if pd.isna(row["qty_num"]) or row["qty_num"] <= 0:
                add(row, group="Format salah", kind="bad_qty", severity=Severity.HIGH,
                    title=f"Qty bukan angka: '{row['qty']}'", actual=str(row["qty"]),
                    suggestion="Tahan dari laporan sampai diperbaiki", data={"hold": True})
            elif row["ctype"] not in TEU:
                add(row, group="Format salah", kind="bad_type", severity=Severity.MEDIUM,
                    title=f"Tipe kontainer tidak dikenal: '{row['ctype']}'", actual=row["ctype"],
                    suggestion="Tahan dari laporan sampai diperbaiki", data={"hold": True})
            elif not LOCODE.match(row["pod_n"]):
                add(row, group="Format salah", kind="bad_pod", severity=Severity.MEDIUM,
                    title=f"POD bukan kode pelabuhan 5 huruf: '{row['pod_n']}'", actual=row["pod_n"],
                    suggestion="Tahan dari laporan sampai diperbaiki", data={"hold": True})
            if (row["etd_date"] is None or pd.isna(row["etd_date"])) and not blank(row["etd"]):
                add(row, group="Format salah", kind="bad_date", severity=Severity.MEDIUM,
                    title=f"ETD tidak valid: '{row['etd']}'", suggestion="Tahan dari laporan sampai diperbaiki",
                    data={"hold": True})

        # duplicates
        keyed = data[~data["booking_no"].map(blank)].assign(k=lambda f: f["booking_no"].map(norm_id))
        for _, row in keyed[keyed.duplicated("k")].iterrows():
            first = keyed[keyed["k"] == row["k"]].iloc[0]
            add(row, group="Booking ganda", kind="duplicate_booking", severity=Severity.HIGH,
                title=f"Booking sama dengan {first['source']}", sources=[first["source"], row["source"]],
                suggestion="Hapus baris ganda dari laporan", data={"drop": True})

        # outliers: revenue per TEU far from the lane median
        ok = data[(data["qty_num"].notna()) & (data["ctype"].isin(TEU)) & (data["rev_num"].notna())].copy()
        ok["rpt"] = ok["rev_num"] / (ok["qty_num"] * ok["ctype"].map(TEU))
        med = ok.groupby("pod_n")["rpt"].median()
        for _, row in ok.iterrows():
            m = med.get(row["pod_n"])
            if m and (row["rpt"] > 3 * m or row["rpt"] < m / 3):
                add(row, group="Angka tidak wajar", kind="revenue_outlier", severity=Severity.MEDIUM,
                    title=f"Revenue per TEU USD {row['rpt']:,.0f}, biasanya ~USD {m:,.0f} untuk {row['pod_n']}",
                    expected=f"~USD {m:,.0f}/TEU", actual=f"USD {row['rpt']:,.0f}/TEU",
                    suggestion="Masukkan dengan tanda (*) setelah dicek; Tolak = keluarkan baris",
                    data={"flag": True})

        dated = data["etd_date"].dropna()
        week = iso_week(max(dated)) if len(dated) else "-"
        preview_rows = self._rows(data, items, {i.id: ReviewDecision(finding_id=i.id, decision="approve", reviewer="preview")
                                                for i in rank(items)}, rank(items))
        items = rank(items)
        rep = build_report(preview_rows, week) if len(preview_rows) else None
        held = sum(1 for i in items if i.data.get("hold"))
        draft = report_markdown(rep, held, sum(1 for i in items if i.data.get("flag"))) if rep else "Tidak ada data."
        items.append(ReviewItem(
            id=f"F{len(items) + 1:03d}", subject=f"Weekly report {week}", group="Draft laporan", kind="report_draft",
            severity=Severity.LOW, title="Draft weekly lifting report (pratinjau jika semua usulan disetujui)",
            suggestion="Terbitkan laporan final dari baris yang disetujui", draft=draft))
        h = rep["headline"] if rep else {"teu": 0, "booking": 0, "teu_change_pct": None, "revenue_usd": 0}
        facts = (f"{len(data)} baris booking dibaca. Minggu {week}: {h['booking']} booking, {h['teu']:,.0f} TEU"
                 + (f" ({h['teu_change_pct']:+.1f}% vs minggu lalu)" if h.get("teu_change_pct") is not None else "")
                 + f". {len(items) - 1} masalah data perlu diputuskan sebelum laporan diterbitkan.")
        context_rows = data.drop(columns=["etd_date"]).assign(etd_iso=data["etd_date"].map(lambda d: d.isoformat() if d else None))
        return AgentResult(
            agent=self.name, title=f"Weekly report {week}", subject_label="Booking",
            kpis=[
                Kpi(label="Baris booking", value=str(len(data)), detail=f"minggu laporan {week}"),
                Kpi(label="TEU minggu ini", value=f"{h['teu']:,.0f}",
                    detail="vs minggu lalu " + (f"{h['teu_change_pct']:+.1f}%" if h.get("teu_change_pct") is not None else "-")),
                Kpi(label="Revenue (USD)", value=f"{h['revenue_usd']:,.0f}", detail="pratinjau"),
                Kpi(label="Ditahan", value=str(held), detail="data belum lengkap / salah format"),
            ],
            items=items, facts=facts, notes=notes,
            context={"rows": context_rows.where(pd.notna(context_rows), None).to_dict("records"), "week": week},
        )

    @staticmethod
    def _rows(data: pd.DataFrame, items: list[ReviewItem], decisions: dict[str, ReviewDecision],
              ranked: list[ReviewItem] | None = None) -> pd.DataFrame:
        frame = data.copy().set_index("rid")
        frame["flag"] = ""
        excluded: set[str] = set()
        for item in ranked or items:
            rid = item.data.get("rid")
            if rid is None:
                continue
            d = decisions.get(item.id)
            if d is None or d.decision == "ask_cs":
                excluded.add(rid)
                continue
            if d.decision == "approve":
                if item.data.get("hold") or item.data.get("drop"):
                    excluded.add(rid)
                for col, val in item.data.get("fix", {}).items():
                    frame.loc[rid, col] = val
                if item.data.get("flag"):
                    frame.loc[rid, "flag"] = "*"
            elif d.decision == "reject" and item.data.get("flag"):
                excluded.add(rid)
        frame = frame.drop(index=[r for r in excluded if r in frame.index])
        frame = frame[frame["ctype"].isin(TEU) & frame["qty_num"].notna()]
        etd = frame["etd_date"] if "etd_date" in frame else frame["etd_iso"].map(parse_date)
        frame = frame.assign(etd_date=etd).dropna(subset=["etd_date"])
        return pd.DataFrame({
            "booking_no": frame["booking_no"], "etd": frame["etd_date"], "week": frame["etd_date"].map(iso_week),
            "customer": frame["customer"], "pod": frame["pod_n"], "container_type": frame["ctype"],
            "qty": frame["qty_num"], "teu": frame["qty_num"] * frame["ctype"].map(TEU),
            "revenue_usd": frame["rev_num"].fillna(0), "flag": frame["flag"],
        }).reset_index(drop=True)

    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        ts, week = stamp(), result.context["week"]
        data = pd.DataFrame(result.context["rows"])
        for col in ("qty_num", "rev_num"):
            data[col] = pd.to_numeric(data[col], errors="coerce")
        data_items = [i for i in result.items if i.kind != "report_draft"]
        rows = self._rows(data, data_items, decisions)
        held = [i for i in data_items if i.data.get("rid") and (
            decisions.get(i.id) is None or decisions[i.id].decision == "ask_cs"
            or (decisions[i.id].decision == "approve" and i.data.get("hold")))]
        draft_item = next(i for i in result.items if i.kind == "report_draft")
        publish = decisions.get(draft_item.id) and decisions[draft_item.id].decision == "approve"
        outputs: dict[str, str] = {}
        extra = {"Data dipakai": rows,
                 "Ditahan": pd.DataFrame([{"booking": i.subject, "alasan": i.title, "sumber": ", ".join(i.sources)} for i in held])}
        if publish and len(rows):
            rep = build_report(rows, week)
            md = report_markdown(rep, len(held), int((rows["flag"] == "*").sum()))
            md_path = out_dir / f"weekly_report_{week}_{ts}.md"
            md_path.write_text(md, encoding="utf-8")
            outputs["report_md"] = str(md_path)
            extra = {"Per POD": rep["pod"], "Per customer": rep["customer"], "Per tipe": rep["type"], **extra}
        outputs["report"] = str(self.write_review_excel(out_dir / f"laporan_{week}_{ts}.xlsx", result, decisions, extra))
        ask = [i for i in data_items if decisions.get(i.id) and decisions[i.id].decision == "ask_cs"]
        if ask:
            path = out_dir / f"draft_email_CS_{week}_{ts}.txt"
            path.write_text("\n".join([
                f"Subject: Data booking untuk weekly report {week}", "", "Halo tim CS,", "",
                "Mohon dilengkapi/diperbaiki data booking berikut agar bisa masuk laporan:", "",
                *(f"- {i.subject}: {i.title}" for i in ask), "", "Terima kasih.", "",
                "[DRAFT dibuat oleh agent - kirim setelah dicek staf]"]), encoding="utf-8")
            outputs["draft_email"] = str(path)
        return outputs

    def demo(self, base: Path) -> Path:
        rng = random.Random(5)
        out = base / "lifting"
        out.mkdir(parents=True, exist_ok=True)
        customers = ["PT Sinar Bahari Logistik", "PT Nusantara Agro Ekspor", "CV Maju Jaya Furniture",
                     "PT Karya Tekstil Indonesia", "PT Mega Kopi Nusantara"]
        pods = {"SGSIN": 180, "MYPKG": 160, "CNSHA": 420, "VNSGN": 260, "THLCH": 300}
        types = ["20GP", "40GP", "40HC"]
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        rows = []
        for week_start, n in ((monday - timedelta(days=7), 22), (monday, 26)):
            for _ in range(n):
                t, pod = rng.choice(types), rng.choice(list(pods))
                qty = rng.randint(1, 6)
                rows.append({
                    "Booking No": f"BKG{rng.randint(100000, 999999)}",
                    "ETD": (week_start + timedelta(days=rng.randint(0, min(6, max(0, (today - week_start).days))))).strftime("%d/%m/%Y"),
                    "Shipper": rng.choice(customers), "POL": "IDTPP", "POD": pod, "Type": t, "Qty": str(qty),
                    "Freight USD": str(round(qty * TEU[t] * pods[pod] * rng.uniform(0.85, 1.15))),
                    "CBF": f"CBF-{rng.randint(1000, 9999)}",
                })
        n = len(rows)
        rows[n - 1]["CBF"] = ""
        rows[n - 2]["CBF"] = ""
        rows[n - 3]["CBF"] = ""
        rows[n - 4]["POD"], rows[n - 4]["Type"] = rows[n - 4]["Type"], rows[n - 4]["POD"]
        rows[n - 5]["Qty"] = "dua"
        rows[n - 6]["Freight USD"] = str(int(float(rows[n - 6]["Freight USD"]) * 10))
        rows.append(dict(rows[n - 7]))
        pd.DataFrame(rows).to_csv(out / "lifting_booking.csv", index=False)
        return out
