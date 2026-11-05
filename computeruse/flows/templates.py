"""Starter flows for common multi-agent orchestration patterns.

* Iterative loop — a director hands a computer agent one task at a time and a critic sends the work
  back with feedback until it passes; a journal file carries progress between rounds.
* Verifiable goal — research, then a hypothesis with a prediction, a test by a computer agent, an
  observation compared with the prediction, and a critic that loops back to a new hypothesis.
* Fan out & condense — parallel sub-agents whose findings a join collects and a summarizer condenses,
  so the agent that answers keeps a small context.
* Parallel workers (offline) — two scripted computer agents on the simulated desktop, joined; runs with
  no model or credentials, which makes it the quickest way to see a flow run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from computeruse.flows.spec import ROLES

Graph = tuple[list[dict[str, Any]], list[dict[str, Any]]]

# Layout grid, in canvas units: node cards are 224 wide (see web/src/flow/graph.ts) and columns leave a
# 48-unit gutter for wires. Four columns keep a template legible when it is fitted to the canvas, so
# longer chains wrap onto a second row instead of growing wider.
COL = (40, 312, 584, 856)


def _node(node_id: str, type_: str, x: float, y: float, label: str = "", **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": type_, "label": label, "pos": [x, y], "config": config, "muted": False}


def _wire(source: str, source_port: str, target: str, target_port: str) -> dict[str, Any]:
    return {"id": f"{source}.{source_port}>{target}.{target_port}", "source": source, "source_port": source_port,
            "target": target, "target_port": target_port}


def _role(role: str, extra: str = "") -> dict[str, Any]:
    text = ROLES[role][2]
    return {"role": role, "instructions": f"{text}\n\n{extra}".strip() if extra else text}


def _example_goal(backend: str) -> str:
    if backend == "simulated":
        return "Use the Calculator to compute 12 * 7 and report the result shown on screen."
    return ("Open en.wikipedia.org, find the current population of Paris and report the figure together with "
            "the year it refers to.")


def iterative_loop(backend: str) -> Graph:
    c0, c1, c2, c3 = COL
    # The worker sits a little below the director → critic line so the goal's wire into the critic and the
    # critic's loop back to the director stay in view instead of running behind cards; the journal sits
    # top right, where the goal, the director and the critic can all reach it.
    nodes = [
        _node("goal", "goal", c0, 0, text=_example_goal(backend)),
        _node("journal", "journal", c3, 0, "journal.md",
              seed="# Journal\nuser_request, progress and dead ends, appended as the run goes.\n"),
        _node("note", "note", c0, 232, text="Director → worker → critic, looping until the critic passes. The journal "
              "keeps progress and dead ends outside any one agent's context; the critic stops after its max rounds."),
        _node("director", "model", c1, 256, "Director", **_role("director")),
        _node("worker", "computer", c2, 400, "Worker", backend=backend),
        _node("critic", "critic", c3, 256, "Critic"),
        _node("output", "output", c3, 560, "Completed"),
    ]
    edges = [
        _wire("goal", "task", "director", "in"),
        _wire("goal", "task", "critic", "context"),
        _wire("goal", "task", "journal", "append"),
        _wire("director", "out", "worker", "task"),
        _wire("director", "out", "journal", "append"),
        _wire("worker", "result", "critic", "in"),
        _wire("worker", "screen", "critic", "screen"),
        _wire("worker", "screen", "output", "screen"),
        _wire("critic", "pass", "output", "in"),
        _wire("critic", "revise", "director", "in"),
        _wire("critic", "revise", "journal", "append"),
        _wire("journal", "context", "director", "context"),
    ]
    return nodes, edges


def verifiable_goal(backend: str) -> Graph:
    c0, c1, c2, c3 = COL
    # Two rows: research → hypothesis → test along the top, observe → critic → output underneath. The
    # journal holds the request, every hypothesis and every observation; the planner and the critic read it.
    nodes = [
        _node("goal", "goal", c0, 0, text=_example_goal(backend)),
        _node("journal", "journal", c1, 0, "journal.md"),
        _node("note", "note", c0, 240, text="Research → hypothesis & prediction → test → observe → critic. A rejected "
              "round goes back to a new hypothesis; the journal gives the planner and the critic the request, every "
              "hypothesis and every observation."),
        _node("research", "model", c1, 240, "Research", **_role("researcher")),
        _node("planner", "model", c2, 240, "Hypothesis", **_role("planner")),
        _node("worker", "computer", c3, 240, "Test", backend=backend),
        _node("observer", "model", c1, 680, "Observe", **_role("observer")),
        _node("critic", "critic", c2, 680, "Critic"),
        _node("output", "output", c3, 680, "Completed"),
    ]
    edges = [
        _wire("goal", "task", "research", "in"),
        _wire("goal", "task", "journal", "append"),
        _wire("research", "out", "planner", "in"),
        _wire("planner", "out", "worker", "task"),
        _wire("planner", "out", "observer", "context"),
        _wire("planner", "out", "journal", "append"),
        _wire("worker", "result", "observer", "in"),
        _wire("worker", "screen", "critic", "screen"),
        _wire("worker", "screen", "output", "screen"),
        _wire("observer", "out", "critic", "in"),
        _wire("observer", "out", "journal", "append"),
        _wire("critic", "pass", "output", "in"),
        _wire("critic", "revise", "planner", "in"),
        _wire("journal", "context", "planner", "context"),
        _wire("journal", "context", "critic", "context"),
    ]
    return nodes, edges


def fan_out(backend: str) -> Graph:
    c0, c1, c2, c3 = COL
    angles = {"a": ("Research A", "Focus on facts and figures."),
              "b": ("Research B", "Focus on background and context."),
              "c": ("Research C", "Focus on risks, caveats and open questions.")}
    # The summarizer sits above the join so the request's wire into it runs clear along the top.
    nodes = [
        _node("goal", "goal", c0, 0, text="What should someone know before adopting a four-day work week?"),
        _node("note", "note", c0, 232, text="Expand → condense: researchers run in parallel, the join waits for all "
              "of them, and the summarizer answers with only their findings and the request in context."),
        *[_node(f"research_{k}", "model", c1, 120 + i * 272, label, **_role("researcher", extra))
          for i, (k, (label, extra)) in enumerate(angles.items())],
        _node("join", "join", c2, 392, "Join"),
        _node("summary", "model", c3, 120, "Summarizer", **_role("summarizer")),
        _node("output", "output", c3, 440, "Answer"),
    ]
    edges = [
        *[_wire("goal", "task", f"research_{k}", "in") for k in angles],
        *[_wire(f"research_{k}", "out", "join", "in") for k in angles],
        _wire("join", "out", "summary", "in"),
        _wire("goal", "task", "summary", "context"),
        _wire("summary", "out", "output", "in"),
    ]
    return nodes, edges


def offline_demo(backend: str) -> Graph:  # noqa: ARG001 - always the simulated desktop
    c0, c1, c2, c3 = COL
    nodes = [
        _node("goal", "goal", c0, 0, text="Run both SimOS demo tasks and report what each agent did."),
        _node("note", "note", c0, 232, text="Runs offline: two computer agents replay reference scripts on the "
              "simulated desktop in parallel; the join collects both reports and the journal records them."),
        _node("calc", "computer", c1, 0, "Calculator agent", backend="simulated",
              demo_task="simulated-basics/calc_multiply"),
        _node("notes", "computer", c1, 440, "Notes agent", backend="simulated",
              demo_task="simulated-basics/notes_type_save"),
        _node("join", "join", c2, 264, "Join"),
        _node("journal", "journal", c2, 560, "journal.md"),
        _node("output", "output", c3, 120, "Report"),
    ]
    edges = [
        _wire("goal", "task", "calc", "task"),
        _wire("goal", "task", "notes", "task"),
        _wire("calc", "result", "join", "in"),
        _wire("notes", "result", "join", "in"),
        _wire("calc", "result", "journal", "append"),
        _wire("notes", "result", "journal", "append"),
        _wire("calc", "screen", "output", "screen"),
        _wire("join", "out", "output", "in"),
    ]
    return nodes, edges


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    needs_antigravity: bool
    build: Callable[[str], Graph]

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "description": self.description, "tags": list(self.tags),
                "needs_antigravity": self.needs_antigravity}


TEMPLATES: dict[str, Template] = {t.id: t for t in (
    Template("iterative-loop", "Iterative loop",
             "A director hands a computer agent one task at a time; a critic checks each report and screen "
             "against a rubric and sends it back with feedback until it passes.",
             ("director", "worker", "critic", "journal"), True, iterative_loop),
    Template("verifiable-goal", "Verifiable goal",
             "Research, then a hypothesis with a prediction, a test on the computer, an observation compared "
             "with the prediction, and a critic that loops back to a new hypothesis.",
             ("research", "hypothesis", "test", "observe", "critic"), True, verifiable_goal),
    Template("fan-out", "Fan out & condense",
             "Three researchers run in parallel, a join waits for all of them, and a summarizer condenses "
             "their findings with the request in context.",
             ("parallel", "join", "summary"), True, fan_out),
    Template("offline-demo", "Parallel workers (offline)",
             "Two scripted computer agents run side by side on the simulated desktop and their reports are "
             "joined. Needs no model or credentials.",
             ("parallel", "computer agents", "no credentials"), False, offline_demo),
)}
