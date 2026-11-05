"""Agent flows: graphs of agents whose outputs are wired to other agents' inputs.

A flow is a small directed graph drawn on the console's Agents tab. Nodes are agents (a computer-use
agent that runs a session, a tool-less model agent with a role, a critic), plumbing (join) and memory
(a journal file); edges carry messages from an output port to an input port.

Ports are typed (`text` or `screen`) and a wire may only join ports of the same type. Every input port
is either a *trigger* — a message arriving there starts the node's work, one message at a time — or a
*latch*: the latest message from each connected source is kept and used as context the next time the
node is triggered. That one distinction is what lets a journal feed every agent without waking them
all up, and lets a critic look at a worker's final screen when the worker's report arrives.

This module is the single source of truth for node types and graph validity; the web UI renders its
palette, ports and settings forms from `catalog()`.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_NODES = 200
MAX_EDGES = 600
MAX_TEXT = 20_000
MAX_COORD = 1_000_000.0

PORT_TYPES: dict[str, dict[str, str]] = {
    "text": {"label": "Text", "doc": "A message: a task, a report, feedback, notes or the journal."},
    "screen": {"label": "Screen", "doc": "The final screenshot of a computer agent's session."},
}

# -- roles for model agents --------------------------------------------------------------------------

DIRECTOR = """\
You direct a small team of agents working toward the goal you are given. Each time you are called, \
read the latest report, the critic's feedback and the journal in your context, then decide the single \
next concrete task for the computer agent. Reply with that task only: one short paragraph written as \
an instruction to the agent, ending with what it should report back. Do not do the work yourself."""

RESEARCHER = """\
You research the question you are given using what you already know. Reply with concise, factual \
findings as a short bullet list, and say where you are unsure. Do not ask follow-up questions."""

PLANNER = """\
You turn the goal and what is known so far into a testable step. Reply with three lines: \
"Hypothesis:" what you believe, "Prediction:" what a computer agent should observe if you are right, \
and "Task:" one instruction for the computer agent that will test it. If you are given feedback from \
a failed round, change the hypothesis rather than repeating it."""

OBSERVER = """\
You compare what a computer agent observed (its report and, when attached, its final screenshot) with \
the prediction in your context. Reply with "Observed:" what actually happened, "Matches prediction:" \
yes or no, and one short paragraph explaining the difference."""

SUMMARIZER = """\
You condense the inputs you are given into one clear answer for the user. Keep the facts, drop the \
process, and point out where the sources disagree. Reply with a few short paragraphs or a bullet list."""

CRITIC = """\
You are a strict critic. Judge whether the work you are given meets the rubric, using the goal in \
your context and, when one is attached, the screenshot as evidence. The first line of your reply must \
be exactly "VERDICT: PASS" or "VERDICT: REVISE". Then give a short justification; for REVISE, say \
precisely what should be done differently in the next round."""

# role id -> (label, kind of the messages it sends, instructions)
ROLES: dict[str, tuple[str, str, str]] = {
    "director": ("Director", "task", DIRECTOR),
    "researcher": ("Researcher", "findings", RESEARCHER),
    "planner": ("Hypothesis & prediction", "plan", PLANNER),
    "observer": ("Observer", "observation", OBSERVER),
    "summarizer": ("Summarizer", "summary", SUMMARIZER),
    "custom": ("Custom", "message", ""),
}

# -- node types ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Port:
    name: str
    type: str  # a key of PORT_TYPES
    label: str
    trigger: bool = True  # inputs only: False = latch (kept as context, does not start work)
    doc: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "label": self.label, "trigger": self.trigger, "doc": self.doc}


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    kind: str  # text | textarea | number | select | role | model | computer_model | backend | demo_task
    default: Any = ""
    options: tuple[tuple[str, str], ...] = ()
    min: float | None = None
    max: float | None = None
    placeholder: str = ""
    doc: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default,
                               "placeholder": self.placeholder, "doc": self.doc}
        if self.options:
            out["options"] = [{"value": v, "label": label} for v, label in self.options]
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        return out


