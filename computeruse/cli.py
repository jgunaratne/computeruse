"""Command-line entry point: `computeruse serve|run|eval|readout|models|doctor|daemon`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from computeruse import __version__
from computeruse.config import Settings, get_settings


def _settings(args: argparse.Namespace) -> Settings:
    s = get_settings()
    if getattr(args, "data_dir", None):
        s.data_dir = Path(args.data_dir)
    return s


# -- serve --------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from computeruse.server.app import create_app

    s = _settings(args)
    host, port = args.host or s.host, args.port or s.port
    s.host, s.port = host, port
    uvicorn.run(create_app(s), host=host, port=port, log_level="info" if not args.quiet else "warning",
                ws_ping_interval=20, ws_ping_timeout=20)
    return 0


# -- run ----------------------------------------------------------------------


def _fmt_event(e) -> str | None:
    d, t = e.data, e.type.value
    if t == "assistant.text":
        return f"  💬 {d.get('text', '').strip()}"
    if t == "action.proposed":
        return f"  → {d.get('description')}"
    if t == "action.executed":
        err = f"  ✗ {d.get('error')}" if not d.get("ok", True) else ""
        return f"    done in {d.get('duration_ms', 0):.0f} ms{err}"
    if t == "guardrail.decision" and d.get("decision") != "allow":
        return f"  🛡 {d.get('decision')}: {d.get('reason')}"
    if t == "approval.requested":
        return f"  ⏸ approval needed: {d.get('reason')} ({d.get('description')})"
    if t == "stuck.nudged":
        return "  ⚠ no screen change; nudging the model"
    if t == "model.retry":
        return f"  ↻ model retry: {d.get('error')}"
    if t == "error":
        return f"  ✗ {d.get('message') or d.get('detail')}"
    if t == "session.ended":
        return f"■ ended: {d.get('outcome')} — {d.get('reason') or ''} ({d.get('steps')} steps, {d.get('turns')} turns, " \
               f"{(d.get('duration_ms') or 0) / 1000:.1f}s, ${d.get('cost_usd') or 0:.3f})"
    if t == "turn.started" and d.get("phase") == "eval_check":
        return f"  ✓ checker passed: {d.get('detail')}" if d.get("passed") else f"  checker: {d.get('detail')}"
    return None


async def _run_async(args: argparse.Namespace) -> int:
    from computeruse.evals.suite import find_suite, run_checker
    from computeruse.server.orchestrator import Orchestrator

    s = _settings(args)
    orch = Orchestrator(s)
    try:
        task, script, backend, checker, tags = args.task or "", None, args.backend, None, ["cli"]
        if args.demo_task:
            suite_name, task_id = args.demo_task.split("/", 1)
            suite = find_suite(suite_name)
            spec = suite.task(task_id)
            task, script, backend = task or spec.instruction, spec.script, backend or suite.backend
            tags += ["demo", *spec.tags]

            async def checker(runner, result):  # type: ignore[no-redef]
                return await run_checker(spec.checker, runner, result)
        if not task:
            print("error: --task or --demo-task is required", file=sys.stderr)
            return 2
        overrides = {k: getattr(args, k) for k in ("max_steps", "max_duration_s", "max_cost_usd") if getattr(args, k) is not None}
        overrides["auto_approve"] = args.auto_approve
        q = orch.bus.subscribe(None)
        rec = await orch.create_session(task=task, backend=backend or "browser", model=args.model or s.model,
                                        overrides=overrides, script=script, tags=tags, checker=checker)
        print(f"▶ session {rec.id} on {rec.backend} with {rec.model}\n  task: {task}")

        async def printer() -> None:
            while True:
                e = await q.get()
                if e is None or e.session_id != rec.id:
                    continue
                line = _fmt_event(e)
                if line:
                    print(line, flush=True)
                if args.json:
                    print(json.dumps(e.to_wire()), flush=True)

        p = asyncio.create_task(printer())
        try:
            rec = await orch.wait(rec.id)
        finally:
            p.cancel()
        if rec.eval_passed is not None:
            print(f"  verifier: {'PASS' if rec.eval_passed else 'FAIL'} — {rec.eval_detail}")
        print(f"  frames + events: {s.data_dir / 'sessions' / rec.id}")
        return 0 if rec.success else 1
    finally:
        await orch.shutdown()


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_async(args))


# -- eval ---------------------------------------------------------------------


async def _eval_async(args: argparse.Namespace) -> int:
    from computeruse.evals.runner import report_markdown, run_suite
    from computeruse.evals.suite import find_suite, list_suites
    from computeruse.server.orchestrator import Orchestrator

    if args.list:
        for suite in list_suites():
            print(f"{suite.name:24s} backend={suite.backend:10s} tasks={len(suite.tasks)}  {suite.description.strip()[:70]}")
        return 0
    if not args.suite:
        print("error: --suite is required (or --list)", file=sys.stderr)
        return 2
    s = _settings(args)
    orch = Orchestrator(s)
    try:
        suite = find_suite(args.suite)

        def progress(rec):
            mark = "PASS" if rec.eval_passed else "FAIL" if rec.eval_passed is False else "----"
            print(f"  [{mark}] {rec.eval_task_id:36s} {rec.outcome.value if rec.outcome else '?':16s} "
                  f"steps={rec.steps:<3d} {str(rec.eval_detail or '')[:60]}", flush=True)

        print(f"▶ eval {suite.name} ({len(suite.tasks)} tasks × {args.repeats}) model={args.model} "
              f"backend={args.backend or suite.backend}")
        summary = await run_suite(orch, suite, model=args.model, repeats=args.repeats,
                                  task_ids=args.task, concurrency=args.concurrency, backend=args.backend,
                                  progress=progress)
        print()
        print(report_markdown(summary))
        if args.out:
            Path(args.out).write_text(json.dumps(summary, indent=2, default=str))
            print(f"\nwrote {args.out}")
        return 0 if (summary["pass_rate"] or 0) >= args.min_pass_rate else 1
    finally:
        await orch.shutdown()


def cmd_eval(args: argparse.Namespace) -> int:
    return asyncio.run(_eval_async(args))


# -- readout ------------------------------------------------------------------


def cmd_readout(args: argparse.Namespace) -> int:
    from computeruse.telemetry import metrics
    from computeruse.telemetry.store import Store

    s = _settings(args)
    store = Store(s.data_dir)
    try:
        summary = metrics.summarize(store, since_s=args.since_days * 86400 if args.since_days else None,
                                    backend=args.backend, include_evals=not args.no_evals)
        print(json.dumps(summary, indent=2, default=str) if args.json else metrics.readout_markdown(summary))
    finally:
        store.close()
    return 0


# -- doctor -------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    from computeruse.computer.profile import profile_age_label
    from computeruse.computer.registry import BackendRegistry
    from computeruse.computer.remote import find_chrome
    from computeruse.computer.sso import security_key_forwarding

    s = _settings(args)
    rows: list[tuple[str, bool | None, str]] = []
    rows.append(("python", True, sys.version.split()[0]))
    from computeruse.agent.providers import ModelCatalog

    catalog = ModelCatalog(s)
    for provider, st in catalog.refresh().items():
        rows.append((f"model:{provider}", st.available, st.reason))
    available = [f"{m.id} ({m.tool})" for m in catalog.models() if m.available]
    rows.append(("models", bool(available),
                 ", ".join(available) if available else "none — only model=scripted / replay will work"))
    rows.append(("default model", catalog.default_model() is not None,
                 catalog.default_model() or f"{s.model} unavailable"))
    rows.append(("model discovery", None if s.model_discovery == "off" else True,
                 f"{s.model_discovery} — run `computeruse models` to list and verify what the credentials can call"
                 if s.model_discovery != "off" else "off (COMPUTERUSE_MODEL_DISCOVERY)"))
    chrome = find_chrome(s.chrome_binary)
    rows.append(("chrome", bool(chrome), chrome or "not found (set COMPUTERUSE_CHROME_BINARY)"))
    rows.append(("Xvfb", bool(shutil.which("Xvfb")), shutil.which("Xvfb") or "missing — browser/x11 backends need it"))
    rows.append(("docker", bool(shutil.which("docker")), shutil.which("docker") or "missing — docker backend unavailable"))
    registry = BackendRegistry(s)
    for b in registry.list():
        rows.append((f"backend:{b.id}", b.available, b.reason or ("ok" + ("" if b.isolated else " (NOT isolated: drives your real desktop)"))))
    prof = registry.browser_profile.status()
    if prof["mode"] == "ephemeral":
        rows.append(("browser profile", None, "ephemeral — cookies/logins are discarded after every session "
                                              "(COMPUTERUSE_CHROME_PROFILE_MODE)"))
    else:
        state = ("in use by a running session" if prof["in_use"]
                 else f"{prof['size_mb']} MB, {profile_age_label(prof['last_used'])}" if prof["exists"]
                 else "not created yet — sign in once with Take control and it persists")
        rows.append(("browser profile", True, f"persistent at {prof['path']} ({state})"))
    sk = security_key_forwarding()
    rows.append(("security keys", True if sk["available"] else None,
                 f"forwarded via {sk['transport']} — {sk['detail']}" if sk["available"] else sk["detail"]))
    web = Path(s.web_dist) / "index.html"
    rows.append(("web ui build", web.exists(), str(web) if web.exists() else "not built: cd web && npm install && npm run build"))
    rows.append(("data dir", True, str(Path(s.data_dir).resolve())))
    for name, ok, detail in rows:
        mark = "✓" if ok else "✗" if ok is False else "·"
        print(f" {mark} {name:22s} {detail}")
    return 0


# -- models -------------------------------------------------------------------


async def _models_async(args: argparse.Namespace) -> int:
    from computeruse.agent.providers import ModelCatalog

    s = _settings(args)
    if args.offline:
        s.model_discovery = "off"
    elif args.no_probe:
        s.model_discovery = "list"
    catalog = ModelCatalog(s)
    catalog.refresh()
    if s.model_discovery != "off" and catalog.any_available():
        print(f"discovering models ({s.model_discovery}) for projects {catalog.projects or '-'} …", file=sys.stderr)
        await catalog.discover(force=True)
    models = catalog.models()
    state = catalog.discovery_state()
    if args.json:
        print(json.dumps({"models": [m.__dict__ for m in models], "default": catalog.default_model(),
                          "discovery": state, "providers": {p: st.__dict__ for p, st in catalog.status.items()}},
                         indent=2, default=str))
        return 0
    for provider, st in catalog.status.items():
        print(f" {'✓' if st.available else '✗'} {provider:10s} {st.reason}")
    if state["status"] == "done":
        print(f"   discovery: {state['count']} candidates, {state['verified']} verified usable, "
              f"{state['duration_ms']} ms" + (f", errors: {state['errors']}" if state["errors"] else ""))
    elif state["status"] == "error":
        print(f"   discovery failed: {state['errors']}")
    print()
    width = max([len(m.id) for m in models] + [5])
    print(f"   {'MODEL':{width}s}  {'PROVIDER':9s} {'TOOL':8s} {'PROJECT':16s} STATUS")
    for m in models:
        mark = "✓" if m.available and m.verified else "?" if m.available else "✗"
        print(f" {mark} {m.id:{width}s}  {m.provider:9s} {m.tool:8s} {(m.project or '-'):16s} {m.reason or 'available'}")
    default = catalog.default_model()
    print(f"\n default: {default or 'none available'}  (✓ verified by a live request · ? offered but unverified · ✗ unavailable)")
    return 0 if any(m.available for m in models) else 1


def cmd_models(args: argparse.Namespace) -> int:
    return asyncio.run(_models_async(args))


# -- daemon -------------------------------------------------------------------


def cmd_daemon(args: argparse.Namespace) -> int:
    from computeruse.computer.daemon import main as daemon_main

    return daemon_main(args.rest) or 0


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="computeruse", description="Computer-use agent harness")
    p.add_argument("--version", action="version", version=f"computeruse {__version__}")
    p.add_argument("--data-dir", help="override COMPUTERUSE_DATA_DIR")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the API + web UI server")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_serve)

    rp = sub.add_parser("run", help="run a single task headlessly and stream the trace")
    rp.add_argument("--task", "-t")
    rp.add_argument("--backend", "-b", help="browser|desktop|simulated|remote|docker|x11")
    rp.add_argument("--model", "-m", help="claude model id, 'scripted', or 'replay:<session_id>'")
    rp.add_argument("--demo-task", help="<suite>/<task_id>: use a reference script (model=scripted)")
    rp.add_argument("--max-steps", type=int)
    rp.add_argument("--max-duration-s", type=float)
    rp.add_argument("--max-cost-usd", type=float)
    rp.add_argument("--auto-approve", action="store_true", help="never block on approvals (headless)")
    rp.add_argument("--json", action="store_true", help="also print raw events as JSON lines")
    rp.set_defaults(fn=cmd_run)

    ep = sub.add_parser("eval", help="run an eval suite")
    ep.add_argument("--suite", "-s")
    ep.add_argument("--list", action="store_true")
    ep.add_argument("--model", "-m", default="scripted")
    ep.add_argument("--backend", "-b")
    ep.add_argument("--repeats", type=int, default=1)
    ep.add_argument("--task", action="append", help="only these task ids (repeatable)")
    ep.add_argument("--concurrency", type=int, default=1)
    ep.add_argument("--min-pass-rate", type=float, default=0.0, help="exit 1 below this rate (CI gate)")
    ep.add_argument("--out", help="write the JSON summary here")
    ep.set_defaults(fn=cmd_eval)

    mp = sub.add_parser("readout", help="print the product metrics readout")
    mp.add_argument("--since-days", type=float)
    mp.add_argument("--backend")
    mp.add_argument("--no-evals", action="store_true")
    mp.add_argument("--json", action="store_true")
    mp.set_defaults(fn=cmd_readout)

    dp = sub.add_parser("doctor", help="check the environment")
    dp.set_defaults(fn=cmd_doctor)

    lp = sub.add_parser("models", help="list the models the configured credentials can call (live-verified)")
    lp.add_argument("--json", action="store_true")
    lp.add_argument("--no-probe", action="store_true", help="trust the provider catalogues; skip the 1-token probes")
    lp.add_argument("--offline", action="store_true", help="no network: show the suggested ids only")
    lp.set_defaults(fn=cmd_models)

    dm = sub.add_parser("daemon", help="run the in-VM control daemon (args passed through)")
    dm.add_argument("rest", nargs=argparse.REMAINDER)
    dm.set_defaults(fn=cmd_daemon)
    return p


def _daemon_passthrough(argv: list[str]) -> list[str] | None:
    """`computeruse [global opts] daemon <daemon args>` → the daemon args, else None.

    argparse would otherwise reject the daemon's own flags (e.g. `--driver`) as
    unknown options of the top-level parser before the subparser sees them.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-v", "--verbose") or tok.startswith("--data-dir="):
            i += 1
        elif tok == "--data-dir":
            i += 2
        else:
            return argv[i + 1:] if tok == "daemon" else None
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    daemon_args = _daemon_passthrough(argv)
    if daemon_args is not None:
        from computeruse.computer.daemon import main as daemon_main

        return daemon_main(daemon_args) or 0
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
