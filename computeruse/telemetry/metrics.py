"""Product metrics derived from telemetry: the numbers that drive prioritisation.

Headline: task success rate. Supporting: steps/turns per task, model and action
latency, failure taxonomy, guardrail/stuck interventions, token cost, action mix.
Everything is computed from the sessions + events tables so the dashboard, CLI
readout and eval reports agree by construction.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from typing import Any

from computeruse.telemetry.events import EventType
from computeruse.telemetry.store import Store


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def summarize(store: Store, since_s: float | None = None, backend: str | None = None,
              include_evals: bool = True) -> dict[str, Any]:
    since = time.time() - since_s if since_s else None
    sessions = store.list_sessions(limit=100000, backend=backend, since=since)
    if not include_evals:
        sessions = [s for s in sessions if not s.eval_run_id]
    ended = [s for s in sessions if s.status.terminal]
    successes = [s for s in ended if s.success]

    by_backend: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_tag: dict[str, dict[str, Any]] = {}
    for s in ended:
        for bucket, key in ((by_backend, s.backend), (by_model, s.model)):
            b = bucket.setdefault(key, {"sessions": 0, "successes": 0, "steps": []})
            b["sessions"] += 1
            b["successes"] += int(bool(s.success))
            b["steps"].append(s.steps)
        for tag in s.tags:
            b = by_tag.setdefault(tag, {"sessions": 0, "successes": 0, "steps": []})
            b["sessions"] += 1
            b["successes"] += int(bool(s.success))
            b["steps"].append(s.steps)
    for bucket in (by_backend, by_model, by_tag):
        for b in bucket.values():
            b["success_rate"] = _rate(b["successes"], b["sessions"])
            b["median_steps"] = _median(b.pop("steps"))

    outcomes = Counter((s.outcome.value if s.outcome else "unknown") for s in ended)
    failure_taxonomy = {k: v for k, v in outcomes.items() if k != "completed"}
    # Sessions the model declared complete but a checker failed are the most
    # dangerous class ("confident failures"); surface them explicitly.
    false_completions = sum(1 for s in ended if s.outcome and s.outcome.value == "completed" and s.eval_passed is False)

    model_latencies: list[float] = []
    retries = 0
    action_latencies: list[float] = []
    action_kinds: Counter[str] = Counter()
    action_errors = 0
    guardrail: Counter[str] = Counter()
    approvals = {"requested": 0, "approved": 0, "rejected": 0}
    stuck_nudges = 0
    session_ids = {s.id for s in sessions}

    for ev in store.events_by_type(EventType.MODEL_CALLED, since=since):
        if ev.session_id in session_ids:
            model_latencies.append(float(ev.data.get("latency_ms", 0)))
            retries += int(ev.data.get("retries", 0))
    for ev in store.events_by_type(EventType.ACTION_EXECUTED, since=since):
        if ev.session_id in session_ids:
            action_latencies.append(float(ev.data.get("duration_ms", 0)))
            action_kinds[str(ev.data.get("kind", "?"))] += 1
            action_errors += 0 if ev.data.get("ok", True) else 1
    for ev in store.events_by_type(EventType.GUARDRAIL_DECISION, since=since):
        if ev.session_id in session_ids and ev.data.get("decision") != "allow":
            guardrail[f"{ev.data.get('decision')}:{ev.data.get('rule')}"] += 1
    for ev in store.events_by_type(EventType.APPROVAL_RESOLVED, since=since):
        if ev.session_id in session_ids:
            approvals["requested"] += 1
            approvals["approved" if ev.data.get("approved") else "rejected"] += 1
    for ev in store.events_by_type(EventType.STUCK_NUDGED, since=since):
        if ev.session_id in session_ids:
            stuck_nudges += 1

    durations = [s.duration_ms / 1000 for s in ended if s.duration_ms]
    costs = [s.cost_usd for s in ended if s.cost_usd is not None]
    return {
        "window_s": since_s,
        "sessions_total": len(sessions),
        "sessions_ended": len(ended),
        "sessions_active": len(sessions) - len(ended),
        "success_rate": _rate(len(successes), len(ended)),
        "evaluated_sessions": sum(1 for s in ended if s.eval_passed is not None),
        "false_completions": false_completions,
        "median_steps_success": _median([s.steps for s in successes]),
        "median_steps_all": _median([s.steps for s in ended]),
        "median_duration_s": _median(durations),
        "p50_model_latency_ms": _pct(model_latencies, 0.5),
        "p95_model_latency_ms": _pct(model_latencies, 0.95),
        "p50_action_latency_ms": _pct(action_latencies, 0.5),
        "p95_action_latency_ms": _pct(action_latencies, 0.95),
        "model_calls": len(model_latencies),
        "model_retries": retries,
        "actions": sum(action_kinds.values()),
        "action_error_rate": _rate(action_errors, sum(action_kinds.values())),
        "action_mix": dict(action_kinds.most_common()),
        "failure_taxonomy": dict(sorted(failure_taxonomy.items(), key=lambda kv: -kv[1])),
        "guardrail_interventions": dict(guardrail.most_common()),
        "approvals": approvals,
        "stuck_nudges": stuck_nudges,
        "total_cost_usd": round(sum(costs), 4) if costs else 0.0,
        "mean_cost_usd": round(statistics.mean(costs), 4) if costs else None,
        "by_backend": by_backend,
        "by_model": by_model,
        "by_tag": by_tag,
    }


def timeseries(store: Store, days: int = 14, backend: str | None = None) -> list[dict[str, Any]]:
    """Daily success rate + volume for the trend chart."""
    since = time.time() - days * 86400
    sessions = store.list_sessions(limit=100000, backend=backend, since=since)
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": 0, "successes": 0, "cost_usd": 0.0})
    for s in sessions:
        if not s.status.terminal:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(s.created_at))
        b = buckets[day]
        b["sessions"] += 1
        b["successes"] += int(bool(s.success))
        b["cost_usd"] += s.cost_usd or 0.0
    out = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400))
        b = buckets.get(day, {"sessions": 0, "successes": 0, "cost_usd": 0.0})
        out.append({"day": day, **b, "success_rate": _rate(b["successes"], b["sessions"])})
    return out


def readout_markdown(summary: dict[str, Any]) -> str:
    """Human-readable usage readout (for the CLI and eval reports)."""
    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines = [
        "# Computer-use usage readout",
        "",
        f"- Sessions: {summary['sessions_total']} total, {summary['sessions_ended']} ended, {summary['sessions_active']} active",
        f"- **Task success rate: {pct(summary['success_rate'])}** ({summary['evaluated_sessions']} checker-evaluated)",
        f"- False completions (model said done, checker disagreed): {summary['false_completions']}",
        f"- Median steps (successful): {summary['median_steps_success']}  ·  median duration: {summary['median_duration_s']}s",
        f"- Model latency p50/p95: {summary['p50_model_latency_ms']} / {summary['p95_model_latency_ms']} ms  ·  retries: {summary['model_retries']}",
        f"- Action latency p50/p95: {summary['p50_action_latency_ms']} / {summary['p95_action_latency_ms']} ms  ·  action error rate: {pct(summary['action_error_rate'])}",
        f"- Guardrail interventions: {sum(summary['guardrail_interventions'].values())}  ·  approvals: {summary['approvals']}  ·  stuck nudges: {summary['stuck_nudges']}",
        f"- Cost: ${summary['total_cost_usd']:.2f} total, mean ${summary['mean_cost_usd'] or 0:.3f}/session",
        "",
        "## Failure taxonomy",
    ]
    if summary["failure_taxonomy"]:
        lines += [f"- {k}: {v}" for k, v in summary["failure_taxonomy"].items()]
    else:
        lines.append("- none")
    lines += ["", "## By backend"]
    for k, v in summary["by_backend"].items():
        lines.append(f"- {k}: {v['sessions']} sessions, success {pct(v['success_rate'])}, median steps {v['median_steps']}")
    if summary["by_tag"]:
        lines += ["", "## By task tag"]
        for k, v in sorted(summary["by_tag"].items(), key=lambda kv: (kv[1]["success_rate"] or 0)):
            lines.append(f"- {k}: {v['sessions']} sessions, success {pct(v['success_rate'])}")
    lines += ["", "## Action mix"]
    lines += [f"- {k}: {v}" for k, v in summary["action_mix"].items()] or ["- none"]
    return "\n".join(lines)
