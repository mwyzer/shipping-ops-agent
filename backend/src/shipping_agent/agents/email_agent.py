"""Agent Email: triage an inbox export and draft replies + Notices of Arrival.

Input: .eml files (exported from Outlook/Gmail), optionally `arrivals` and
`consignee` tables for NOA. Output is a daily digest and draft .eml files.
Nothing is sent: drafts land in a folder for staff to open and send.
"""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import pandas as pd

from ..llm import get_chat_model
from ..parsing import blank, map_columns, norm_company, parse_date
from ..schemas import Severity
from .base import Agent, AgentResult, DecisionOption, Kpi, ReviewDecision, ReviewItem, find_file, rank, read_any, safe_filename, stamp

CATEGORIES: list[tuple[str, str, tuple[str, ...]]] = [
    # (key, label, keywords) - first match wins, so specific before generic
    ("newsletter", "Newsletter / promosi", ("unsubscribe", "newsletter", "promo", "webinar", "diskon spesial")),
    ("complaint", "Komplain", ("komplain", "complain", "kecewa", "rusak", "damage", "claim", "klaim", "tidak profesional")),
    ("hq", "Permintaan HQ", ("@hq.", "head office", "kantor pusat", "dari hq", "hq request")),
    ("rate", "Permintaan tarif", ("rate", "tarif", "quotation", "penawaran harga", "ocean freight", "harga")),
    ("schedule", "Jadwal kapal", ("schedule", "jadwal", "delay", "etd", "eta", "omit", "closing time")),
    ("arrival", "Kedatangan / DO", ("delivery order", " do ", "pengambilan", "arrival", "kedatangan", "noa", "release")),
    ("documentation", "Dokumentasi", ("b/l", "bill of lading", "shipping instruction", " si ", "manifest", "draft bl", "koreksi")),
    ("booking", "Booking", ("booking", "space", "pesan kontainer", "reservasi")),
    ("billing", "Tagihan / pembayaran", ("invoice", "tagihan", "pembayaran", "payment", "bukti transfer")),
]
CAT_LABEL = {k: label for k, label, _ in CATEGORIES} | {"other": "Lainnya", "noa": "NOA"}
URGENT = ("urgent", "segera", "asap", "hari ini", "today", "mendesak", "penting")
ROUTE_HINT = {"rate": "Agent Tarif", "schedule": "Agent Jadwal", "booking": "Agent Jadwal (cek space)",
              "documentation": "Agent Rekonsiliasi", "billing": "Agent Input Data"}

REPLY = {
    "booking": "Terima kasih atas permintaan booking Anda{ref}. Kami cek ketersediaan space dan jadwal kapal, "
               "konfirmasi akan kami kirim paling lambat hari ini.",
    "rate": "Terima kasih atas permintaan tarifnya. Penawaran harga untuk rute yang Anda minta sedang kami siapkan "
            "dan akan kami kirim segera.",
    "schedule": "Terima kasih atas informasinya{ref}. Kami sedang memeriksa dampak perubahan jadwal ini terhadap "
                "booking terkait dan akan mengabari langkah selanjutnya.",
    "arrival": "Terima kasih{ref}. Untuk penerbitan DO, mohon lampirkan B/L asli/telex release dan bukti pembayaran "
               "local charges. Kami proses setelah dokumen lengkap.",
    "documentation": "Terima kasih{ref}. Koreksi dokumen Anda sudah kami terima dan sedang kami cek terhadap manifest. "
                     "Draft terbaru akan kami kirim untuk persetujuan.",
    "complaint": "Mohon maaf atas ketidaknyamanan yang Anda alami{ref}. Kasus ini sudah kami teruskan ke supervisor "
                 "operasional dan kami akan menghubungi Anda hari ini dengan perkembangan.",
    "billing": "Terima kasih{ref}. Tagihan/bukti pembayaran Anda sudah kami terima dan kami teruskan ke tim finance "
               "untuk diverifikasi.",
    "hq": "Noted{ref}. Kami siapkan data yang diminta dan kirim sebelum tenggat.",
}
ENTITY_PATTERNS = {
    "booking": r"\bBKG\d{5,}\b",
    "container": r"\b[A-Z]{4}\d{7}\b",
    "bl": r"\b(?:BL|B/L)[\s:#-]*([A-Z0-9-]{6,})\b",
    "voyage": r"\b(?:voy(?:age)?\.?\s*)([A-Z0-9]{3,8})\b",
}


