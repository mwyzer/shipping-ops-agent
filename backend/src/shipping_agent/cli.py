"""Command line entry point.

    shipping-agent serve                          # web dashboard on http://127.0.0.1:8000
    shipping-agent agents                         # list the agents
    shipping-agent demo --agent pricing           # sample data for one agent, review in the terminal
    shipping-agent demo --agent all --auto-approve
    shipping-agent run --folder path/to/files --request "cocokkan manifest dengan container list"
    shipping-agent ask "buatkan quotation rate untuk PT X"   # coordinator routing only
    shipping-agent graph                          # print the workflow as Mermaid
    shipping-agent openapi                        # write ../frontend/openapi.json for the Next.js types
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from .agents.base import AgentResult
from .agents.registry import AGENTS
from .graph import build_graph

console = Console()
SEV_STYLE = {"HIGH": "bold red", "MEDIUM": "yellow", "LOW": "dim"}


def _show(result: AgentResult, narrative: str) -> None:
    console.print(Panel(narrative, title=f"{AGENTS[result.agent].label} · {result.title}", border_style="cyan"))
    for note in result.notes:
        console.print(f"[dim]catatan: {note}[/dim]")
    table = Table(show_lines=False)
    for col in ("ID", "Urgensi", result.subject_label, "Kelompok", "Masalah", "Usulan agent"):
        table.add_column(col, overflow="fold")
    for i in result.items:
        table.add_row(i.id, f"[{SEV_STYLE[i.severity.value]}]{i.severity.value}[/]", i.subject, i.group, i.title, i.suggestion)
    console.print(table)


def _decide(result: AgentResult, auto: bool, decisions_file: Path | None, reviewer: str | None) -> list[dict]:
    if decisions_file:
        return json.loads(decisions_file.read_text(encoding="utf-8"))
    if auto:
        return [{"finding_id": i.id, "decision": "approve", "reviewer": reviewer or "auto-demo", "note": "auto-approve (demo)"}
                for i in result.items]
    options = AGENTS[result.agent].decision_options
    keys = [str(n + 1) for n in range(len(options))]
    reviewer = reviewer or Prompt.ask("Nama reviewer", default="staf")
    console.print("[bold]Keputusan:[/bold] " + ", ".join(f"{k} = {o.label}" for k, o in zip(keys, options)))
    out = []
    for i in result.items:
        if i.draft:
            console.print(Panel(i.draft, title=f"Draft {i.id}", border_style="dim"))
        k = Prompt.ask(f"{i.id} {i.subject} - {i.title}", choices=keys, default="1")
        note = Prompt.ask("  catatan (opsional)", default="") if k != "1" else ""
        out.append({"finding_id": i.id, "decision": options[int(k) - 1].key, "reviewer": reviewer, "note": note})
    return out


def run_workflow(request: str, folder: Path | None, out: Path, agent: str | None = None, auto: bool = False,
                 decisions_file: Path | None = None, reviewer: str | None = None) -> dict:
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = graph.invoke({"request": request, "folder": str(folder) if folder else "", "output_dir": str(out),
                          "agent_override": agent, "log": []}, config)
    console.print(f"[cyan]Koordinator[/cyan] -> [bold]{state['route']}[/bold] ({state['route_reason']})")
    while "__interrupt__" in state:
        result = AgentResult(**state["result"])
        _show(result, state["narrative"])
        state = graph.invoke(Command(resume=_decide(result, auto, decisions_file, reviewer)), config)
    if state.get("outputs"):
        console.print(Panel("\n".join(f"{k}: {v}" for k, v in state["outputs"].items()), title="File hasil", border_style="green"))
    elif state.get("answer"):
        console.print(Panel(state["answer"], title="Jawaban", border_style="yellow"))
    console.print("[dim]Jejak: " + " | ".join(state.get("log", [])) + "[/dim]")
    return state


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="shipping-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def review_flags(sp):
        sp.add_argument("--out", type=Path, default=Path("output"))
        sp.add_argument("--auto-approve", action="store_true", help="approve every item (demo/CI)")
        sp.add_argument("--decisions", type=Path, help="JSON file with review decisions")
        sp.add_argument("--reviewer", help="reviewer name for the audit log")

    d = sub.add_parser("demo", help="generate sample data for an agent and run it")
    d.add_argument("--agent", default="reconciliation", choices=[*AGENTS, "all"])
    d.add_argument("--data", type=Path, default=Path("data/sample"))
    review_flags(d)

    r = sub.add_parser("run", help="run on a folder of files")
    r.add_argument("--folder", type=Path, required=True)
    r.add_argument("--request", default="", help="what to do; the coordinator picks the agent")
    r.add_argument("--agent", choices=list(AGENTS), help="skip the coordinator")
    review_flags(r)

    a = sub.add_parser("ask", help="send a free-text request to the coordinator")
    a.add_argument("request")
    a.add_argument("--folder", type=Path)
    review_flags(a)

    s = sub.add_parser("serve", help="start the web dashboard (FastAPI)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--work-dir", type=Path, default=Path("output/web"))

    o = sub.add_parser("openapi", help="write the API schema (used to generate the Next.js types)")
    o.add_argument("--out", type=Path, default=Path("../frontend/openapi.json"))

    sub.add_parser("agents", help="list the agents")
    sub.add_parser("graph", help="print the LangGraph workflow as Mermaid")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        console.print(f"[green]Dashboard:[/green] http://{args.host}:{args.port}  (API docs: /docs)")
        uvicorn.run(create_app(args.work_dir), host=args.host, port=args.port)
    elif args.cmd == "openapi":
        import tempfile

        from .api import create_app

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(create_app(tempfile.mkdtemp()).openapi(), indent=2), encoding="utf-8")
        console.print(f"[green]Schema API ditulis ke[/green] {args.out}")
    elif args.cmd == "agents":
        for agent in AGENTS.values():
            console.print(f"[bold]{agent.name}[/bold] · {agent.label}: {agent.description}")
    elif args.cmd == "graph":
        print(build_graph().get_graph().draw_mermaid())
    elif args.cmd == "demo":
        for name in (AGENTS if args.agent == "all" else [args.agent]):
            agent = AGENTS[name]
            folder = agent.demo(args.data / name)
            console.rule(f"{agent.label} · data contoh: {folder}")
            run_workflow(agent.demo_request, folder, args.out / name, None, args.auto_approve, args.decisions, args.reviewer)
    elif args.cmd == "run":
        run_workflow(args.request, args.folder, args.out, args.agent, args.auto_approve, args.decisions, args.reviewer)
    elif args.cmd == "ask":
        run_workflow(args.request, args.folder, args.out, None, args.auto_approve, args.decisions, args.reviewer)


if __name__ == "__main__":
    main()
