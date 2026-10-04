"""Business rules that staff can tune without touching the engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReconConfig:
    # Gross weight difference allowed between documents, in kg AND in percent.
    # A row is flagged only when it exceeds BOTH, so small rounding is ignored.
    weight_tolerance_kg: float = 500.0
    weight_tolerance_pct: float = 2.0
    # Indonesian NPWP has 16 digits since the 2024 NIK/NPWP integration.
    npwp_length: int = 16
    # Container IDs that differ by at most this many characters are flagged as
    # "look-alike" (typo or prefix confusion), e.g. MSKU1234565 vs MSKU1234556.
    lookalike_max_distance: int = 1
    # Columns that must agree across all documents for the same container.
    compare_fields: tuple[str, ...] = field(default=("pod", "seal_no", "size_type"))


DEFAULT_CONFIG = ReconConfig()


def llm_model() -> str | None:
    """LLM is optional. Returns the model id only if an API key is also present."""
    model = os.getenv("LLM_MODEL", "").strip()
    if model and os.getenv("ANTHROPIC_API_KEY"):
        return model
    return None
