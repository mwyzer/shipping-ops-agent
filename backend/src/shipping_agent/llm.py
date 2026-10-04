"""Optional LLM layer.

The system runs fully offline with rule-based fallbacks. When LLM_MODEL and
ANTHROPIC_API_KEY are set (and `pip install .[llm]`), the LLM is used for the two
language tasks only: understanding the staff request and explaining findings.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import llm_model

log = logging.getLogger(__name__)


def get_chat_model() -> Any | None:
    model = llm_model()
    if not model:
        return None
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        log.warning("LLM_MODEL is set but langchain-anthropic is not installed; using rules.")
        return None
    return ChatAnthropic(model=model, temperature=0, max_tokens=1024)