def parse_eml(path: Path) -> dict:
    msg = BytesParser(policy=policy.default).parse(path.open("rb"))
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    body = re.sub(r"<[^>]+>", " ", body)
    try:
        when = parsedate_to_datetime(msg["date"]) if msg["date"] else None
    except (TypeError, ValueError):
        when = None
    name, addr = parseaddr(str(msg["from"] or ""))
    return {"file": path.name, "from_name": name, "from": addr.lower(), "subject": str(msg["subject"] or "(tanpa subjek)"),
            "date": when.replace(tzinfo=None).isoformat(timespec="minutes") if when else "", "body": body.strip(),
            "message_id": str(msg["message-id"] or "")}


def classify(mail: dict) -> tuple[str, str]:
    """Keyword score per category; words in the subject count double. Newsletter/complaint/HQ win outright."""
    subject = f" {mail['subject']} ".lower()
    text = f" {mail['from']} {mail['subject']} {mail['body']} ".lower()
    scores = {}
    for key, _, words in CATEGORIES:
        score = sum(1 for w in words if w in text) + sum(1 for w in words if w in subject)
        if score and key in {"newsletter", "complaint", "hq"}:
            return key, "keyword"
        scores[key] = score
    best = max(scores, key=lambda k: scores[k])
    if scores[best]:
        return best, "keyword"
    model = get_chat_model()
    if model is not None:
        try:
            keys = ", ".join(k for k, _, _ in CATEGORIES)
            out = str(model.invoke(f"Kategori email operasional pelayaran. Jawab SATU kata dari: {keys}, other.\n\n"
                                   f"Subjek: {mail['subject']}\n{mail['body'][:1500]}").content).strip().lower()
            if out in CAT_LABEL:
                return out, "llm"
        except Exception:
            pass
    return "other", "keyword"


def entities(text: str) -> dict[str, list[str]]:
    found = {}
    for key, pattern in ENTITY_PATTERNS.items():
        hits = re.findall(pattern, text, flags=re.IGNORECASE if key in {"bl", "voyage"} else 0)
        if key == "bl":
            hits = [h for h in hits if not re.fullmatch(ENTITY_PATTERNS["booking"], h.upper())]
        if hits:
            found[key] = sorted(set(h.upper() for h in hits))
    return found


def make_eml(to: str, subject: str, body: str, in_reply_to: str = "") -> bytes:
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg["X-Draft-By"] = "shipping-ops-agent (belum dikirim)"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    return bytes(msg)


