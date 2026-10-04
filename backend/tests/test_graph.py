import json
import uuid

import pytest
from langgraph.types import Command

from shipping_agent.agents.coordinator import route
from shipping_agent.agents.registry import AGENTS
from shipping_agent.graph import build_graph


@pytest.mark.parametrize("text, agent", [
    ("Tolong cek manifest dan cocokkan dengan container list", "reconciliation"),
    ("cek selisih NPWP di manifest", "reconciliation"),
    ("rapikan tagihan vendor ke format impor accounting", "data_entry"),
    ("buatkan weekly report lifting", "reporting"),
    ("kirim NOA ke semua consignee lewat email", "email"),
    ("buatkan quotation rate untuk pelanggan baru", "pricing"),
    ("kapal delay, closing CY berubah", "schedule"),
    ("apa kabar?", "unknown"),
])
def test_coordinator_routes_by_request(text, agent):
    assert route(text).agent == agent


@pytest.mark.parametrize("name", list(AGENTS))
def test_coordinator_routes_by_files_alone(name, tmp_path):
    folder = AGENTS[name].demo(tmp_path)
    assert route("", [p.name for p in folder.iterdir()]).agent == name


def _start(tmp_path, request, folder, agent=None):
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    out = tmp_path / "out"
    state = graph.invoke({"request": request, "folder": str(folder) if folder else "", "output_dir": str(out),
                          "agent_override": agent, "log": []}, config)
    return graph, config, state, out


@pytest.mark.parametrize("name", list(AGENTS))
def test_every_agent_pauses_for_review_then_finalizes(name, tmp_path):
    agent = AGENTS[name]
    graph, config, state, out = _start(tmp_path, agent.demo_request, agent.demo(tmp_path / "data"))
    assert state["route"] == name
    assert "__interrupt__" in state
    assert not out.exists(), "no output may be written before a person reviews"
    items = state["result"]["items"]
    last = agent.decision_options[-1].key
    final = graph.invoke(Command(resume=[{"finding_id": i["id"], "decision": last if n % 2 else "approve", "reviewer": "tester"}
                                         for n, i in enumerate(items)]), config)
    assert "report" in final["outputs"] and "audit_log" in final["outputs"]
    audit = [json.loads(line) for line in open(final["outputs"]["audit_log"], encoding="utf-8")]
    assert len(audit) == len(items) and {a["agent"] for a in audit} == {name}


def test_agent_override_skips_coordinator(tmp_path):
    folder = AGENTS["pricing"].demo(tmp_path / "data")
    _, _, state, _ = _start(tmp_path, "apa saja", folder, agent="pricing")
    assert state["route"] == "pricing" and "__interrupt__" in state


def test_resume_rejects_unknown_item_and_wrong_option(voyage, tmp_path):
    folder, _ = voyage
    graph, config, state, _ = _start(tmp_path, "cek manifest", folder)
    with pytest.raises(ValueError, match="unknown findings"):
        graph.invoke(Command(resume=[{"finding_id": "F999", "decision": "approve", "reviewer": "x"}]), config)
    graph, config, state, _ = _start(tmp_path, "cek manifest", folder)
    with pytest.raises(ValueError, match="not allowed"):
        graph.invoke(Command(resume=[{"finding_id": "F001", "decision": "ask_vendor", "reviewer": "x"}]), config)


def test_known_agent_without_files_answers(tmp_path):
    _, _, state, out = _start(tmp_path, "buatkan quotation rate", None)
    assert state["route"] == "pricing" and "belum ada file" in state["answer"]
    assert "result" not in state and not out.exists()


def test_unknown_request_lists_agents(tmp_path):
    _, _, state, _ = _start(tmp_path, "apa kabar?", None)
    assert state["route"] == "unknown"
    assert all(a.label in state["answer"] for a in AGENTS.values())
