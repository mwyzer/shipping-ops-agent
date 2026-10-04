"""Parsers for the messy values staff actually receive (Indonesian + English formats)."""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime

import pandas as pd

MONTHS = {
    "jan": 1, "januari": 1, "january": 1, "feb": 2, "februari": 2, "february": 2, "mar": 3, "maret": 3, "march": 3,
    "apr": 4, "april": 4, "mei": 5, "may": 5, "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7,
    "agu": 8, "agt": 8, "agustus": 8, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10, "nov": 11, "nopember": 11, "november": 11,
    "des": 12, "dec": 12, "desember": 12, "december": 12,
}


def blank(value: object) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() in {"", "nan", "NaT", "None"}


def parse_amount(value: object) -> float | None:
    """'Rp 12.500.000,00' -> 12500000.0 ; '12,500,000.50' -> 12500000.5 ; 'USD 1,250' -> 1250.0"""
    if blank(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d.,-]", "", str(value))
    if not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):  # 12.500.000,00
            text = text.replace(".", "").replace(",", ".")
        else:  # 12,500,000.00
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = text.replace(",", "") if len(parts[-1]) == 3 else text.replace(",", ".")
    elif text.count(".") > 1 or (text.count(".") == 1 and len(text.split(".")[-1]) == 3):
        text = text.replace(".", "")  # 4.250.000 or 4.250 (thousands)
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(value: object) -> date | None:
    """Day-first, like Indonesian documents: 12/09/2026, 2026-09-12, 12 Sep 2026, 12 September 2026."""
    if blank(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    text = re.sub(r"\s+00:00:00$", "", text)
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", text):
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(year, int(m[2]), int(m[1]))
        if m := re.fullmatch(r"(\d{1,2})[\s-]+([a-z]+)[\s-]+(\d{4})", text):
            month = MONTHS.get(m[2]) or MONTHS.get(m[2][:3])
            return date(int(m[3]), month, int(m[1])) if month else None
    except ValueError:  # 31/02/2026
        return None
    return None


def parse_datetime(value: object) -> datetime | None:
    if blank(value):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    d = parse_date(text)
    return datetime(d.year, d.month, d.day) if d else None


def norm_company(name: object) -> str:
    """'PT. Samudera Depo Logistik, Tbk' -> 'SAMUDERA DEPO LOGISTIK'"""
    if blank(name):
        return ""
    text = re.sub(r"[^A-Z0-9 ]", " ", str(name).upper())
    text = re.sub(r"\b(PT|CV|TBK|UD|PERSERO|LTD|PTE|SDN|BHD|CO|INC)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def closest(name: str, candidates: list[str], cutoff: float = 0.85) -> str | None:
    """Best fuzzy match on normalised company names, or None."""
    keyed = {norm_company(c): c for c in candidates}
    hit = difflib.get_close_matches(norm_company(name), list(keyed), n=1, cutoff=cutoff)
    return keyed[hit[0]] if hit else None


def norm_id(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper()) if not blank(value) else ""


def lookalike(a: str, b: str) -> bool:
    """Equal-length ids that differ by one character or one adjacent swap."""
    if a == b or len(a) != len(b):
        return False
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diffs) == 1:
        return True
    return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]


def transposed(a: str, b: str) -> bool:
    """Same characters with exactly one neighbouring pair swapped (0118 vs 0181).
    Sequential numbers (0118 vs 0119) are normal and are NOT flagged."""
    if a == b or len(a) != len(b):
        return False
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]


def rupiah(value: float | None) -> str:
    if value is None:
        return "-"
    return "Rp " + f"{value:,.0f}".replace(",", ".")


def money(value: float | None, currency: str = "IDR") -> str:
    if value is None:
        return "-"
    return rupiah(value) if currency == "IDR" else f"{currency} {value:,.2f}"


def map_columns(frame: pd.DataFrame, aliases: dict[str, list[str]]) -> tuple[pd.DataFrame, list[str]]:
    """Rename columns to canonical names using alias lists; return (frame, unmapped)."""
    key = lambda h: re.sub(r"[^a-z0-9]", "", str(h).lower())  # noqa: E731
    lookup = {key(a): canon for canon, al in aliases.items() for a in [canon, *al]}
    rename, unmapped = {}, []
    for col in frame.columns:
        canon = lookup.get(key(col))
        if canon and canon not in rename.values():
            rename[col] = canon
        else:
            unmapped.append(str(col))
    return frame.rename(columns=rename), unmapped
