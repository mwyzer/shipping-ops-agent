"""Deterministic reconciliation rules.

Comparisons are plain Python on purpose: a number either matches or it does not,
and every rule here is unit-tested. The LLM never decides whether data matches.
"""

from __future__ import annotations

import string

import pandas as pd

from .config import ReconConfig
from .loader import LoadedDocument
from .schemas import Finding, Severity

# ---------------------------------------------------------------- ISO 6346 ---
# Letter values skip multiples of 11 (11, 22, 33).
_LETTER_VALUES: dict[str, int] = {}
_v = 10
for _ch in string.ascii_uppercase:
    if _v % 11 == 0:
        _v += 1
    _LETTER_VALUES[_ch] = _v
    _v += 1


def iso6346_check_digit(first10: str) -> int:
    total = 0
    for i, ch in enumerate(first10):
        value = _LETTER_VALUES[ch] if ch.isalpha() else int(ch)
        total += value * (2**i)
    return total % 11 % 10


def is_valid_container_no(container_no: str) -> bool:
    if len(container_no) != 11:
        return False
    owner, serial, check = container_no[:4], container_no[4:10], container_no[10]
    if not (owner.isalpha() and serial.isdigit() and check.isdigit()):
        return False
    return iso6346_check_digit(container_no[:10]) == int(check)


def char_distance(a: str, b: str) -> int:
    """Edit distance for equal-length ids: substitutions count 1 each, and a swap of
    two neighbouring characters (…845 vs …485) also counts as a single error."""
    if len(a) != len(b):
        return max(len(a), len(b))
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]:
        return 1
    return len(diffs)


# ---------------------------------------------------------------- engine -----
class _Collector:
    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(self, **kwargs) -> None:
        self.items.append(Finding(id=f"F{len(self.items) + 1:03d}", **kwargs))


LABEL = {
    "manifest": "Manifest",
    "container_list": "Container list",
    "proforma_loading": "Proforma loading",
    "stacking_list": "Stacking list",
}

MISSING_SEVERITY = {
    "container_list": Severity.HIGH,
    "proforma_loading": Severity.MEDIUM,
    "stacking_list": Severity.MEDIUM,
}

FIELD_SEVERITY = {"pod": Severity.HIGH, "seal_no": Severity.MEDIUM, "size_type": Severity.MEDIUM}
FIELD_LABEL = {"pod": "POD", "seal_no": "Seal", "size_type": "Size/type"}


