"""Runs an eval suite through the orchestrator and produces a readout.

Every eval session is a normal session (visible/replayable in the UI) tagged
with `eval_run_id`, so product metrics and eval metrics share one data model.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from typing import Any

from computeruse.evals.suite import Suite, TaskSpec, run_checker
from computeruse.server.orchestrator import Orchestrator
from computeruse.telemetry.events import SessionRecord


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def new_run_id() -> str:
    return uuid.uuid4().hex[:10]


async def run_suite(orch: Orchestrator, suite: Suite, *, model: str, repeats: int = 1,
                    task_ids: list[str] | None = None, concurrency: int = 1,
                    backend: str | None = None, progress=None, run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    backend = backend or suite.backend
    tasks = [t for t in suite.tasks if not task_ids or t.id in task_ids]
    if not tasks:
        raise ValueError("no tasks selected")
    if model == "scripted":
        missing = [t.id for t in tasks if not t.script]
        if missing:
            raise ValueError(f"tasks without a reference script cannot run with model=scripted: {missing}")
    info = orch.registry.info(backend)
    if not info.available:
        raise ValueError(f"backend {backend!r} unavailable: {info.reason}")
    orch.store.create_eval_run(run_id, suite.name, model, backend)
    sem = asyncio.Semaphore(max(1, concurrency if backend != "desktop" else 1))
    records: list[SessionRecord] = []
    started = time.time()

    async def one(task: TaskSpec, attempt: int) -> None:
        async with sem:
            async def checker(runner, result):
                return await run_checker(task.checker, runner, result)

            overrides = {"max_steps": task.max_steps, "max_duration_s": task.max_duration_s,
                         "system_prompt_extra": task.system_prompt_extra, "auto_approve": True}
            # Evals start from a clean browser unless the task says otherwise: reproducible, and they
            # never touch (or contend for) the operator's persistent logins.
            backend_options = {"profile": "ephemeral", **task.backend_options}
            rec = await orch.create_session(
                task=task.instruction, backend=backend, model=model, overrides=overrides,
                script=task.script if model == "scripted" else None, tags=["eval", *task.tags],
                backend_options=backend_options, eval_run_id=run_id, eval_task_id=task.id,
                checker=checker, metadata={"attempt": attempt, "suite": suite.name},
                guardrails=task.guardrails or None,
            )
            rec = await orch.wait(rec.id)
            records.append(rec)
            if progress:
                progress(rec)

    try:
        await asyncio.gather(*(one(t, i) for t in tasks for i in range(repeats)))
    except BaseException as e:
        summary = summarize_run(run_id, suite, model, backend, records, time.time() - started)
        summary["error"] = f"{type(e).__name__}: {e}"
        orch.store.finish_eval_run(run_id, summary, status="failed")
        raise
    summary = summarize_run(run_id, suite, model, backend, records, time.time() - started)
    orch.store.finish_eval_run(run_id, summary)
    return summary


def summarize_run(run_id: str, suite: Suite, model: str, backend: str, records: list[SessionRecord],
                  wall_s: float) -> dict[str, Any]:
    per_task: dict[str, dict[str, Any]] = {}
    per_tag: dict[str, dict[str, int]] = {}
    outcomes: Counter[str] = Counter()
    for rec in records:
        t = per_task.setdefault(rec.eval_task_id or "?", {"runs": 0, "passed": 0, "steps": [], "details": []})
        t["runs"] += 1
        t["passed"] += int(bool(rec.eval_passed))
        t["steps"].append(rec.steps)
        t["details"].append({"session_id": rec.id, "passed": rec.eval_passed, "outcome": rec.outcome.value if rec.outcome else None,
                             "detail": rec.eval_detail, "steps": rec.steps, "cost_usd": rec.cost_usd})
        outcomes[rec.outcome.value if rec.outcome else "unknown"] += 1
        spec = next((x for x in suite.tasks if x.id == rec.eval_task_id), None)
        for tag in (spec.tags if spec else []):
            g = per_tag.setdefault(tag, {"runs": 0, "passed": 0})
            g["runs"] += 1
            g["passed"] += int(bool(rec.eval_passed))
    for t in per_task.values():
        t["pass_rate"] = _rate(t["passed"], t["runs"])
        t["mean_steps"] = round(sum(t["steps"]) / len(t["steps"]), 1) if t["steps"] else None
        del t["steps"]
    for g in per_tag.values():
        g["pass_rate"] = _rate(g["passed"], g["runs"])
    passed = sum(1 for r in records if r.eval_passed)
    costs = [r.cost_usd for r in records if r.cost_usd]
    return {
        "run_id": run_id, "suite": suite.name, "backend": backend, "model": model,
        "runs": len(records), "passed": passed, "pass_rate": _rate(passed, len(records)),
        "false_completions": sum(1 for r in records if r.outcome and r.outcome.value == "completed" and r.eval_passed is False),
        "mean_steps": round(sum(r.steps for r in records) / len(records), 1) if records else None,
        "outcomes": dict(outcomes), "per_task": per_task, "per_tag": per_tag,
        "total_cost_usd": round(sum(costs), 4), "wall_s": round(wall_s, 1),
    }


def report_markdown(summary: dict[str, Any]) -> str:
    pr = summary["pass_rate"]
    lines = [
        f"# Eval run {summary['run_id']} — {summary['suite']} on {summary['backend']} with {summary['model']}",
        "",
        f"**Pass rate: {pr * 100:.1f}%** ({summary['passed']}/{summary['runs']}), mean steps {summary['mean_steps']}, "
        f"cost ${summary['total_cost_usd']:.3f}, wall {summary['wall_s']}s, false completions {summary['false_completions']}",
        "",
        "| task | runs | pass rate | mean steps | last detail |",
        "|---|---|---|---|---|",
    ]
    for tid, t in summary["per_task"].items():
        last = t["details"][-1]["detail"] if t["details"] else ""
        lines.append(f"| {tid} | {t['runs']} | {(t['pass_rate'] or 0) * 100:.0f}% | {t['mean_steps']} | {str(last)[:80]} |")
    if summary["per_tag"]:
        lines += ["", "| tag | runs | pass rate |", "|---|---|---|"]
        for tag, g in sorted(summary["per_tag"].items(), key=lambda kv: kv[1]["pass_rate"] or 0):
            lines.append(f"| {tag} | {g['runs']} | {(g['pass_rate'] or 0) * 100:.0f}% |")
    lines += ["", "Outcomes: " + ", ".join(f"{k}={v}" for k, v in summary["outcomes"].items())]
    return "\n".join(lines)