@dataclass(frozen=True)
class NodeType:
    type: str
    title: str
    desc: str
    category: str  # start | agent | flow | memory | end | note
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    fields: tuple[ConfigField, ...] = ()
    agent: bool = False  # every invocation counts against the run's agent-call budget
    needs_antigravity: bool = False

    def input(self, name: str) -> Port | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str) -> Port | None:
        return next((p for p in self.outputs if p.name == name), None)

    def field(self, key: str) -> ConfigField | None:
        return next((f for f in self.fields if f.key == key), None)

    def to_json(self, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
        fields = []
        for f in self.fields:
            j = f.to_json()
            if defaults and f.key in defaults:
                j["default"] = defaults[f.key]
            fields.append(j)
        return {"type": self.type, "title": self.title, "desc": self.desc, "category": self.category,
                "inputs": [p.to_json() for p in self.inputs], "outputs": [p.to_json() for p in self.outputs],
                "fields": fields, "agent": self.agent, "needs_antigravity": self.needs_antigravity}


_ROLE_OPTIONS = tuple((k, v[0]) for k, v in ROLES.items())
_CONTEXT = Port("context", "text", "context", trigger=False,
                doc="Latched: the latest message from each wired source is added as context on the next run.")
_SCREEN_IN = Port("screen", "screen", "screen", trigger=False,
                  doc="Latched: the latest screenshot is attached to the next message (image-capable models).")

NODE_TYPES: dict[str, NodeType] = {t.type: t for t in (
    NodeType(
        "goal", "Goal", "The request that starts a run. Every goal sends its text when the run starts.", "start",
        outputs=(Port("task", "text", "task"),),
        fields=(ConfigField("text", "Request", "textarea", "", placeholder="What should the agents achieve?",
                            doc="Sent on the task output when the run starts."),),
    ),
    NodeType(
        "computer", "Computer agent",
        "Runs a computer-use session on each task it receives and reports the agent's final message and screen.",
        "agent",
        inputs=(Port("task", "text", "task", doc="Each message starts one session."), _CONTEXT),
        outputs=(Port("result", "text", "result", doc="The agent's final message, or why it stopped."),
                 Port("screen", "screen", "screen", doc="The session's final screenshot.")),
        fields=(
            ConfigField("backend", "Computer", "backend", "browser"),
            ConfigField("model", "Model", "computer_model", "", placeholder="Server default"),
            ConfigField("max_steps", "Max steps", "number", 20, min=1, max=5000,
                        doc="Executed actions per session."),
            ConfigField("instructions", "Instructions", "textarea", "",
                        placeholder="Extra instructions for this agent (added to its system prompt)"),
            ConfigField("demo_task", "Scripted demo", "demo_task", "",
                        doc="Replay a reference script instead of calling a model (no credentials needed)."),
        ),
        agent=True,
    ),
    NodeType(
        "model", "Model agent",
        "A tool-less Antigravity model with a role: it directs, researches, plans, observes or summarises. "
        "It can only answer; it never touches a computer.",
        "agent",
        inputs=(Port("in", "text", "in", doc="Each message is one turn."), _CONTEXT, _SCREEN_IN),
        outputs=(Port("out", "text", "out"),),
        fields=(
            ConfigField("role", "Role", "role", "director", options=_ROLE_OPTIONS),
            ConfigField("instructions", "Instructions", "textarea", DIRECTOR,
                        doc="The agent's whole system prompt. Picking a role fills it in."),
            ConfigField("model", "Model", "model", "", placeholder="Chat default"),
            ConfigField("memory", "Memory", "select", "keep",
                        options=(("keep", "Keep its conversation across rounds"),
                                 ("fresh", "Start fresh for every message"))),
        ),
        agent=True, needs_antigravity=True,
    ),
    NodeType(
        "critic", "Critic",
        "Judges each piece of work against a rubric (and the screen, when wired) and routes it: pass goes on, "
        "revise goes back with feedback. After the last round it passes the work on, marked as stopped.",
        "agent",
        inputs=(Port("in", "text", "work", doc="The work to judge."), _CONTEXT, _SCREEN_IN),
        outputs=(Port("pass", "text", "pass", doc="The accepted work."),
                 Port("revise", "text", "revise", doc="Feedback for another round.")),
        fields=(
            ConfigField("rubric", "Rubric", "textarea",
                        "The goal is fully achieved and the result is stated explicitly, with evidence."),
            ConfigField("max_rounds", "Max rounds", "number", 3, min=1, max=20,
                        doc="Stop-hook: after this many verdicts the work is passed on, marked as stopped."),
            ConfigField("model", "Model", "model", "", placeholder="Chat default"),
        ),
        agent=True, needs_antigravity=True,
    ),
    NodeType(
        "join", "Join",
        "Waits for one message on every wire into it, then sends them on together — fan-in after "
        "agents that ran in parallel.",
        "flow",
        inputs=(Port("in", "text", "in"),),
        outputs=(Port("out", "text", "out"),),
    ),
    NodeType(
        "journal", "Journal",
        "Memory as a file: appends every message it receives to journal.md and sends the whole journal to "
        "the agents wired to it, as context.",
        "memory",
        inputs=(Port("append", "text", "append"),),
        outputs=(Port("context", "text", "journal"),),
        fields=(ConfigField("seed", "Starts with", "textarea", "",
                            placeholder="e.g. user_request / progress / dead_ends headings"),),
    ),
    NodeType(
        "output", "Output", "Collects the result. The run completes once every output has received one.", "end",
        inputs=(Port("in", "text", "result"), _SCREEN_IN),
    ),
    NodeType(
        "note", "Note", "A comment on the canvas. Notes never take part in a run.", "note",
        fields=(ConfigField("text", "Text", "textarea", ""),),
    ),
)}

# -- flow documents ---------------------------------------------------------------------------------------


class FlowNode(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    type: str
    label: str = Field(default="", max_length=80)
    pos: tuple[float, float] = (0.0, 0.0)
    config: dict[str, Any] = Field(default_factory=dict)
    muted: bool = False


class FlowEdge(BaseModel):
    id: str = Field(default="", max_length=300)
    source: str
    source_port: str
    target: str
    target_port: str


class Viewport(BaseModel):
    x: float = 0.0
    y: float = 0.0
    zoom: float = Field(default=1.0, ge=0.2, le=3.0)


class FlowSettings(BaseModel):
    max_agent_calls: int = Field(default=24, ge=1, le=500)


class Flow(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    name: str = Field(min_length=1, max_length=120)
    nodes: list[FlowNode] = Field(default_factory=list, max_length=MAX_NODES)
    edges: list[FlowEdge] = Field(default_factory=list, max_length=MAX_EDGES)
    viewport: Viewport = Field(default_factory=Viewport)
    settings: FlowSettings = Field(default_factory=FlowSettings)
    template: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def node(self, node_id: str) -> FlowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)


def edge_id(e: FlowEdge) -> str:
    return f"{e.source}.{e.source_port}>{e.target}.{e.target_port}"


def node_label(n: FlowNode) -> str:
    return n.label.strip() or NODE_TYPES[n.type].title


def normalize_config(nt: NodeType, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Only the type's own fields survive, each coerced to its kind; missing values take the default."""
    raw = raw or {}
    out: dict[str, Any] = {}
    for f in nt.fields:
        v = raw.get(f.key, f.default)
        if f.kind == "number":
            try:
                num = float(v)
            except (TypeError, ValueError):
                num = float(f.default)
            if not math.isfinite(num):
                num = float(f.default)
            if f.min is not None:
                num = max(f.min, num)
            if f.max is not None:
                num = min(f.max, num)
            out[f.key] = int(num) if isinstance(f.default, int) else num
        elif f.kind in ("select", "role"):
            allowed = {o[0] for o in f.options}
            out[f.key] = str(v) if str(v) in allowed else f.default
        else:
            out[f.key] = ("" if v is None else str(v))[:MAX_TEXT]
    return out


def validate_flow(flow: Flow) -> Flow:
    """Check the graph and return a normalised copy (configs coerced, edge ids filled in).

    Raises ValueError with a message that names the offending node or wire.
    """
    seen: set[str] = set()
    nodes: list[FlowNode] = []
    for n in flow.nodes:
        if n.id in seen:
            raise ValueError(f"duplicate node id {n.id!r}")
        seen.add(n.id)
        nt = NODE_TYPES.get(n.type)
        if nt is None:
            raise ValueError(f"node {n.id!r} has unknown type {n.type!r}")
        x, y = n.pos
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"node {n.id!r} has a non-finite position")
        pos = (max(-MAX_COORD, min(MAX_COORD, x)), max(-MAX_COORD, min(MAX_COORD, y)))
        nodes.append(n.model_copy(update={"config": normalize_config(nt, n.config), "label": n.label.strip(),
                                          "pos": pos}))
    by_id = {n.id: n for n in nodes}
    edges: list[FlowEdge] = []
    keys: set[str] = set()
    for e in flow.edges:
        src, tgt = by_id.get(e.source), by_id.get(e.target)
        if src is None or tgt is None:
            raise ValueError(f"wire {edge_id(e)} references a node that does not exist")
        if src.id == tgt.id:
            raise ValueError(f"wire {edge_id(e)} connects {node_label(src)!r} to itself")
        out_port = NODE_TYPES[src.type].output(e.source_port)
        in_port = NODE_TYPES[tgt.type].input(e.target_port)
        if out_port is None:
            raise ValueError(f"{node_label(src)!r} has no output {e.source_port!r}")
        if in_port is None:
            raise ValueError(f"{node_label(tgt)!r} has no input {e.target_port!r}")
        if out_port.type != in_port.type:
            raise ValueError(f"cannot wire {node_label(src)}.{e.source_port} ({out_port.type}) to "
                             f"{node_label(tgt)}.{e.target_port} ({in_port.type}): port types differ")
        key = edge_id(e)
        if key in keys:
            raise ValueError(f"duplicate wire {key}")
        keys.add(key)
        edges.append(e.model_copy(update={"id": key}))
    return flow.model_copy(update={"nodes": nodes, "edges": edges})


def catalog(defaults: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the editor needs to draw palettes, ports and settings forms."""
    defaults = defaults or {}
    return {
        "node_types": [nt.to_json(defaults.get(nt.type)) for nt in NODE_TYPES.values()],
        "port_types": PORT_TYPES,
        "roles": [{"value": k, "label": v[0], "instructions": v[2]} for k, v in ROLES.items()],
        "limits": {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES},
    }