def reconcile(
    docs: dict[str, LoadedDocument],
    registered_npwp: set[str],
    config: ReconConfig,
) -> list[Finding]:
    out = _Collector()
    manifest = docs["manifest"].frame
    others = {name: d.frame for name, d in docs.items() if name != "manifest"}

    # 1. Duplicates inside a single document.
    for name, doc in docs.items():
        dup = doc.frame["container_no"][doc.frame["container_no"].duplicated()].unique()
        for cn in dup:
            out.add(
                container_no=cn, rule="duplicate_in_document", severity=Severity.HIGH,
                message=f"Kontainer muncul lebih dari sekali di {LABEL[name]}",
                sources=[LABEL[name]], suggestion="Hapus baris ganda atau cek apakah salah ketik nomor",
            )

    # 2. ISO 6346 check digit (catches most typos before anything else).
    all_ids = set(manifest["container_no"]).union(*(set(f["container_no"]) for f in others.values()))
    for cn in sorted(all_ids):
        if not is_valid_container_no(cn):
            where = [LABEL[n] for n, d in docs.items() if cn in set(d.frame["container_no"])]
            out.add(
                container_no=cn, rule="invalid_check_digit", severity=Severity.MEDIUM,
                message="Nomor kontainer tidak valid menurut check digit ISO 6346 (kemungkinan salah ketik)",
                sources=where, suggestion="Cocokkan dengan nomor fisik di EIR atau foto kontainer",
            )

    # 3. Presence across documents, with look-alike detection.
    manifest_ids = set(manifest["container_no"])
    for name, frame in others.items():
        other_ids = set(frame["container_no"])
        only_manifest = sorted(manifest_ids - other_ids)
        only_other = sorted(other_ids - manifest_ids)
        paired_other: set[str] = set()
        for cn in only_manifest:
            twin = next(
                (o for o in only_other if o not in paired_other
                 and char_distance(cn, o) <= config.lookalike_max_distance),
                None,
            )
            if twin:
                paired_other.add(twin)
                out.add(
                    container_no=cn, rule="lookalike_container", severity=Severity.HIGH,
                    message=f"Nomor mirip tapi tidak sama: {cn} di Manifest vs {twin} di {LABEL[name]}",
                    sources=["Manifest", LABEL[name]], expected=cn, actual=twin,
                    suggestion="Kemungkinan salah ketik satu karakter; konfirmasi nomor yang benar",
                )
            else:
                out.add(
                    container_no=cn, rule=f"missing_in_{name}", severity=MISSING_SEVERITY[name],
                    message=f"Ada di Manifest tetapi tidak ada di {LABEL[name]}",
                    sources=["Manifest", LABEL[name]],
                    suggestion=f"Cek apakah kontainer batal muat atau terlewat di {LABEL[name]}",
                )
        for cn in only_other:
            if cn in paired_other:
                continue
            out.add(
                container_no=cn, rule="missing_in_manifest", severity=Severity.HIGH,
                message=f"Ada di {LABEL[name]} tetapi tidak ada di Manifest (manifest terlewat)",
                sources=[LABEL[name], "Manifest"],
                suggestion="Tambahkan ke manifest setelah konfirmasi ke CS/shipper",
            )

    # 4. Field-by-field comparison for containers present in both.
    m_idx = manifest.drop_duplicates("container_no").set_index("container_no")
    for name, frame in others.items():
        o_idx = frame.drop_duplicates("container_no").set_index("container_no")
        common = sorted(set(m_idx.index) & set(o_idx.index))
        for cn in common:
            m_row, o_row = m_idx.loc[cn], o_idx.loc[cn]
            for field in config.compare_fields:
                if field not in o_idx.columns or field not in m_idx.columns:
                    continue
                a, b = m_row[field], o_row[field]
                if a and b and a != b:
                    out.add(
                        container_no=cn, rule=f"{field}_mismatch", severity=FIELD_SEVERITY[field],
                        message=f"{FIELD_LABEL[field]} berbeda: Manifest {a} vs {LABEL[name]} {b}",
                        sources=["Manifest", LABEL[name]], expected=a, actual=b,
                        suggestion="Tanya POL dokumen mana yang benar sebelum closing",
                    )
            if "gross_weight_kg" in o_idx.columns:
                w_m, w_o = m_row["gross_weight_kg"], o_row["gross_weight_kg"]
                if pd.notna(w_m) and pd.notna(w_o):
                    diff = abs(w_m - w_o)
                    pct = diff / max(w_m, 1) * 100
                    if diff > config.weight_tolerance_kg and pct > config.weight_tolerance_pct:
                        out.add(
                            container_no=cn, rule="weight_mismatch", severity=Severity.MEDIUM,
                            message=(f"Berat selisih {diff:,.0f} kg ({pct:.1f}%): "
                                     f"Manifest {w_m:,.0f} vs {LABEL[name]} {w_o:,.0f}"),
                            sources=["Manifest", LABEL[name]],
                            expected=f"{w_m:,.0f}", actual=f"{w_o:,.0f}",
                            suggestion="Konfirmasi VGM ke shipper",
                        )

    # 5. NPWP checks on the manifest.
    if "npwp" in manifest.columns:
        for _, row in manifest.drop_duplicates("container_no").iterrows():
            npwp, cn = row["npwp"], row["container_no"]
            if not npwp:
                out.add(container_no=cn, rule="npwp_missing", severity=Severity.MEDIUM,
                        message="NPWP kosong di Manifest", sources=["Manifest"],
                        suggestion="Minta NPWP ke shipper")
            elif len(npwp) != config.npwp_length:
                out.add(container_no=cn, rule="npwp_invalid_length", severity=Severity.MEDIUM,
                        message=f"NPWP {len(npwp)} digit, seharusnya {config.npwp_length}",
                        sources=["Manifest"], actual=npwp,
                        suggestion="Minta NPWP 16 digit terbaru ke shipper")
            elif registered_npwp and npwp not in registered_npwp:
                out.add(container_no=cn, rule="npwp_not_registered", severity=Severity.LOW,
                        message="NPWP tidak ada di master pelanggan", sources=["Manifest", "Master pelanggan"],
                        actual=npwp, suggestion="Cek apakah pelanggan baru; perbarui master data")

    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    ranked = sorted(out.items, key=lambda f: (order[f.severity], f.container_no, f.rule))
    return [f.model_copy(update={"id": f"F{i + 1:03d}"}) for i, f in enumerate(ranked)]