class EmailAgent(Agent):
    name = "email"
    label = "Agent Email"
    description = "Memilah inbox per kategori dan prioritas, menyiapkan draft balasan dan NOA ke consignee."
    keywords = ("email", "inbox", "noa", "notice of arrival", "blasting", "balas", "surel", "korespondensi")
    file_hints = (".eml", "arrival", "consignee")
    demo_request = "Pilah inbox hari ini, siapkan draft balasan dan NOA untuk kedatangan kapal"
    decision_options = [
        DecisionOption(key="approve", label="Setujui draft", tone="approve"),
        DecisionOption(key="reject", label="Abaikan", tone="reject"),
        DecisionOption(key="forward", label="Teruskan", tone="ask"),
    ]

    def run(self, folder: Path) -> AgentResult:
        mails = [parse_eml(p) for p in sorted(folder.glob("*.eml"))]
        arrivals_file = find_file(folder, "arrival", "kedatangan")
        if not mails and not arrivals_file:
            raise FileNotFoundError("Tidak ada file .eml atau data arrival di folder")
        items: list[ReviewItem] = []
        notes: list[str] = []
        by_cat: dict[str, int] = {}
        threads: dict[str, list[dict]] = {}
        for m in mails:
            threads.setdefault(re.sub(r"^((re|fw|fwd)\s*:\s*)+", "", m["subject"].lower()).strip(), []).append(m)

        for key, group in threads.items():
            m = max(group, key=lambda x: x["date"])  # latest message of the thread
            cat, how = classify(m)
            by_cat[cat] = by_cat.get(cat, 0) + 1
            text = f"{m['subject']}\n{m['body']}"
            ents = entities(text)
            thread_text = " ".join(f"{g['subject']} {g['body']}" for g in group).lower()
            urgent = any(w in thread_text for w in URGENT)
            sev = (Severity.HIGH if cat in {"complaint", "hq"} or urgent
                   else Severity.LOW if cat in {"newsletter", "other"} else Severity.MEDIUM)
            ref_bits = [f"{k.upper()} {', '.join(v)}" for k, v in ents.items()]
            ref = f" ({'; '.join(ref_bits)})" if ref_bits else ""
            snippet = re.sub(r"\s+", " ", m["body"])[:220]
            draft = None
            if cat in REPLY:
                greeting = f"Yth. {m['from_name'] or 'Bapak/Ibu'},"
                draft = (f"To: {m['from']}\nSubject: Re: {m['subject']}\n\n{greeting}\n\n"
                         f"{REPLY[cat].format(ref=ref)}\n\nSalam,\nTim Operasional")
            suggestion = {"newsletter": "Arsipkan, tidak perlu dibalas",
                          "other": "Baca manual, kategori tidak dikenali"}.get(cat, "Kirim draft balasan")
            if cat in ROUTE_HINT:
                suggestion += f"; tindak lanjut oleh {ROUTE_HINT[cat]}"
            items.append(ReviewItem(
                subject=m["subject"][:80], group=CAT_LABEL[cat], kind=f"mail_{cat}", severity=sev,
                title=f"{CAT_LABEL[cat]} dari {m['from_name'] or m['from']}" + (" · MENDESAK" if urgent else ""),
                detail=f"{m['date'].replace('T', ' ')} · {snippet}" + (f" · {len(group)} pesan dalam thread" if len(group) > 1 else ""),
                sources=[g["file"] for g in group], suggestion=suggestion, draft=draft,
                data={"to": m["from"], "subject": f"Re: {m['subject']}", "in_reply_to": m["message_id"],
                      "category": cat, "entities": ents, "classified_by": how, "body": draft or ""}))

        noa_count = 0
        if arrivals_file:
            arrivals, _ = map_columns(read_any(arrivals_file), {
                "bl_no": ["blno", "bl", "nobl", "billoflading"], "consignee": ["consignee", "penerima"],
                "vessel": ["vessel", "kapal"], "voyage": ["voyage", "voy"], "eta": ["eta", "tibaperkiraan"],
                "pod": ["pod", "pelabuhan"], "containers": ["containers", "kontainer", "containerno"]})
            cons_file = find_file(folder, "consignee", "penerima")
            master = pd.DataFrame()
            if cons_file:
                master, _ = map_columns(read_any(cons_file), {
                    "consignee": ["consignee", "nama", "penerima"], "email": ["email", "surel"],
                    "address": ["address", "alamat"], "updated_at": ["updatedat", "diperbarui", "tglupdate"]})
                master["key"] = master["consignee"].map(norm_company)
                master["upd"] = master["updated_at"].map(parse_date)
            else:
                notes.append("Master consignee tidak ada: alamat/email NOA tidak bisa diisi")
            for _, a in arrivals.iterrows():
                noa_count += 1
                rows = master[master["key"] == norm_company(a["consignee"])].sort_values("upd") if len(master) else master
                latest = rows.iloc[-1] if len(rows) else None
                eta = parse_date(a.get("eta"))
                body = (f"To: {latest['email'] if latest is not None and not blank(latest['email']) else '(email belum ada)'}\n"
                        f"Subject: Notice of Arrival - B/L {a['bl_no']}\n\n"
                        f"Yth. {a['consignee']},\n\nKiriman Anda dengan B/L {a['bl_no']} diangkut kapal {a.get('vessel', '-')} "
                        f"voyage {a.get('voyage', '-')}, perkiraan tiba di {a.get('pod', '-')} pada "
                        f"{eta:%d/%m/%Y}." if eta else "")
                body += (f"\nKontainer: {a.get('containers', '-')}\n"
                         f"Alamat terdaftar: {latest['address'] if latest is not None else '-'}\n\n"
                         "Untuk pengambilan DO, siapkan B/L asli/telex release dan pembayaran local charges.\n\n"
                         "Salam,\nTim Import")
                base = dict(subject=f"NOA B/L {a['bl_no']}", group="NOA", sources=[arrivals_file.name], draft=body,
                            data={"to": latest["email"] if latest is not None else "", "subject": f"Notice of Arrival - B/L {a['bl_no']}",
                                  "body": body, "category": "noa"})
                if latest is None or blank(latest["email"]):
                    items.append(ReviewItem(**base, kind="noa_no_contact", severity=Severity.HIGH,
                                            title=f"NOA {a['consignee']}: email consignee tidak ada di master",
                                            suggestion="Lengkapi master consignee, lalu kirim NOA"))
                elif len(rows) > 1 and rows.iloc[-1]["address"] != rows.iloc[-2]["address"]:
                    items.append(ReviewItem(**base, kind="noa_address_changed", severity=Severity.MEDIUM,
                                            title=f"NOA {a['consignee']}: alamat berubah sejak {rows.iloc[-1]['updated_at']}",
                                            expected=str(rows.iloc[-1]["address"]), actual=str(rows.iloc[-2]["address"]),
                                            suggestion="Pakai alamat terbaru (sudah di draft); pastikan benar"))
                else:
                    items.append(ReviewItem(**base, kind="noa_ready", severity=Severity.LOW,
                                            title=f"NOA siap untuk {a['consignee']}", suggestion="Kirim NOA"))

        items = rank(items)
        urgent_n = sum(1 for i in items if i.severity == Severity.HIGH)
        facts = (f"{len(mails)} email dalam {len(threads)} thread dipilah ke {len(by_cat)} kategori; {urgent_n} butuh perhatian segera. "
                 + (f"{noa_count} NOA disiapkan." if noa_count else ""))
        return AgentResult(
            agent=self.name, title=f"Inbox {datetime.now():%d/%m/%Y}", subject_label="Email / NOA",
            kpis=[
                Kpi(label="Email masuk", value=str(len(mails)), detail=f"{len(threads)} thread"),
                Kpi(label="Mendesak", value=str(urgent_n), detail="komplain, HQ, kata 'urgent/segera'"),
                Kpi(label="Draft balasan", value=str(sum(1 for i in items if i.draft and i.data.get('category') != 'noa')),
                    detail="belum dikirim"),
                Kpi(label="NOA", value=str(noa_count), detail=arrivals_file.name if arrivals_file else "tidak ada data arrival"),
            ],
            items=items, facts=facts, notes=notes,
            context={"categories": by_cat, "mail_count": len(mails)},
        )

    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        ts = stamp()
        drafts = out_dir / f"draft_email_{ts}"
        written = 0
        for item in result.items:
            d = decisions.get(item.id)
            if d and d.decision == "approve" and item.draft and item.data.get("to"):
                body = item.draft.split("\n\n", 1)[1] if item.draft.startswith("To:") else item.draft
                if d.note:
                    body += f"\n\n[catatan reviewer: {d.note}]"
                drafts.mkdir(parents=True, exist_ok=True)
                (drafts / f"{item.id}_{safe_filename(item.subject, 40)}.eml").write_bytes(
                    make_eml(item.data["to"], item.data["subject"], body, item.data.get("in_reply_to", "")))
                written += 1
        lines = [f"# Ringkasan inbox ({datetime.now():%d/%m/%Y %H:%M})", "", result.facts, ""]
        for sev, title in (("HIGH", "Segera"), ("MEDIUM", "Hari ini"), ("LOW", "Bisa menunggu")):
            group = [i for i in result.items if i.severity.value == sev]
            if group:
                lines += [f"## {title} ({len(group)})", ""]
                for i in group:
                    d = decisions.get(i.id)
                    lines.append(f"- [{i.group}] {i.title} - _{i.subject}_ → {self.option_label(d.decision) if d else 'belum diputuskan'}"
                                 + (f" ({d.note})" if d and d.note else ""))
                lines.append("")
        digest = out_dir / f"ringkasan_inbox_{ts}.md"
        digest.write_text("\n".join(lines), encoding="utf-8")
        outputs = {"digest": str(digest),
                   "report": str(self.write_review_excel(out_dir / f"triase_email_{ts}.xlsx", result, decisions))}
        if written:
            import shutil
            outputs["drafts_zip"] = shutil.make_archive(str(drafts), "zip", drafts)
        return outputs

    def demo(self, base: Path) -> Path:
        rng = random.Random(3)
        out = base / "inbox"
        out.mkdir(parents=True, exist_ok=True)
        now = datetime.now().replace(second=0, microsecond=0)
        mails = [
            ("Rina Hartono <rina@sinarbahari.example>", "Request booking 4x40HC Jakarta - Shanghai",
             "Dear team,\nMohon booking 4x40HC untuk ETD minggu depan ke CNSHA. Cargo: furniture.\nRegards, Rina"),
            ("Budi Santoso <budi@majujaya.example>", "Komplain: kontainer rusak saat diterima",
             "Kontainer TGHU3904879 diterima dalam kondisi rusak di pintu. Kami sangat kecewa, mohon segera ditindaklanjuti."),
            ("HQ Trade Desk <report@hq.example>", "URGENT: volume report per lane sebelum jam 15.00",
             "Hi, HQ request: please send TEU volume per lane for the last 4 weeks today before 15:00. Thanks."),
            ("Lisa Wong <lisa@globaltrading.example>", "Rate inquiry IDTPP to SGSIN 20GP",
             "Hello, could you share your ocean freight rate for 10x20GP from Tanjung Priok to Singapore for November?"),
            ("Ops Liner <ops@liner-partner.example>", "Schedule update: MV Bahari Jaya voy 041N delay",
             "Please be informed MV Bahari Jaya voyage 041N ETD Tanjung Priok delayed 2 days due to port congestion."),
            ("Andi <andi@agroekspor.example>", "Koreksi draft BL BKG615642",
             "Mohon koreksi consignee di draft BL untuk booking BKG615642, nama perusahaan kurang 'PT'."),
            ("Dewi <dewi@karyatekstil.example>", "Pengambilan DO BL IDJKT260915",
             "Kami ingin mengambil delivery order untuk BL IDJKT260915. Dokumen apa saja yang diperlukan?"),
            ("Finance PT Mega Kopi <finance@megakopi.example>", "Bukti transfer invoice INV-OF-2209",
             "Terlampir bukti transfer untuk invoice INV-OF-2209. Mohon dikonfirmasi."),
            ("Port Webinar <news@portevents.example>", "Webinar: digitalisasi pelabuhan 2026",
             "Daftar sekarang untuk webinar kami. Klik untuk unsubscribe."),
            ("Rina Hartono <rina@sinarbahari.example>", "Re: Request booking 4x40HC Jakarta - Shanghai",
             "Tambahan: mohon space dikonfirmasi hari ini ya, cargo sudah siap."),
            ("Pak Joko <joko@vendor-depo.example>", "Info jam operasional depo",
             "Depo kami buka sampai jam 22.00 mulai minggu depan."),
        ]
        for i, (sender, subject, body) in enumerate(mails):
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, "ops@yourline.example", subject
            msg["Date"] = (now - timedelta(minutes=(len(mails) - i) * 37 + rng.randint(0, 20))).strftime("%a, %d %b %Y %H:%M:%S +0700")
            msg["Message-ID"] = f"<demo-{i}@inbox.example>"
            msg.set_content(body)
            (out / f"mail_{i + 1:02d}.eml").write_bytes(bytes(msg))
        eta = date.today() + timedelta(days=3)
        pd.DataFrame([
            {"BL No": "IDJKT260915", "Consignee": "PT Karya Tekstil Indonesia", "Vessel": "MV Bahari Jaya", "Voyage": "041N", "ETA": eta.strftime("%d/%m/%Y"), "POD": "IDTPP", "Containers": "CAIU2604948"},
            {"BL No": "IDJKT260916", "Consignee": "PT Mega Kopi Nusantara", "Vessel": "MV Bahari Jaya", "Voyage": "041N", "ETA": eta.strftime("%d/%m/%Y"), "POD": "IDTPP", "Containers": "MSKU2318212, MSKU9950442"},
            {"BL No": "IDJKT260917", "Consignee": "CV Maju Jaya Furniture", "Vessel": "MV Bahari Jaya", "Voyage": "041N", "ETA": eta.strftime("%d/%m/%Y"), "POD": "IDTPP", "Containers": "SEGU0844952"},
            {"BL No": "IDJKT260918", "Consignee": "PT Lautan Rempah Abadi", "Vessel": "MV Bahari Jaya", "Voyage": "041N", "ETA": eta.strftime("%d/%m/%Y"), "POD": "IDTPP", "Containers": "TCLU1234560"},
        ]).to_csv(out / "arrivals.csv", index=False)
        pd.DataFrame([
            {"Consignee": "PT Karya Tekstil Indonesia", "Email": "import@karyatekstil.example", "Alamat": "Jl. Industri Raya 12, Bandung", "Updated At": "2025-11-02"},
            {"Consignee": "PT Mega Kopi Nusantara", "Email": "logistik@megakopi.example", "Alamat": "Jl. Pelabuhan 8, Jakarta Utara", "Updated At": "2025-06-10"},
            {"Consignee": "PT Mega Kopi Nusantara", "Email": "logistik@megakopi.example", "Alamat": "Kawasan Industri Cikarang Blok C-5, Bekasi", "Updated At": (date.today() - timedelta(days=12)).isoformat()},
            {"Consignee": "CV Maju Jaya Furniture", "Email": "budi@majujaya.example", "Alamat": "Jl. Mebel 3, Jepara", "Updated At": "2026-01-15"},
        ]).to_csv(out / "consignee_master.csv", index=False)
        return out
