"""Agent Input Data: vendor invoices -> one clean accounting import file.

Reads invoice exports with different layouts (.xlsx/.csv) and single-invoice text
documents (.txt), normalises them, checks them against the vendor master and the
bank statement, and prepares an import file. Nothing is imported: the output is
a CSV for the accounting system, containing only rows a person approved.
"""

from __future__ import annotations

import random
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..llm import get_chat_model
from ..parsing import blank, closest, map_columns, money, norm_company, norm_id, parse_amount, parse_date, transposed
from ..schemas import Severity
from .base import Agent, AgentResult, DecisionOption, Kpi, ReviewDecision, ReviewItem, find_file, rank, read_any, stamp

INVOICE_ALIASES = {
    "vendor": ["vendor", "namavendor", "supplier", "pengirim", "dari", "vendorname"],
    "invoice_no": ["invoiceno", "noinvoice", "nomorinvoice", "notagihan", "nomortagihan", "invoicenumber", "invoice"],
    "invoice_date": ["invoicedate", "tanggal", "tgl", "tglinvoice", "date"],
    "due_date": ["duedate", "jatuhtempo", "tgljatuhtempo"],
    "description": ["description", "keterangan", "uraian"],
    "amount": ["amount", "amountidr", "nominal", "total", "jumlah", "grandtotal", "totalidr"],
    "currency": ["currency", "matauang", "curr"],
}
BANK_ALIASES = {
    "date": ["tanggal", "tgl", "date", "valuedate"],
    "description": ["keterangan", "description", "remark", "berita"],
    "debit": ["debit", "dr", "keluar", "mutasidebit"],
    "credit": ["kredit", "credit", "cr", "masuk"],
}
TEXT_FIELDS = {
    "vendor": r"(?:vendor|supplier|dari|from)\s*[:\-]\s*(.+)",
    "invoice_no": r"(?:no\.?\s*invoice|invoice\s*no\.?|nomor\s*invoice|no\.?\s*tagihan)\s*[:\-]\s*(.+)",
    "invoice_date": r"(?:tanggal(?:\s*invoice)?|invoice\s*date|tgl)\s*[:\-]\s*(.+)",
    "due_date": r"(?:jatuh\s*tempo|due\s*date)\s*[:\-]\s*(.+)",
    "description": r"(?:keterangan|description|uraian)\s*[:\-]\s*(.+)",
    "amount": r"(?:grand\s*total|total(?:\s*tagihan)?|jumlah)\s*[:\-]\s*(.+)",
}
REQUIRED = {"vendor": "nama vendor", "invoice_no": "nomor invoice", "invoice_date": "tanggal", "amount": "nominal"}
IMPORT_COLUMNS = ["vendor", "invoice_no", "invoice_date", "due_date", "currency", "amount", "description", "status_bayar", "source"]


