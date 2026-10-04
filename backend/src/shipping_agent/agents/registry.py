"""All specialist agents, in the order they appear in the dashboard."""

from __future__ import annotations

from .base import Agent
from .data_entry import DataEntryAgent
from .email_agent import EmailAgent
from .pricing import PricingAgent
from .reconciliation import ReconciliationAgent
from .reporting import ReportingAgent
from .schedule import ScheduleAgent

AGENTS: dict[str, Agent] = {a.name: a for a in (
    ReconciliationAgent(), DataEntryAgent(), ReportingAgent(), EmailAgent(), PricingAgent(), ScheduleAgent(),
)}


def get_agent(name: str) -> Agent:
    return AGENTS[name]
