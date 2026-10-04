"""Read voyage documents and normalise them into one schema.

Real files rarely agree on column names ("Container No", "CNTR NO", "No. Kontainer").
Each header is matched against an alias table, so a new POL format usually means
adding one alias rather than changing code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

CANONICAL_ALIASES: dict[str, list[str]] = {
    "container_no": ["container_no", "container", "containerno", "cntrno", "cntr", "nokontainer", "nocontainer", "equipment"],
    "size_type": ["size_type", "sizetype", "size", "type", "isocode", "ukuran"],
    "gross_weight_kg": ["gross_weight_kg", "grossweight", "grossweightkg", "gw", "vgm", "vgmkg", "berat", "beratkg", "weight"],
    "seal_no": ["seal_no", "seal", "sealno", "nosegel", "segel"],
    "pol": ["pol", "portofloading", "pelabuhanmuat"],
    "pod": ["pod", "portofdischarge", "destination", "pelabuhantujuan"],
    "shipper": ["shipper", "pengirim"],
    "consignee": ["consignee", "penerima"],
    "npwp": ["npwp", "taxid", "shippernpwp"],
}

# Which canonical fields each document is expected to carry.
DOCUMENT_TYPES: dict[str, list[str]] = {
    "manifest": ["container_no", "size_type", "gross_weight_kg", "seal_no", "pol", "pod", "shipper", "consignee", "npwp"],
    "container_list": ["container_no", "size_type", "gross_weight_kg", "seal_no", "pod"],
    "proforma_loading": ["container_no", "size_type", "pod"],
    "stacking_list": ["container_no", "size_type", "gross_weight_kg", "pod"],
}

FILENAME_HINTS: dict[str, list[str]] = {
    "manifest": ["manifest"],
    "container_list": ["container_list", "containerlist", "cntr_list"],
    "proforma_loading": ["proforma", "loading"],
    "stacking_list": ["stacking", "stack"],
}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


_ALIAS_LOOKUP = {_key(alias): canon for canon, aliases in CANONICAL_ALIASES.items() for alias in aliases}


def normalise_container_no(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper()) if pd.notna(value) else ""


def normalise_text(value: object) -> str:
    return str(value).strip().upper() if pd.notna(value) else ""


def normalise_digits(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    if text.endswith(".0"):  # Excel turns long numbers into floats
        text = text[:-2]
    return re.sub(r"\D", "", text)


@dataclass
class LoadedDocument:
    name: str
    path: Path
    frame: pd.DataFrame
    unmapped_columns: list[str]


def detect_document_type(path: Path) -> str | None:
    stem = path.stem.lower()
    for doc_type, hints in FILENAME_HINTS.items():
        if any(hint in stem for hint in hints):
            return doc_type
    return None


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str)


def load_document(path: Path, doc_type: str) -> LoadedDocument:
    raw = read_table(path)
    rename: dict[str, str] = {}
    unmapped: list[str] = []
    for col in raw.columns:
        canon = _ALIAS_LOOKUP.get(_key(col))
        if canon and canon not in rename.values():
            rename[col] = canon
        else:
            unmapped.append(str(col))
    frame = raw.rename(columns=rename)
    keep = [c for c in DOCUMENT_TYPES[doc_type] if c in frame.columns]
    frame = frame[keep].copy()

    frame["container_no"] = frame["container_no"].map(normalise_container_no)
    for col in ("size_type", "seal_no", "pol", "pod", "shipper", "consignee"):
        if col in frame:
            frame[col] = frame[col].map(normalise_text)
    if "npwp" in frame:
        frame["npwp"] = frame["npwp"].map(normalise_digits)
    if "gross_weight_kg" in frame:
        frame["gross_weight_kg"] = pd.to_numeric(
            frame["gross_weight_kg"].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
    frame = frame[frame["container_no"] != ""].reset_index(drop=True)
    return LoadedDocument(name=doc_type, path=path, frame=frame, unmapped_columns=unmapped)


def load_voyage_folder(folder: Path) -> dict[str, LoadedDocument]:
    docs: dict[str, LoadedDocument] = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in {".xlsx", ".xls", ".csv"}:
            continue
        doc_type = detect_document_type(path)
        if doc_type and doc_type not in docs:
            docs[doc_type] = load_document(path, doc_type)
    missing = [d for d in DOCUMENT_TYPES if d not in docs]
    if "manifest" in missing:
        raise FileNotFoundError(f"No manifest found in {folder}")
    return docs


def load_customer_master(path: Path | None) -> set[str]:
    """Registered NPWPs. Missing master file = skip the registry check."""
    if path is None or not path.exists():
        return set()
    frame = read_table(path)
    col = next((c for c in frame.columns if _ALIAS_LOOKUP.get(_key(c)) == "npwp"), None)
    if col is None:
        return set()
    return {normalise_digits(v) for v in frame[col] if normalise_digits(v)}