def parse_text_invoice(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        for field, pattern in TEXT_FIELDS.items():
            if field not in found and (m := re.match(pattern, line.strip(), re.IGNORECASE)):
                found[field] = m[1].strip()
    if "amount" in found and re.search(r"\bUSD\b|\$", found["amount"]):
        found["currency"] = "USD"
    return found


def _llm_extract(text: str) -> dict[str, str]:
    """Optional: ask the LLM for fields the regex missed. Never invents defaults."""
    model = get_chat_model()
    if model is None:
        return {}
    from pydantic import BaseModel

    class Extracted(BaseModel):
        vendor: str | None = None
        invoice_no: str | None = None
        invoice_date: str | None = None
        due_date: str | None = None
        amount: str | None = None

    try:
        out = model.with_structured_output(Extracted).invoke(
            "Ambil field invoice dari teks berikut. Kosongkan jika tidak tertulis.\n\n" + text[:4000])
        return {k: v for k, v in out.model_dump().items() if v}
    except Exception:
        return {}


class DataEntryAgent(Agent):
    name = "data_entry"
    label = "Agent Input Data"
    description = "Merapikan invoice vendor dari berbagai format menjadi satu file impor accounting dan mencocokkan mutasi bank."
    keywords = ("input", "invoice", "tagihan", "vendor", "salin", "rapikan", "ekstrak", "mutasi", "accounting",
                "impor", "b/l ke", "entry")
    file_hints = ("invoice", "tagihan", "mutasi", "vendor")
    demo_request = "Rapikan tagihan vendor bulan ini ke format impor accounting dan cocokkan dengan mutasi bank"
    decision_options = [
        DecisionOption(key="approve", label="Setujui usulan", tone="approve"),
        DecisionOption(key="reject", label="Keluarkan", tone="reject"),
        DecisionOption(key="ask_vendor", label="Tanya vendor", tone="ask"),
    ]

    # --------------------------------------------------------------- read ---
    def _load_invoices(self, folder: Path) -> tuple[list[dict], list[str]]:
        records, notes = [], []
        for path in sorted(folder.iterdir()):
            stem = path.stem.lower()
            if not any(h in stem for h in ("invoice", "tagihan", "inv_")):
                continue
            if path.suffix.lower() == ".txt":
                text = path.read_text(encoding="utf-8", errors="replace")
                fields = parse_text_invoice(text)
                missing = [f for f in REQUIRED if f not in fields]
                if missing:
                    fields = {**_llm_extract(text), **fields}
                records.append({**fields, "source": path.name})
            elif path.suffix.lower() in {".csv", ".xlsx", ".xls"}:
                frame, unmapped = map_columns(read_any(path), INVOICE_ALIASES)
                if unmapped:
                    notes.append(f"{path.name}: kolom tidak dipakai {unmapped}")
                for n, row in enumerate(frame.to_dict("records"), start=2):
                    records.append({**{k: v for k, v in row.items() if k in INVOICE_ALIASES}, "source": f"{path.name} baris {n}"})
        for i, r in enumerate(records):
            r["rid"] = f"R{i + 1:03d}"
            r["vendor_raw"] = "" if blank(r.get("vendor")) else str(r["vendor"]).strip()
            r["invoice_no"] = "" if blank(r.get("invoice_no")) else str(r["invoice_no"]).strip()
            r["amount_value"] = parse_amount(r.get("amount"))
            r["date_value"] = parse_date(r.get("invoice_date"))
            r["due_value"] = parse_date(r.get("due_date"))
            cur = str(r.get("currency") or "IDR").upper()
            r["currency"] = "USD" if "USD" in cur or "$" in cur else "IDR"
        return records, notes

    # ---------------------------------------------------------------- run ---
    def run(self, folder: Path) -> AgentResult:
        records, notes = self._load_invoices(folder)
        if not records:
            raise FileNotFoundError("Tidak ada file invoice (nama file harus mengandung 'invoice' atau 'tagihan')")
        vendor_file = find_file(folder, "vendor", "master")
        vendors = []
        if vendor_file:
            vf, _ = map_columns(read_any(vendor_file), {"vendor": ["vendor", "namavendor", "nama", "supplier"]})
            vendors = [str(v) for v in vf.get("vendor", pd.Series(dtype=str)).dropna()]
        else:
            notes.append("Master vendor tidak ada: cek nama vendor dilewati")
        bank_file = find_file(folder, "mutasi", "bank", "statement", "rekening")
        today = date.today()
        items: list[ReviewItem] = []
        flagged: set[str] = set()

        def add(rec: dict | None, **kw) -> None:
            if rec is not None:
                flagged.add(rec["rid"])
                kw.setdefault("subject", rec["invoice_no"] or f"(tanpa nomor) {rec['source']}")
                kw.setdefault("sources", [rec["source"]])
                kw.setdefault("data", {}).setdefault("rids", [rec["rid"]])
            items.append(ReviewItem(**kw))

        # 1. Required fields + parse errors
        for r in records:
            missing = [label for f, label in REQUIRED.items()
                       if (f == "amount" and r["amount_value"] is None and blank(r.get("amount")))
                       or (f == "invoice_date" and blank(r.get("invoice_date")))
                       or (f in {"vendor", "invoice_no"} and not r[f if f == "invoice_no" else "vendor_raw"])]
            if missing:
                add(r, group="Data wajib kosong", kind="missing_field", severity=Severity.HIGH,
                    title=f"Field kosong: {', '.join(missing)}",
                    suggestion="Keluarkan dari impor dan minta vendor mengirim ulang", data={"exclude": True})
                continue
            if r["amount_value"] is None:
                add(r, group="Format tidak terbaca", kind="bad_amount", severity=Severity.HIGH,
                    title=f"Nominal tidak terbaca: '{r.get('amount')}'", actual=str(r.get("amount")),
                    suggestion="Keluarkan dari impor, cek dokumen asli", data={"exclude": True})
            if r["date_value"] is None:
                add(r, group="Format tidak terbaca", kind="bad_date", severity=Severity.MEDIUM,
                    title=f"Tanggal tidak valid: '{r.get('invoice_date')}'", actual=str(r.get("invoice_date")),
                    suggestion="Keluarkan dari impor, konfirmasi tanggal ke vendor", data={"exclude": True})

        # 2. Vendor master (fuzzy)
        if vendors:
            canon = {norm_company(v): v for v in vendors}
            renamed = 0
            for r in records:
                exact = canon.get(norm_company(r["vendor_raw"]))
                if exact and exact != r["vendor_raw"]:
                    r["vendor_raw"], renamed = exact, renamed + 1
            if renamed:
                notes.append(f"{renamed} baris: penulisan nama vendor (PT./Tbk/huruf besar) disamakan dengan master")
            for r in records:
                if r["vendor_raw"] and norm_company(r["vendor_raw"]) not in canon:
                    match = closest(r["vendor_raw"], vendors)
                    if match:
                        add(r, group="Nama vendor", kind="vendor_alias", severity=Severity.LOW,
                            title=f"Nama vendor ditulis berbeda: '{r['vendor_raw']}'", expected=match, actual=r["vendor_raw"],
                            suggestion=f"Pakai nama master: {match}", data={"fix": {"vendor": match}})
                    else:
                        add(r, group="Nama vendor", kind="vendor_unknown", severity=Severity.MEDIUM,
                            title=f"Vendor tidak ada di master: '{r['vendor_raw']}'", actual=r["vendor_raw"],
                            suggestion="Daftarkan vendor baru dulu, atau keluarkan dari impor")

        # 3. Duplicates and look-alike invoice numbers (per vendor)
        by_key: dict[tuple[str, str], list[dict]] = {}
        for r in records:
            if r["invoice_no"]:
                by_key.setdefault((norm_company(r["vendor_raw"]), norm_id(r["invoice_no"])), []).append(r)
        for (_, inv), group in by_key.items():
            for dup in group[1:]:
                same_amount = dup["amount_value"] == group[0]["amount_value"]
                add(dup, group="Tagihan ganda", kind="duplicate_invoice", severity=Severity.HIGH,
                    title=f"Nomor invoice sama dengan {group[0]['source']}" + ("" if same_amount else " (nominal berbeda)"),
                    sources=[group[0]["source"], dup["source"]],
                    expected=money(group[0]["amount_value"]), actual=money(dup["amount_value"]),
                    suggestion="Hapus baris ganda ini dari impor" if same_amount else "Keluarkan, tanyakan ke vendor mana yang benar",
                    data={"exclude": True})
        keys = list(by_key)
        for i, (v1, n1) in enumerate(keys):
            for v2, n2 in keys[i + 1:]:
                if v1 == v2 and transposed(n1, n2):
                    a, b = by_key[(v1, n1)][0], by_key[(v2, n2)][0]
                    add(b, group="Nomor invoice mirip", kind="lookalike_invoice", severity=Severity.MEDIUM,
                        title=f"Nomor mirip dengan {a['invoice_no']} dari vendor yang sama",
                        sources=[a["source"], b["source"]], expected=a["invoice_no"], actual=b["invoice_no"],
                        suggestion="Pastikan keduanya tagihan berbeda (bukan salah ketik), lalu masukkan",
                        data={"rids": [b["rid"]]})

        # 4. Bank statement matching
        paid: dict[str, str] = {}
        if bank_file:
            bank, _ = map_columns(read_any(bank_file), BANK_ALIASES)
            for _, row in bank.iterrows():
                debit = parse_amount(row.get("debit"))
                if not debit:
                    continue
                desc = str(row.get("description", ""))
                hit = next((r for r in records if r["invoice_no"] and norm_id(r["invoice_no"]) in norm_id(desc)), None)
                label = f"Mutasi {row.get('date', '')}: {desc}"
                if hit is None:
                    add(None, subject=desc[:40] or "Mutasi bank", group="Pembayaran", kind="payment_without_invoice",
                        severity=Severity.MEDIUM, title=f"Pembayaran {money(debit)} tanpa invoice yang cocok",
                        detail=label, sources=[bank_file.name],
                        suggestion="Cari invoice-nya; catat di daftar 'perlu ditindaklanjuti'",
                        data={"bank": {"date": str(row.get("date", "")), "description": desc, "debit": debit}})
                elif hit["amount_value"] is not None and abs(hit["amount_value"] - debit) > 1:
                    paid[hit["rid"]] = "Dibayar beda nominal"
                    add(hit, group="Pembayaran", kind="payment_amount_mismatch", severity=Severity.MEDIUM,
                        title="Nominal dibayar berbeda dengan invoice", detail=label,
                        sources=[hit["source"], bank_file.name],
                        expected=money(hit["amount_value"], hit["currency"]), actual=money(debit),
                        suggestion="Masukkan invoice; tandai selisih untuk dicek finance")
                else:
                    paid[hit["rid"]] = "Lunas"
            for r in records:
                if r["rid"] not in paid and r["due_value"] and r["due_value"] < today and r["amount_value"]:
                    add(r, group="Pembayaran", kind="overdue", severity=Severity.LOW,
                        title=f"Belum ada pembayaran, jatuh tempo {r['due_value']:%d/%m/%Y} sudah lewat",
                        suggestion="Masukkan invoice; ingatkan finance soal jatuh tempo")
        else:
            notes.append("Mutasi bank tidak ada: status pembayaran tidak dicek")

        # 5. Clean rows: one item each so every imported row has a decision
        for r in records:
            if r["rid"] not in flagged:
                add(r, group="Siap impor", kind="ready", severity=Severity.LOW,
                    title=f"{r['vendor_raw']} · {money(r['amount_value'], r['currency'])}",
                    detail=str(r.get("description") or ""), suggestion="Masukkan ke file impor")

        items = rank(items)
        ready = sum(1 for i in items if i.kind == "ready")
        total_idr = sum(r["amount_value"] or 0 for r in records if r["currency"] == "IDR")
        issues = len(items) - ready
        facts = (f"{len(records)} baris invoice dibaca dari {len({r['source'].split(' baris')[0] for r in records})} file "
                 f"(total {money(total_idr)}). {ready} siap impor, {issues} perlu dicek.")
        context_records = [{k: (str(v) if isinstance(v, date) else v) for k, v in r.items()} for r in records]
        for r in context_records:
            r["status_bayar"] = paid.get(r["rid"], "Belum dibayar" if bank_file else "Tidak dicek")
        return AgentResult(
            agent=self.name, title=f"Tagihan vendor {date.today():%d/%m/%Y}", subject_label="No. invoice",
            kpis=[
                Kpi(label="Baris invoice", value=str(len(records)), detail=f"total {money(total_idr)}"),
                Kpi(label="Siap impor", value=str(ready), detail="tanpa masalah"),
                Kpi(label="Perlu dicek", value=str(issues), detail="lihat tabel"),
                Kpi(label="Lunas di mutasi bank", value=str(sum(1 for v in paid.values() if v == "Lunas")),
                    detail=bank_file.name if bank_file else "tidak ada file mutasi"),
            ],
            items=items, facts=facts, notes=notes, context={"records": context_records},
        )

    # ----------------------------------------------------------- finalize ---
    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        ts = stamp()
        records = {r["rid"]: dict(r) for r in result.context["records"]}
        excluded: dict[str, str] = {}
        ask_vendor: list[tuple[ReviewItem, ReviewDecision]] = []
        follow_up = []
        for item in result.items:
            d = decisions.get(item.id)
            rids = item.data.get("rids", [])
            if d is None:
                for rid in rids:
                    excluded.setdefault(rid, "Belum diputuskan")
                continue
            if d.decision == "approve":
                for rid in rids:
                    if item.data.get("exclude"):
                        excluded[rid] = item.title
                    for field, value in item.data.get("fix", {}).items():
                        records[rid]["vendor_raw" if field == "vendor" else field] = value
                if "bank" in item.data:
                    follow_up.append({**item.data["bank"], "catatan": d.note})
            elif d.decision == "reject":
                for rid in rids:
                    excluded[rid] = f"Dikeluarkan reviewer: {d.note or item.title}"
            else:
                for rid in rids:
                    excluded[rid] = f"Menunggu jawaban vendor: {item.title}"
                ask_vendor.append((item, d))

        rows = []
        for rid, r in records.items():
            if rid in excluded:
                continue
            rows.append({"vendor": r["vendor_raw"], "invoice_no": r["invoice_no"], "invoice_date": r["date_value"],
                         "due_date": r["due_value"], "currency": r["currency"], "amount": r["amount_value"],
                         "description": r.get("description") or "", "status_bayar": r["status_bayar"],
                         "source": r["source"]})
        import_path = out_dir / f"impor_accounting_{ts}.csv"
        pd.DataFrame(rows, columns=IMPORT_COLUMNS).to_csv(import_path, index=False)
        exceptions = pd.DataFrame([{"source": records[rid]["source"], "invoice_no": records[rid]["invoice_no"],
                                    "alasan": why} for rid, why in excluded.items()])
        extra = {"Impor": pd.DataFrame(rows, columns=IMPORT_COLUMNS), "Dikeluarkan": exceptions}
        if follow_up:
            extra["Mutasi tanpa invoice"] = pd.DataFrame(follow_up)
        outputs = {
            "import_csv": str(import_path),
            "report": str(self.write_review_excel(out_dir / f"input_data_{ts}.xlsx", result, decisions, extra)),
        }
        if ask_vendor:
            path = out_dir / f"draft_email_vendor_{ts}.txt"
            blocks = []
            by_vendor: dict[str, list[str]] = {}
            for item, d in ask_vendor:
                vendor = next((records[r]["vendor_raw"] for r in item.data.get("rids", [])), "Vendor")
                by_vendor.setdefault(vendor or "Vendor", []).append(
                    f"- {item.subject}: {item.title}" + (f" ({d.note})" if d.note else ""))
            for vendor, lines in by_vendor.items():
                blocks += [f"Kepada: {vendor}", "Subject: Konfirmasi tagihan", "", "Dengan hormat,", "",
                           "Mohon konfirmasi tagihan berikut:", *lines, "", "Terima kasih.", "", "-" * 40, ""]
            path.write_text("\n".join(blocks + ["[DRAFT dibuat oleh agent - kirim setelah dicek staf]"]), encoding="utf-8")
            outputs["draft_email"] = str(path)
        return outputs

    # --------------------------------------------------------------- demo ---
    def demo(self, base: Path) -> Path:
        rng = random.Random(11)
        out = base / "tagihan_vendor"
        out.mkdir(parents=True, exist_ok=True)
        vendors = ["PT Samudra Depo Logistik", "PT Trans Kontainer Mandiri", "CV Bahari Trucking", "PT Jasa Terminal Nusantara"]
        pd.DataFrame({"Nama Vendor": vendors, "NPWP": [f"0{rng.randint(10**14, 10**15 - 1)}" for _ in vendors]}).to_csv(
            out / "master_vendor.csv", index=False)
        today = date.today()
        d = lambda days: today - timedelta(days=days)  # noqa: E731

        a_rows = [
            ["INV-2026-0118", d(40), d(10), vendors[0], "Lift on/off Sept", 12_500_000],
            ["INV-2026-0181", d(35), d(5), vendors[0], "Storage depo Sept", 8_750_000],
            ["INV-2026-0120", d(30), d(-0), vendors[0], "Washing container", 3_200_000],
            ["INV-2026-0120", d(30), d(-0), vendors[0], "Washing container", 3_200_000],
            ["INV-2026-0125", d(20), d(-10), vendors[0], "Repair container", 6_400_000],
            ["INV-2026-0127", d(12), d(-18), vendors[0], "Lift on/off Okt", 11_900_000],
        ]
        pd.DataFrame(a_rows, columns=["Invoice No", "Invoice Date", "Due Date", "Vendor", "Description", "Amount (IDR)"]).to_excel(
            out / "invoice_samudra_depo.xlsx", index=False)

        b_rows = [
            ["TKM/IX/0042", d(28).strftime("%d/%m/%Y"), d(-2).strftime("%d/%m/%Y"), "PT. Trans Kontainer Mandiri, Tbk", "Trucking Tanjung Priok - Cikarang", "Rp 4.250.000"],
            ["TKM/IX/0043", d(27).strftime("%d/%m/%Y"), d(-3).strftime("%d/%m/%Y"), "PT. Trans Kontainer Mandiri, Tbk", "Trucking Tanjung Priok - Karawang", "Rp 5.100.000"],
            ["TKM/IX/0044", "31/02/2026", "", "PT. Trans Kontainer Mandiri, Tbk", "Trucking Priok - Bekasi", "Rp 3.900.000"],
            ["TKM/IX/0045", d(15).strftime("%d/%m/%Y"), d(-15).strftime("%d/%m/%Y"), "PT. Trans Kontainer Mandiri, Tbk", "Trucking Priok - Cikarang", ""],
            ["TKM/IX/0046", d(9).strftime("%d/%m/%Y"), d(-21).strftime("%d/%m/%Y"), "PT. Trans Kontainer Mandiri, Tbk", "Trucking Priok - Bogor", "Rp 4.800.000"],
        ]
        pd.DataFrame(b_rows, columns=["No. Tagihan", "Tgl", "Jatuh Tempo", "Nama Vendor", "Keterangan", "Nominal"]).to_csv(
            out / "tagihan_trans_kontainer.csv", index=False)

        texts = {
            "invoice_bahari_0912.txt": f"INVOICE\nDari: CV Bahari Trucking\nNo. Invoice: BHR-0912\nTanggal: {d(25):%d %B %Y}\n"
                                       f"Jatuh Tempo: {d(-5):%d/%m/%Y}\nKeterangan: Haulage 3 x 40HC\nTotal: Rp 9.600.000,00\n",
            "invoice_bahari_0915.txt": f"INVOICE\nDari: CV Bahari Trucking\nNo. Invoice: BHR-0915\nTanggal: {d(8):%d/%m/%Y}\n"
                                       f"Jatuh Tempo: {d(-22):%d/%m/%Y}\nKeterangan: Haulage 2 x 20GP\nTotal: Rp 5.400.000,00\n",
            "invoice_terminal_7781.txt": f"TAX INVOICE\nSupplier: PT Jasa Terminal Nusantra\nInvoice No: JTN-7781\n"
                                         f"Invoice Date: {d(18):%Y-%m-%d}\nDue Date: {d(-12):%Y-%m-%d}\n"
                                         f"Description: THC export voyage GA123\nGrand Total: Rp 21.350.000\n",
            "invoice_gudang_rahayu.txt": f"INVOICE\nDari: PT Gudang Rahayu Sentosa\nNo. Invoice: GRS-221\nTanggal: {d(6):%d/%m/%Y}\n"
                                         f"Keterangan: Sewa gudang transit\nTotal: Rp 7.000.000\n",
        }
        for name, text in texts.items():
            (out / name).write_text(text, encoding="utf-8")

        bank = [
            [d(9).strftime("%d/%m/%Y"), "TRF INV-2026-0118 SAMUDRA DEPO", "12.500.000,00", ""],
            [d(4).strftime("%d/%m/%Y"), "TRF TKM/IX/0042 TRANS KONTAINER", "4.000.000,00", ""],
            [d(3).strftime("%d/%m/%Y"), "TRF BHR-0912 BAHARI", "9.600.000,00", ""],
            [d(2).strftime("%d/%m/%Y"), "TRF PEMBAYARAN JASA SURVEY KAPAL", "2.750.000,00", ""],
            [d(2).strftime("%d/%m/%Y"), "SETORAN PELANGGAN PT SINAR BAHARI", "", "45.000.000,00"],
        ]
        pd.DataFrame(bank, columns=["Tanggal", "Keterangan", "Debit", "Kredit"]).to_csv(out / "mutasi_bank.csv", index=False)
        return out
