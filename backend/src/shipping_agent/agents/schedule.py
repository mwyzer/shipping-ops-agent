"""Agent Jadwal: schedule changes, stacking period and space allocation.

Compares the schedule customers were told (baseline) with the latest one from
the liner, recomputes open-stack and CY closing, lists affected bookings, and
checks booked TEU against the voyage allocation. Stowage and bay plans stay with
the planner: this agent only prepares the numbers and the notices.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from ..parsing import blank, map_columns, parse_amount, parse_datetime
from ..schemas import Severity
from .base import Agent, AgentResult, DecisionOption, Kpi, ReviewDecision, ReviewItem, find_file, rank, read_any, stamp

POL = "IDTPP"
OPEN_STACK_DAYS = 5      # open stack = ETD minus 5 days, 08:00
CLOSING_HOURS = 24       # CY closing = ETD minus 24 hours
OVERBOOK_PCT = 100
NEAR_FULL_PCT = 90
LOW_UTIL_PCT = 60
TEU = {"20GP": 1, "20RF": 1, "40GP": 2, "40HC": 2, "40RF": 2, "45HC": 2.25}

SCHED = {"vessel": ["vessel", "kapal", "vesselname"], "voyage": ["voyage", "voy"], "port": ["port", "pelabuhan", "locode"],
         "eta": ["eta"], "etd": ["etd"]}
BOOK = {"booking_no": ["bookingno", "booking", "bkgno"], "customer": ["customer", "shipper", "pelanggan"],
        "email": ["email", "contact"], "vessel": ["vessel", "kapal"], "voyage": ["voyage", "voy"],
        "pod": ["pod", "destination", "tujuan"], "container_type": ["containertype", "type", "tipe"], "qty": ["qty", "jumlah", "box"],
        "booked_at": ["bookedat", "tanggalbooking", "createdat"]}
ALLOC = {"vessel": ["vessel", "kapal"], "voyage": ["voyage", "voy"], "allocated_teu": ["allocatedteu", "allocation", "alokasi", "teu"]}


def stacking(etd: datetime) -> tuple[datetime, datetime]:
    open_stack = (etd - timedelta(days=OPEN_STACK_DAYS)).replace(hour=8, minute=0)
    return open_stack, etd - timedelta(hours=CLOSING_HOURS)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%d/%m/%Y %H:%M") if dt else "-"


class ScheduleAgent(Agent):
    name = "schedule"
    label = "Agent Jadwal"
    description = "Memantau perubahan jadwal kapal, menghitung ulang stacking period dan closing CY, serta alokasi space vs booking."
    keywords = ("jadwal", "schedule", "stacking", "closing", "open stack", "alokasi", "space", "delay", "eta", "etd", "kapal", "voyage")
    file_hints = ("schedule", "jadwal", "allocation", "alokasi")
    demo_request = "Cek perubahan jadwal kapal dan dampaknya ke stacking period dan booking"
    decision_options = [
        DecisionOption(key="approve", label="Setujui & siapkan notice", tone="approve"),
        DecisionOption(key="reject", label="Tolak", tone="reject"),
        DecisionOption(key="ask_line", label="Konfirmasi ke pelayaran", tone="ask"),
    ]

    @staticmethod
    def _table(folder: Path, aliases: dict, *hints: str) -> pd.DataFrame | None:
        path = find_file(folder, *hints)
        if path is None:
            return None
        frame, _ = map_columns(read_any(path), aliases)
        for col in aliases:
            if col not in frame:
                frame[col] = None
        for col in ("vessel", "voyage", "port", "pod", "container_type"):
            if col in frame:
                frame[col] = frame[col].map(lambda v: "" if blank(v) else str(v).strip().upper())
        return frame

    def run(self, folder: Path) -> AgentResult:
        base = self._table(folder, SCHED, "baseline", "lama", "published")
        latest = self._table(folder, SCHED, "latest", "terbaru", "update")
        if latest is None:
            raise FileNotFoundError("Tidak ada jadwal terbaru (nama file mengandung 'latest', 'terbaru' atau 'update')")
        bookings = self._table(folder, BOOK, "booking")
        alloc = self._table(folder, ALLOC, "allocation", "alokasi")
        notes = []
        if base is None:
            notes.append("Jadwal baseline tidak ada: perubahan jadwal tidak bisa dibandingkan")
        if bookings is None:
            notes.append("Data booking tidak ada: dampak ke pelanggan dan alokasi space tidak dihitung")
            bookings = pd.DataFrame(columns=list(BOOK))
        bookings = bookings.assign(teu=[(parse_amount(q) or 0) * TEU.get(t, 2) for q, t in zip(bookings["qty"], bookings["container_type"])])
        items: list[ReviewItem] = []
        voyages = latest[["vessel", "voyage"]].drop_duplicates().itertuples(index=False)
        rows_changes = []

        for vessel, voyage in voyages:
            key = f"{vessel} {voyage}"
            lat = latest[(latest["vessel"] == vessel) & (latest["voyage"] == voyage)]
            old = base[(base["vessel"] == vessel) & (base["voyage"] == voyage)] if base is not None else lat.iloc[0:0]
            vb = bookings[(bookings["vessel"] == vessel) & (bookings["voyage"] == voyage)]
            customers = sorted(set(vb["customer"]))

            # ETD at POL -> stacking period
            new_pol = lat[lat["port"] == POL]
            old_pol = old[old["port"] == POL]
            if len(new_pol) and len(old_pol):
                etd_new, etd_old = parse_datetime(new_pol.iloc[0]["etd"]), parse_datetime(old_pol.iloc[0]["etd"])
                if etd_new and etd_old and etd_new != etd_old:
                    hours = (etd_new - etd_old).total_seconds() / 3600
                    o_old, c_old = stacking(etd_old)
                    o_new, c_new = stacking(etd_new)
                    earlier = hours < 0
                    sev = Severity.HIGH if earlier or abs(hours) >= 48 else Severity.MEDIUM
                    direction = f"maju {abs(hours):.0f} jam" if earlier else f"mundur {hours:.0f} jam"
                    rows_changes.append({"voyage": key, "jenis": "ETD", "lama": fmt(etd_old), "baru": fmt(etd_new),
                                         "closing_lama": fmt(c_old), "closing_baru": fmt(c_new), "booking_terdampak": len(vb)})
                    notice = self._notice(key, etd_new, o_new, c_new, earlier)
                    items.append(ReviewItem(
                        subject=key, group="ETD maju" if earlier else "ETD mundur", kind="etd_change", severity=sev,
                        title=f"ETD {POL} {direction}: closing CY jadi {fmt(c_new)}",
                        detail=(f"Open stack {fmt(o_old)} → {fmt(o_new)}. {len(vb)} booking, {len(customers)} pelanggan terdampak"
                                + (": closing lebih awal, cargo berisiko tertinggal" if earlier else "")),
                        sources=["jadwal baseline", "jadwal terbaru"], expected=fmt(c_old), actual=fmt(c_new),
                        suggestion="Kirim pemberitahuan open stack/closing CY baru ke pelanggan terdampak",
                        draft=notice, data={"notify": customers, "bookings": vb["booking_no"].tolist(), "notice": notice}))

            # port omission (rotation changed)
            omitted = sorted(set(old["port"]) - set(lat["port"]))
            for port in omitted:
                hit = vb[vb["pod"] == port]
                rows_changes.append({"voyage": key, "jenis": "Omit port", "lama": port, "baru": "-",
                                     "closing_lama": "", "closing_baru": "", "booking_terdampak": len(hit)})
                items.append(ReviewItem(
                    subject=key, group="Pelabuhan di-omit", kind="port_omitted", severity=Severity.HIGH,
                    title=f"{port} dihapus dari rotasi; {len(hit)} booking ke {port} perlu dipindah",
                    detail="Booking: " + ", ".join(hit["booking_no"].tolist()[:10]) if len(hit) else "Tidak ada booking ke port ini",
                    sources=["jadwal baseline", "jadwal terbaru"], expected=port, actual="tidak ada di rotasi",
                    suggestion="Konfirmasi ke pelayaran voyage pengganti, lalu kabari pelanggan",
                    data={"notify": sorted(set(hit["customer"])), "bookings": hit["booking_no"].tolist()}))

            # space allocation
            if alloc is not None:
                a = alloc[(alloc["vessel"] == vessel) & (alloc["voyage"] == voyage)]
                if len(a):
                    cap = parse_amount(a.iloc[0]["allocated_teu"]) or 0
                    booked = float(vb["teu"].sum())
                    util = booked / cap * 100 if cap else 0
                    if util > OVERBOOK_PCT:
                        excess = booked - cap
                        roll = vb.sort_values("booked_at", ascending=False)
                        picked, acc = [], 0.0
                        for _, b in roll.iterrows():
                            if acc >= excess:
                                break
                            picked.append(b["booking_no"])
                            acc += b["teu"]
                        items.append(ReviewItem(
                            subject=key, group="Overbooking", kind="overbooked", severity=Severity.HIGH,
                            title=f"Booking {booked:.0f} TEU melebihi alokasi {cap:.0f} TEU ({util:.0f}%)",
                            detail=f"Usulan roll {acc:.0f} TEU (booking terakhir masuk): {', '.join(picked)}",
                            expected=f"≤ {cap:.0f} TEU", actual=f"{booked:.0f} TEU",
                            suggestion="Roll booking terakhir ke voyage berikutnya atau minta tambahan alokasi",
                            data={"roll": picked, "excess_teu": excess}))
                    elif util >= NEAR_FULL_PCT:
                        items.append(ReviewItem(
                            subject=key, group="Hampir penuh", kind="near_full", severity=Severity.MEDIUM,
                            title=f"Terisi {util:.0f}% ({booked:.0f}/{cap:.0f} TEU), sisa {cap - booked:.0f} TEU",
                            suggestion="Batasi booking baru; prioritaskan pelanggan kontrak"))
                    elif util < LOW_UTIL_PCT:
                        items.append(ReviewItem(
                            subject=key, group="Space masih longgar", kind="low_util", severity=Severity.LOW,
                            title=f"Baru terisi {util:.0f}% ({booked:.0f}/{cap:.0f} TEU), sisa {cap - booked:.0f} TEU",
                            suggestion="Info ke tim sales untuk menawarkan space"))

        items = rank(items)
        changed = sum(1 for i in items if i.kind in {"etd_change", "port_omitted"})
        affected = len({b for i in items for b in i.data.get("bookings", [])})
        facts = (f"{len(latest[['vessel', 'voyage']].drop_duplicates())} voyage dipantau. {changed} perubahan jadwal, "
                 f"{affected} booking terdampak. {sum(1 for i in items if i.kind == 'overbooked')} voyage overbooking.")
        return AgentResult(
            agent=self.name, title=f"Jadwal kapal {date.today():%d/%m/%Y}", subject_label="Voyage",
            kpis=[
                Kpi(label="Voyage dipantau", value=str(len(latest[["vessel", "voyage"]].drop_duplicates())), detail=f"POL {POL}"),
                Kpi(label="Perubahan jadwal", value=str(changed), detail="ETD berubah / port di-omit"),
                Kpi(label="Booking terdampak", value=str(affected), detail=f"dari {len(bookings)} booking"),
                Kpi(label="Aturan stacking", value=f"H-{OPEN_STACK_DAYS}", detail=f"open stack 08:00, closing ETD-{CLOSING_HOURS} jam"),
            ],
            items=items, facts=facts, notes=notes,
            context={"changes": rows_changes,
                     "bookings": bookings.drop(columns=["teu"]).where(pd.notna(bookings.drop(columns=["teu"])), None).to_dict("records")},
        )

    @staticmethod
    def _notice(voyage: str, etd: datetime, open_stack: datetime, closing: datetime, earlier: bool) -> str:
        return (f"Subject: [UPDATE] Open stack & closing CY {voyage}\n\nYth. Pelanggan,\n\n"
                f"Jadwal {voyage} berubah. Berikut jadwal terbaru di {POL}:\n"
                f"  ETD           : {fmt(etd)}\n  Open stack    : {fmt(open_stack)}\n  Closing CY    : {fmt(closing)}\n\n"
                + ("Closing CY menjadi LEBIH AWAL. Mohon pastikan kontainer masuk sebelum waktu di atas.\n\n" if earlier else "")
                + "Mohon sesuaikan jadwal stuffing dan trucking Anda.\n\nSalam,\nTim Operasional")

    def finalize(self, result: AgentResult, decisions: dict[str, ReviewDecision], out_dir: Path) -> dict[str, str]:
        ts = stamp()
        bookings = pd.DataFrame(result.context["bookings"])
        notices, roll_rows, ask = [], [], []
        for item in result.items:
            d = decisions.get(item.id)
            if not d:
                continue
            if d.decision == "approve":
                if item.data.get("notice"):
                    for cust in item.data["notify"]:
                        mine = bookings[(bookings["customer"] == cust) & bookings["booking_no"].isin(item.data["bookings"])]
                        to = next((e for e in mine["email"] if not blank(e)), "(email belum ada)")
                        notices.append(f"To: {to}\n" + item.data["notice"].replace(
                            "Yth. Pelanggan,", f"Yth. {cust},\nBooking Anda: {', '.join(mine['booking_no'])}"))
                for b in item.data.get("roll", []):
                    roll_rows.append({"voyage": item.subject, "booking_no": b, "aksi": "roll ke voyage berikutnya", "catatan": d.note})
            elif d.decision == "ask_line":
                ask.append((item, d))
        outputs = {"report": str(self.write_review_excel(out_dir / f"jadwal_{ts}.xlsx", result, decisions, {
            "Perubahan": pd.DataFrame(result.context["changes"]), "Rencana roll": pd.DataFrame(roll_rows)}))}
        if notices:
            path = out_dir / f"draft_notice_pelanggan_{ts}.txt"
            path.write_text(("\n\n" + "=" * 50 + "\n\n").join(notices) + "\n\n[DRAFT - kirim setelah dicek staf]", encoding="utf-8")
            outputs["notices"] = str(path)
        if ask:
            path = out_dir / f"draft_email_pelayaran_{ts}.txt"
            path.write_text("\n".join(["Subject: Konfirmasi perubahan jadwal/space", "", "Dear liner ops,", "",
                                       "Mohon konfirmasi hal berikut:", "",
                                       *(f"- {i.subject}: {i.title}" + (f" ({d.note})" if d.note else "") for i, d in ask),
                                       "", "Terima kasih.", "", "[DRAFT dibuat oleh agent - kirim setelah dicek staf]"]), encoding="utf-8")
            outputs["draft_email"] = str(path)
        return outputs

    def demo(self, base: Path) -> Path:
        rng = random.Random(9)
        out = base / "jadwal"
        out.mkdir(parents=True, exist_ok=True)
        d0 = datetime.combine(date.today(), datetime.min.time())
        rotations = {
            ("MV BAHARI JAYA", "041N"): [("IDTPP", 6), ("SGSIN", 8), ("CNSHA", 13)],
            ("MV BAHARI JAYA", "042N"): [("IDTPP", 13), ("SGSIN", 15), ("CNSHA", 20)],
            ("MV SAMUDRA PERSADA", "017S"): [("IDTPP", 9), ("VNSGN", 12), ("THLCH", 14)],
            ("MV SAMUDRA PERSADA", "018S"): [("IDTPP", 16), ("VNSGN", 19), ("THLCH", 21)],
        }
        base_rows, latest_rows = [], []
        for (vessel, voy), ports in rotations.items():
            for port, day in ports:
                etd = d0 + timedelta(days=day, hours=18)
                row = {"Vessel": vessel, "Voyage": voy, "Port": port,
                       "ETA": (etd - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M"), "ETD": etd.strftime("%Y-%m-%d %H:%M")}
                base_rows.append(row)
                new = dict(row)
                if voy == "041N":
                    new["ETD"] = (etd + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
                if voy == "042N" and port == "IDTPP":
                    new["ETD"] = (etd - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")
                if voy == "017S" and port == "VNSGN":
                    continue
                latest_rows.append(new)
        pd.DataFrame(base_rows).to_csv(out / "schedule_baseline.csv", index=False)
        pd.DataFrame(latest_rows).to_csv(out / "schedule_latest.csv", index=False)

        customers = {"PT Sinar Bahari Logistik": "rina@sinarbahari.example", "PT Nusantara Agro Ekspor": "andi@agroekspor.example",
                     "CV Maju Jaya Furniture": "budi@majujaya.example", "PT Karya Tekstil Indonesia": "dewi@karyatekstil.example",
                     "PT Mega Kopi Nusantara": "finance@megakopi.example"}
        books = []
        plan = {("MV BAHARI JAYA", "041N"): 30, ("MV BAHARI JAYA", "042N"): 12,
                ("MV SAMUDRA PERSADA", "017S"): 14, ("MV SAMUDRA PERSADA", "018S"): 6}
        for (vessel, voy), n in plan.items():
            pods = [p for p, _ in rotations[(vessel, voy)] if p != "IDTPP"]
            for k in range(n):
                cust = rng.choice(list(customers))
                books.append({"Booking No": f"BKG{rng.randint(100000, 999999)}", "Customer": cust, "Email": customers[cust],
                              "Vessel": vessel, "Voyage": voy, "POD": rng.choice(pods), "Type": rng.choice(["20GP", "40HC", "40GP"]),
                              "Qty": rng.randint(1, 4), "Booked At": (d0 - timedelta(days=20 - k % 20)).strftime("%Y-%m-%d")})
        pd.DataFrame(books).to_csv(out / "booking_list.csv", index=False)
        pd.DataFrame([
            {"Vessel": "MV BAHARI JAYA", "Voyage": "041N", "Allocated TEU": 120},
            {"Vessel": "MV BAHARI JAYA", "Voyage": "042N", "Allocated TEU": 45},
            {"Vessel": "MV SAMUDRA PERSADA", "Voyage": "017S", "Allocated TEU": 60},
            {"Vessel": "MV SAMUDRA PERSADA", "Voyage": "018S", "Allocated TEU": 60},
        ]).to_csv(out / "space_allocation.csv", index=False)
        return out
