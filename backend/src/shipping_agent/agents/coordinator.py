"""Agent Koordinator: decide which specialist handles a request.

Signals, strongest first: the LLM (when configured), keywords in the request,
then the names of the uploaded files. Low confidence returns "unknown" instead
of guessing, so nothing runs on the wrong data.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from pydantic import BaseModel, Field

from ..llm import get_chat_model
from .registry import AGENTS

log = logging.getLogger(__name__)


class RouteDecision(BaseModel):
    agent: str = Field(description="Name of the specialist agent, or 'unknown'")
    reason: str = Field(description="One short sentence in Bahasa Indonesia")
    scores: dict[str, float] = Field(default_factory=dict)


def _keyword_scores(request: str, filenames: Iterable[str]) -> dict[str, float]:
    text = request.lower()
    names = [f.lower() for f in filenames]
    scores: dict[str, float] = {}
    for name, agent in AGENTS.items():
        words = sum(2 for kw in agent.keywords if re.search(rf"(?<![a-z]){re.escape(kw)}", text))
        files = sum(1 for f in names if any(h in f for h in agent.file_hints))
        scores[name] = words + files
    return scores


def _route_by_llm(request: str, filenames: list[str]) -> RouteDecision | None:
    model = get_chat_model()
    if model is None:
        return None
    catalogue = "\n".join(f"- {a.name}: {a.label}. {a.description}" for a in AGENTS.values())
    prompt = ("Kamu koordinator tim AI agent operasional pelayaran. Pilih SATU agent untuk permintaan berikut, "
              f"atau 'unknown' jika tidak ada yang cocok.\n\nAgent:\n{catalogue}\n\nPermintaan: {request}\n"
              f"File: {', '.join(filenames) or '-'}")
    try:
        out = model.with_structured_output(RouteDecision).invoke(prompt)
        return out if out.agent in AGENTS or out.agent == "unknown" else None
    except Exception as exc:  # network, quota, schema: never block the workflow
        log.warning("LLM routing failed (%s); falling back to keywords.", exc)
        return None


def route(request: str, filenames: Iterable[str] = ()) -> RouteDecision:
    filenames = list(filenames)
    llm = _route_by_llm(request, filenames)
    if llm:
        return llm
    scores = _keyword_scores(request, filenames)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top == 0:
        return RouteDecision(agent="unknown", reason="Tidak ada kata kunci atau file yang dikenali.", scores=scores)
    if top == second:
        tied = [AGENTS[n].label for n, s in ranked if s == top]
        return RouteDecision(agent="unknown", reason=f"Ragu antara {', '.join(tied)}; sebutkan tugasnya lebih jelas.",
                             scores=scores)
    return RouteDecision(agent=best, reason=f"Skor {top:g} untuk {AGENTS[best].label} (berikutnya {second:g}).", scores=scores)
