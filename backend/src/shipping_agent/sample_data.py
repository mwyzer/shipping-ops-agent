"""Generate a realistic demo voyage with known, injected discrepancies.

Each document deliberately uses different column names, as real files from
different POLs and systems do. All data is fictional.
"""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd

from .rules import iso6346_check_digit

OWNERS = ["MSKU", "TGHU", "CAIU", "SEGU", "TCLU"]
SHIPPERS = [
    ("PT SINAR BAHARI LOGISTIK", "0123456789012345"),
    ("PT NUSANTARA AGRO EKSPOR", "0234567890123456"),
    ("CV MAJU JAYA FURNITURE", "0345678901234567"),
    ("PT KARYA TEKSTIL INDONESIA", "0456789012345678"),
]
CONSIGNEES = ["GLOBAL TRADING PTE LTD", "ASIA HOME DECOR SDN BHD", "PACIFIC FOODS LTD"]
PODS = ["SGSIN", "MYPKG", "CNSHA"]

# What the demo injects, so tests and the README can point at it.
INJECTED = {
    "missing_in_manifest": "container only in container list",
    "lookalike_container": "one-character typo in stacking list",
    "weight_mismatch": "stacking list weight off by 2,400 kg",
    "pod_mismatch": "proforma POD differs",
    "seal_no_mismatch": "container list seal differs",
    "npwp_invalid_length": "15-digit NPWP",
    "npwp_not_registered": "NPWP not in customer master",
    "duplicate_in_document": "duplicate row in container list",
    "missing_in_proforma_loading": "container cancelled from proforma",
}


def make_container_no(rng: random.Random) -> str:
    first10 = rng.choice(OWNERS) + f"{rng.randint(0, 999999):06d}"
    return first10 + str(iso6346_check_digit(first10))


def typo(container_no: str) -> str:
    """Swap the last two serial digits (classic keying error)."""
    s = list(container_no)
    s[8], s[9] = s[9], s[8]
    if s == list(container_no):
        s[9] = str((int(s[9]) + 1) % 10)
    return "".join(s)


def generate(folder: Path, voyage: str = "GA123", n: int = 24, seed: int = 7) -> Path:
    rng = random.Random(seed)
    out = folder / voyage
    out.mkdir(parents=True, exist_ok=True)

    rows = []
    seen: set[str] = set()
    while len(rows) < n:
        cn = make_container_no(rng)
        if cn in seen or cn[8] == cn[9]:
            continue
        seen.add(cn)
        shipper, npwp = rng.choice(SHIPPERS)
        size = rng.choice(["20GP", "40HC", "40GP"])
        rows.append({
            "container_no": cn, "size_type": size,
            "gross_weight_kg": rng.randint(8000, 26000) if size != "20GP" else rng.randint(6000, 21000),
            "seal_no": f"ID{rng.randint(100000, 999999)}", "pol": "IDTPP", "pod": rng.choice(PODS),
            "shipper": shipper, "consignee": rng.choice(CONSIGNEES), "npwp": npwp,
        })
    base = pd.DataFrame(rows)

    manifest = base.copy()
    container_list = base.copy()
    proforma = base.copy()
    stacking = base.copy()

    # --- inject discrepancies -------------------------------------------------
    extra = make_container_no(rng)  # missing_in_manifest
    extra_row = base.iloc[0].to_dict() | {"container_no": extra, "seal_no": "ID777777"}
    container_list = pd.concat([container_list, pd.DataFrame([extra_row])], ignore_index=True)

    stacking.loc[2, "container_no"] = typo(stacking.loc[2, "container_no"])  # lookalike
    stacking.loc[4, "gross_weight_kg"] = stacking.loc[4, "gross_weight_kg"] + 2400  # weight
    proforma.loc[6, "pod"] = "MYPKG" if proforma.loc[6, "pod"] != "MYPKG" else "SGSIN"  # pod
    container_list.loc[8, "seal_no"] = "ID000123"  # seal
    manifest.loc[10, "npwp"] = manifest.loc[10, "npwp"][:15]  # 15-digit NPWP
    manifest.loc[12, "npwp"] = "0999888777666555"  # unregistered
    container_list = pd.concat([container_list, container_list.iloc[[14]]], ignore_index=True)  # duplicate
    proforma = proforma.drop(index=16).reset_index(drop=True)  # cancelled

    # --- write each document with its own header style ------------------------
    manifest.rename(columns={
        "container_no": "Container No", "size_type": "Size/Type", "gross_weight_kg": "Gross Weight (KG)",
        "seal_no": "Seal No", "pol": "POL", "pod": "POD", "shipper": "Shipper",
        "consignee": "Consignee", "npwp": "NPWP",
    }).to_excel(out / f"manifest_{voyage}.xlsx", index=False)

    # Container list from the terminal: spaced IDs + different headers.
    cl = container_list[["container_no", "size_type", "gross_weight_kg", "seal_no", "pod"]].copy()
    cl["container_no"] = cl["container_no"].map(lambda c: f"{c[:4]} {c[4:10]}-{c[10]}")
    cl.rename(columns={"container_no": "CNTR NO", "size_type": "ISO Code", "gross_weight_kg": "VGM",
                       "seal_no": "Seal", "pod": "Port of Discharge"}).to_excel(
        out / f"container_list_{voyage}.xlsx", index=False)

    proforma[["container_no", "size_type", "pod"]].rename(columns={
        "container_no": "No. Kontainer", "size_type": "Ukuran", "pod": "Pelabuhan Tujuan"}).to_excel(
        out / f"proforma_loading_{voyage}.xlsx", index=False)

    st = stacking[["container_no", "size_type", "gross_weight_kg", "pod"]].copy()
    st["block"] = [f"A{(i % 4) + 1}" for i in range(len(st))]
    st.rename(columns={"container_no": "Container", "size_type": "Type", "gross_weight_kg": "Weight",
                       "pod": "Destination", "block": "Block"}).to_csv(out / f"stacking_list_{voyage}.csv", index=False)

    master = pd.DataFrame([{"Customer": s, "NPWP": n} for s, n in SHIPPERS])
    master.to_csv(out / "master_customers.csv", index=False)
    return out


if __name__ == "__main__":
    print(generate(Path("data/sample")))
