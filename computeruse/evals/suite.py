"""Eval harness: task suites with programmatic checkers.

A suite is YAML: a backend, defaults, and tasks. Each task has a natural-language
instruction (what the model sees), tags, limits, a checker that inspects ground
truth after the run, and optionally a `script` — a reference solution for the
scripted model client so the harness itself can be regression-tested offline.

Checkers
  sim_state   path/equals/contains/truthy against SimulatedComputer.state()
  shell       run a command in the computer; pass if exit code 0 (+ optional output contains)
  chrome_tab  url_contains / title_contains via Chrome DevTools (browser backend)
  final_text  contains / regex on the model's final message
  all / any   composites
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from computeruse.agent.loop import LoopResult

SUITES_DIR = Path(__file__).parent / "suites"


@dataclass
class TaskSpec:
    id: str
    instruction: str
    checker: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    max_steps: int | None = None
    max_duration_s: float | None = None
    script: list[dict[str, Any]] | None = None
    backend_options: dict[str, Any] = field(default_factory=dict)
    system_prompt_extra: str | None = None
    guardrails: dict[str, Any] = field(default_factory=dict)  # per-session policy overrides


@dataclass
class Suite:
    name: str
    backend: str
    tasks: list[TaskSpec]
    description: str = ""
    path: Path | None = None

    def task(self, task_id: str) -> TaskSpec:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)


def load_suite(path: Path | str) -> Suite:
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    tasks = [TaskSpec(
        id=t["id"], instruction=t["instruction"], checker=t.get("checker") or {"type": "completed"},
        tags=list(t.get("tags", [])), max_steps=t.get("max_steps"), max_duration_s=t.get("max_duration_s"),
        script=t.get("script"), backend_options=t.get("backend_options", {}),
        system_prompt_extra=t.get("system_prompt_extra"), guardrails=t.get("guardrails") or {},
    ) for t in data.get("tasks", [])]
    return Suite(name=data.get("name", path.stem), backend=data.get("backend", "simulated"), tasks=tasks,
                 description=data.get("description", ""), path=path)


def list_suites(extra_dirs: list[Path] | None = None) -> list[Suite]:
    dirs = [SUITES_DIR, *(extra_dirs or [])]
    suites: list[Suite] = []
    for d in dirs:
        if d.exists():
            for p in sorted(d.glob("*.yaml")):
                try:
                    suites.append(load_suite(p))
                except Exception as e:  # noqa: BLE001
                    print(f"warning: could not load suite {p}: {e}")
    return suites


def find_suite(name: str, extra_dirs: list[Path] | None = None) -> Suite:
    p = Path(name)
    if p.suffix == ".yaml" and p.exists():
        return load_suite(p)
    for s in list_suites(extra_dirs):
        if s.name == name:
            return s
    raise KeyError(f"suite {name!r} not found")


# -- checkers -----------------------------------------------------------------


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)]
        else:
            return None
    return obj


async def run_checker(spec: dict[str, Any], runner: Any, result: LoopResult) -> tuple[bool | None, str]:
    """Returns (passed, detail). `None` means the checker could not decide."""
    kind = spec.get("type", "completed")
    if kind == "completed":
        ok = result.outcome.value == "completed"
        return ok, f"outcome={result.outcome.value}"
    if kind in ("all", "any"):
        results = [await run_checker(c, runner, result) for c in spec.get("checks", [])]
        verdicts = [r[0] for r in results]
        detail = "; ".join(f"{c.get('type')}:{'pass' if v else 'fail' if v is False else '?'} ({d})"
                           for c, (v, d) in zip(spec.get("checks", []), results, strict=False))
        if kind == "all":
            return (all(v is True for v in verdicts) if verdicts else False), detail
        return any(v is True for v in verdicts), detail
    if kind == "sim_state":
        state_fn = getattr(runner.computer, "state", None)
        if not callable(state_fn):
            return None, "sim_state checker requires the simulated backend"
        value = _dig(state_fn(), spec["path"])
        if "equals" in spec:
            ok = value == spec["equals"]
            return ok, f"{spec['path']}={value!r} (expected {spec['equals']!r})"
        if "contains" in spec:
            ok = isinstance(value, (str, list)) and spec["contains"] in value
            return ok, f"{spec['path']}={value!r} (expected to contain {spec['contains']!r})"
        if "regex" in spec:
            ok = isinstance(value, str) and re.search(spec["regex"], value) is not None
            return ok, f"{spec['path']}={value!r} (regex {spec['regex']!r})"
        return bool(value), f"{spec['path']}={value!r} (truthy)"
    if kind == "shell":
        try:
            code, out = await runner.computer.run_shell(spec["command"], timeout=float(spec.get("timeout", 30)))
        except Exception as e:  # noqa: BLE001
            return None, f"shell checker unavailable: {e}"
        ok = code == 0 and (spec.get("contains") is None or spec["contains"] in out)
        return ok, f"exit={code} output={out.strip()[:200]!r}"
    if kind == "chrome_tab":
        url = getattr(runner.computer, "devtools_url", None)
        via_daemon = getattr(runner.computer, "devtools_tabs", None)
        tabs = None
        try:
            if url:  # local browser backend: DevTools port is reachable from the host
                async with httpx.AsyncClient(timeout=5) as client:
                    tabs = (await client.get(f"{url}/json")).json()
            elif callable(via_daemon):  # remote / docker sandbox: the daemon proxies /json
                tabs = await via_daemon()
        except Exception as e:  # noqa: BLE001
            return None, f"DevTools unreachable: {e}"
        if tabs is None:
            return None, "chrome_tab checker needs a Chrome with --remote-debugging-port (browser/remote/docker)"
        pages = [t for t in tabs if t.get("type") == "page"]
        hay = " | ".join(f"{t.get('title','')} <{t.get('url','')}>" for t in pages)
        ok = True
        if "url_contains" in spec:
            ok = ok and any(spec["url_contains"] in (t.get("url") or "") for t in pages)
        if "title_contains" in spec:
            ok = ok and any(spec["title_contains"].lower() in (t.get("title") or "").lower() for t in pages)
        if "url_regex" in spec:
            ok = ok and any(re.search(spec["url_regex"], t.get("url") or "") for t in pages)
        return ok, f"tabs: {hay[:300]}"
    if kind == "final_text":
        text = result.final_text or ""
        if "contains" in spec:
            ok = spec["contains"].lower() in text.lower()
            return ok, f"final text {'contains' if ok else 'lacks'} {spec['contains']!r}"
        if "regex" in spec:
            ok = re.search(spec["regex"], text, re.I) is not None
            return ok, f"final text regex {spec['regex']!r} -> {ok}"
        return bool(text), "final text present" if text else "no final text"
    if kind == "guardrail":
        # Did the harness intervene? Counts recorded guardrail decisions for this session.
        events = runner.orch.store.get_events(runner.record.id, types=["guardrail.decision"])
        want_decision, want_rule = spec.get("decision", "block"), spec.get("rule")
        hits = [e for e in events if e.data.get("decision") == want_decision
                and (not want_rule or e.data.get("rule") == want_rule)]
        ok = len(hits) >= int(spec.get("min_count", 1))
        reasons = "; ".join(str(e.data.get("reason")) for e in hits[:3])
        return ok, f"{len(hits)} {want_decision} decision(s){' by ' + want_rule if want_rule else ''}: {reasons[:200]}"
    return None, f"unknown checker type {kind!r}"


def suite_to_json(suite: Suite) -> dict[str, Any]:
    return {
        "name": suite.name, "backend": suite.backend, "description": suite.description,
        "path": str(suite.path) if suite.path else None,
        "tasks": [{
            "id": t.id, "instruction": t.instruction, "tags": t.tags, "max_steps": t.max_steps,
            "checker": t.checker, "has_script": bool(t.script), "script": t.script, "guardrails": t.guardrails,
        } for t in suite.tasks],
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
