r"""LangGraph workflow shared by all specialist agents.

    START -> coordinator --(agent + files)--> run_agent -> explain -> human_review -> finalize -> END
                         \--(unknown / no files)--> respond -> END

`human_review` calls `interrupt()`: the graph pauses, state is checkpointed, and it
resumes only when a person sends decisions with `Command(resume=[...])`.
`finalize` is the only node that writes files, so nothing leaves before review.
"""

from __future__ import annotations

import logging
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .agents import coordinator
from .agents.base import AgentResult, ReviewDecision, append_audit
from .agents.registry import AGENTS
from .llm import get_chat_model

log = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    request: str
    folder: str
    output_dir: str
    agent_override: str | None
    route: str
    route_reason: str
    result: dict
    narrative: str
    decisions: list[dict]
    outputs: dict[str, str]
    answer: str
    log: Annotated[list[str], operator.add]


def _files(state: AgentState) -> list[str]:
    folder = state.get("folder")
    return sorted(p.name for p in Path(folder).iterdir()) if folder and Path(folder).is_dir() else []


def coordinator_node(state: AgentState) -> dict[str, Any]:
    forced = state.get("agent_override")
    if forced:
        return {"route": forced, "route_reason": f"Dipilih langsung: {AGENTS[forced].label}.",
                "log": [f"coordinator -> {forced} (dipilih pengguna)"]}
    decision = coordinator.route(state["request"], _files(state))
    return {"route": decision.agent, "route_reason": decision.reason,
            "log": [f"coordinator -> {decision.agent}: {decision.reason}"]}


def run_agent_node(state: AgentState) -> dict[str, Any]:
    agent = AGENTS[state["route"]]
    result = agent.run(Path(state["folder"]))
    return {"result": result.model_dump(mode="json"),
            "log": [f"{agent.name}: {len(result.items)} item untuk direview"]}


def explain_node(state: AgentState) -> dict[str, Any]:
    result = AgentResult(**state["result"])
    narrative = result.facts
    model = get_chat_model()
    if model is not None and result.items:
        table = "\n".join(f"{i.id} [{i.severity.value}] {i.subject}: {i.title}" for i in result.items[:40])
        try:
            text = str(model.invoke(
                "Tulis ringkasan maksimal 4 kalimat dalam Bahasa Indonesia untuk staf operasional pelayaran yang akan "
                "meninjau hasil berikut. Sebut apa yang harus dicek dulu. Jangan menambah angka yang tidak ada.\n\n"
                f"Fakta:\n{result.facts}\n\nItem:\n{table}").content).strip()
            narrative = text or narrative
        except Exception as exc:
            log.warning("LLM explanation failed (%s); using template.", exc)
    return {"narrative": narrative, "log": ["explain: ringkasan siap untuk reviewer"]}


def human_review_node(state: AgentState) -> dict[str, Any]:
    result = AgentResult(**state["result"])
    if not result.items:
        return {"decisions": [], "log": ["human_review: tidak ada yang perlu direview"]}
    agent = AGENTS[result.agent]
    raw = interrupt({"type": "review", "agent": result.agent, "narrative": state["narrative"],
                     "items": [i.id for i in result.items],
                     "options": [o.model_dump() for o in agent.decision_options]})
    decisions = [ReviewDecision(**d) for d in raw]
    known = {i.id for i in result.items}
    unknown = [d.finding_id for d in decisions if d.finding_id not in known]
    if unknown:
        raise ValueError(f"Decisions reference unknown findings: {unknown}")
    bad = [d.decision for d in decisions if d.decision not in agent.option_keys()]
    if bad:
        raise ValueError(f"Decision not allowed for {agent.name}: {sorted(set(bad))}")
    return {"decisions": [d.model_dump() for d in decisions],
            "log": [f"human_review: {len(decisions)}/{len(known)} item diputuskan"]}


def finalize_node(state: AgentState) -> dict[str, Any]:
    result = AgentResult(**state["result"])
    out_dir = Path(state.get("output_dir") or "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions = [ReviewDecision(**d) for d in state.get("decisions", [])]
    outputs = AGENTS[result.agent].finalize(result, {d.finding_id: d for d in decisions}, out_dir)
    outputs["audit_log"] = str(append_audit(out_dir, result.agent, result.title, decisions))
    return {"outputs": outputs, "answer": state.get("narrative", ""), "log": [f"finalize: {sorted(outputs)}"]}


def respond_node(state: AgentState) -> dict[str, Any]:
    agent = AGENTS.get(state["route"])
    if agent is not None:
        answer = f"{agent.label} siap, tetapi belum ada file. {agent.description} Unggah file-nya lalu jalankan lagi."
    else:
        names = "; ".join(f"{a.label}: {a.description}" for a in AGENTS.values())
        answer = f"{state.get('route_reason', '')} Agent yang tersedia: {names}"
    return {"answer": answer.strip(), "log": [f"respond: {state['route']} (tidak ada aksi)"]}


def _after_coordinator(state: AgentState) -> str:
    return "run_agent" if state["route"] in AGENTS and _files(state) else "respond"


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("coordinator", coordinator_node)
    g.add_node("run_agent", run_agent_node)
    g.add_node("explain", explain_node)
    g.add_node("human_review", human_review_node)
    g.add_node("finalize", finalize_node)
    g.add_node("respond", respond_node)
    g.add_edge(START, "coordinator")
    g.add_conditional_edges("coordinator", _after_coordinator, {"run_agent": "run_agent", "respond": "respond"})
    g.add_edge("run_agent", "explain")
    g.add_edge("explain", "human_review")
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
