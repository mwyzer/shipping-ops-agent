"""Typed data passed between agents."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Finding(BaseModel):
    id: str = Field(description="Stable id, e.g. F001")
    container_no: str
    rule: str = Field(description="Machine name of the rule that fired")
    severity: Severity
    message: str = Field(description="What is wrong, in plain language")
    sources: list[str] = Field(description="Documents involved")
    expected: str | None = None
    actual: str | None = None
    suggestion: str = Field(description="What the agent proposes; staff decides")
