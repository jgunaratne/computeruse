"""HTTP + WebSocket API.

Everything the web UI (and the CLI) needs: create/inspect/control sessions,
stream events and live preview frames, serve persisted screenshots, product
metrics, and eval runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from computeruse import __version__
from computeruse.evals import runner as eval_runner
from computeruse.evals.suite import Suite, find_suite, list_suites, run_checker, suite_to_json
from computeruse.server.orchestrator import Orchestrator
from computeruse.server.schemas import (
    ControlRequest,
    CreateSessionRequest,
    EvalRunRequest,
    session_to_json,
)
from computeruse.telemetry import metrics
from computeruse.telemetry.events import EventType

log = logging.getLogger("computeruse.api")
router = APIRouter(prefix="/api")


def _orch(request: Request | WebSocket) -> Orchestrator:
    return request.app.state.orch


def _demo_task(name: str) -> tuple[Suite, Any]:
    if "/" not in name:
        raise HTTPException(400, "demo_task must look like '<suite>/<task_id>'")
    suite_name, task_id = name.split("/", 1)
    try:
        suite = find_suite(suite_name)
        return suite, suite.task(task_id)
    except (FileNotFoundError, KeyError) as e:
        raise HTTPException(404, f"unknown demo task {name!r}: {e}") from e


# -- meta ---------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__, "time": time.time()}


@router.get("/backends")
def backends(request: Request) -> list[dict[str, Any]]:
    return [b.__dict__ for b in _orch(request).registry.list()]


def _models_payload(catalog) -> dict[str, Any]:
    return {"models": [asdict(m) for m in catalog.models()], "default": catalog.default_model(),
            "discovery": catalog.discovery_state()}


@router.get("/config")
def config(request: Request) -> dict[str, Any]:
    orch = _orch(request)
    s = orch.settings
    catalog = orch.catalog
    models = [asdict(m) for m in catalog.models()]
    return {
        "model": catalog.default_model() or s.model, "models": models,
        "models_available": catalog.any_available(), "model_access": catalog.summary(),
        "models_discovery": catalog.discovery_state(),
        "tool_version": s.tool_version, "preview_fps": s.preview_fps, "control_preview_fps": s.control_preview_fps,
        "browser_size": s.browser_size, "browser_profile": orch.registry.browser_profile.status(),
        "defaults": {
            "max_steps": s.default_max_steps, "max_duration_s": s.default_max_duration_s,
            "max_cost_usd": s.default_max_cost_usd, "stuck_threshold": s.stuck_threshold,
            "screenshot_history": s.screenshot_history, "settle_ms": s.settle_ms,
            "approval_timeout_s": s.approval_timeout_s,
        },
        "guardrails": {
            "allowed_domains": s.allowed_domains, "blocked_domains": s.blocked_domains,
            "blocked_keys": s.blocked_keys, "desktop_blocked_keys": s.desktop_blocked_keys,
            "max_type_length": s.max_type_length,
        },
    }


@router.get("/models")
def models(request: Request) -> dict[str, Any]:
    """Models the picker offers, with discovery state. Cheap: never triggers network calls."""
    return _models_payload(_orch(request).catalog)


@router.post("/models/refresh")
async def refresh_models(request: Request) -> dict[str, Any]:
    """Re-run model discovery now (lists the provider catalogues and re-verifies access), then return the result."""
    catalog = _orch(request).catalog
    await catalog.discover(force=True)
    return _models_payload(catalog)


@router.get("/demo-tasks")
def demo_tasks() -> list[dict[str, Any]]:
    out = []
    for suite in list_suites():
        for t in suite.tasks:
            if t.script:
                out.append({"id": f"{suite.name}/{t.id}", "suite": suite.name, "task_id": t.id,
                            "backend": suite.backend, "instruction": t.instruction, "tags": t.tags,
                            "checker": t.checker})
    return out


# -- sessions -----------------------------------------------------------------


@router.post("/sessions", status_code=201)
async def create_session(request: Request, req: CreateSessionRequest) -> dict[str, Any]:
    orch = _orch(request)
    task, script, backend, checker, tags = req.task.strip(), req.script, req.backend, None, list(req.tags)
    backend_options = dict(req.backend_options)
    guardrails = dict(req.guardrails or {})
    if req.demo_task:
        suite, spec = _demo_task(req.demo_task)
        task = task or spec.instruction
        script = script or spec.script
        backend = backend or suite.backend
        backend_options = {**spec.backend_options, **backend_options}
        guardrails = {**spec.guardrails, **guardrails}
        tags = ["demo", *spec.tags, *tags]

        async def checker(runner, result):  # type: ignore[no-redef]
            return await run_checker(spec.checker, runner, result)

    if not task:
        raise HTTPException(400, "task is required")
    overrides = req.overrides.model_dump(exclude_none=True) if req.overrides else {}
    if req.start_paused:
        overrides["start_paused"] = True
    try:
        rec = await orch.create_session(
            task=task, backend=backend or "browser", model=req.model or orch.settings.model, overrides=overrides,
            script=script, tags=tags, backend_options=backend_options, checker=checker,
            metadata={"demo_task": req.demo_task} if req.demo_task else {}, guardrails=guardrails or None,
        )
    except KeyError as e:
        raise HTTPException(400, str(e.args[0]) if e.args else "unknown backend") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return session_to_json(rec)


@router.get("/sessions")
def list_sessions(request: Request, limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0),
                  backend: str | None = None, eval_run_id: str | None = None, status: str | None = None,
                  since_s: float | None = None) -> list[dict[str, Any]]:
    since = time.time() - since_s if since_s else None
    recs = _orch(request).list_sessions(limit=limit, offset=offset, backend=backend, eval_run_id=eval_run_id,
                                        status=status, since=since)
    return [session_to_json(r) for r in recs]


@router.get("/sessions/{session_id}")
def get_session(request: Request, session_id: str, include_events: bool = False) -> dict[str, Any]:
    orch = _orch(request)
    rec = orch.get_session(session_id)
    if not rec:
        raise HTTPException(404, "unknown session")
    out = session_to_json(rec)
    if include_events:
        out["events"] = [e.to_wire() for e in orch.events(session_id)]
    return out


@router.get("/sessions/{session_id}/events")
def get_events(request: Request, session_id: str, after: int = Query(-1),
               types: str | None = None) -> list[dict[str, Any]]:
    orch = _orch(request)
    if not orch.get_session(session_id):
        raise HTTPException(404, "unknown session")
    wanted = set(types.split(",")) if types else None
    return [e.to_wire() for e in orch.events(session_id, after_seq=after) if not wanted or e.type.value in wanted]


@router.get("/sessions/{session_id}/frames/{seq}.png")
def get_frame(request: Request, session_id: str, seq: int) -> FileResponse:
    path = _orch(request).store.frame_path(session_id, seq)
    if not path:
        raise HTTPException(404, "no such frame")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.post("/sessions/{session_id}/control")
async def control(request: Request, session_id: str, req: ControlRequest) -> dict[str, Any]:
    try:
        return await _orch(request).control(session_id, req.command, req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/sessions/{session_id}/manual")
async def manual_action(request: Request, session_id: str, action: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _orch(request).manual_action(session_id, action)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


# -- websockets ---------------------------------------------------------------


Sender = Callable[[dict[str, Any]], Awaitable[None]]


class InputPump:
    """Executes operator input arriving on a session websocket (`{"type": "input", "action": {...}, "id"?: n}`).

    Discrete actions (clicks, keys, text) run strictly in order. Pointer moves are collapsed to the
    latest position and consecutive typed characters are merged, so a computer that is slower than
    the operator's hand never builds up a backlog of stale hovers. Results are reported for inputs
    that carry an `id`, and for every failure.
    """

    MAX_QUEUE = 512

    def __init__(self, orch: Orchestrator, session_id: str, send: Sender) -> None:
        self.orch, self.session_id, self.send = orch, session_id, send
        self.queue: deque[dict[str, Any]] = deque()
        self.move: dict[str, Any] | None = None
        self.wake = asyncio.Event()
        self.dropped = 0

    def push(self, msg: dict[str, Any]) -> bool:
        action = msg.get("action")
        if not isinstance(action, dict):
            raise ValueError("input message needs an 'action' object")
        if action.get("action") == "mouse_move":
            self.move = msg
        elif len(self.queue) >= self.MAX_QUEUE:
            self.dropped += 1
            return False
        else:
            self.queue.append(msg)
        self.wake.set()
        return True

    async def run(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.queue:
                await self._exec(self._merge_typing(self.queue.popleft()))
            if self.move is not None:
                msg, self.move = self.move, None
                await self._exec(msg)

    def _merge_typing(self, msg: dict[str, Any]) -> dict[str, Any]:
        action = msg["action"]
        if action.get("action") != "type":
            return msg
        text, last_id = str(action.get("text", "")), msg.get("id")
        while self.queue and self.queue[0]["action"].get("action") == "type":
            nxt = self.queue.popleft()
            text += str(nxt["action"].get("text", ""))
            last_id = nxt.get("id", last_id)
        return {**msg, "id": last_id, "action": {**action, "text": text}}

    async def _exec(self, msg: dict[str, Any]) -> None:
        action = msg["action"]
        try:
            result = await self.orch.operator_input(self.session_id, action)
        except ValueError as e:
            result = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - computer transport errors
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if msg.get("id") is not None or not result.get("ok"):
            await self.send({"type": "input_result", "id": msg.get("id"), "action": action.get("action"), **result})


async def _ws_receiver(ws: WebSocket, send: Sender | None = None, pump: InputPump | None = None) -> None:
    """Drain client messages so we notice disconnects; answers pings and feeds operator input to the pump."""
    while True:
        msg = await ws.receive_json()
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "ping" and send:
            await send({"type": "pong", "time": time.time()})
        elif kind == "input" and pump is not None and send is not None:
            try:
                ok = pump.push(msg)
            except ValueError as e:
                await send({"type": "input_result", "id": msg.get("id"), "ok": False, "error": str(e)})
                continue
            if not ok:
                await send({"type": "input_result", "id": msg.get("id"), "ok": False,
                            "error": "input queue full; slow down"})


def _locked_sender(ws: WebSocket) -> Sender:
    """Serialises sends from the event loop and the input pump onto one socket."""
    lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with lock:
            await ws.send_json(payload)

    return send


@router.websocket("/sessions/{session_id}/ws")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    orch = _orch(ws)
    rec = orch.get_session(session_id)
    if not rec:
        await ws.close(code=4404, reason="unknown session")
        return
    await ws.accept()
    send = _locked_sender(ws)
    # Subscribe before loading history so nothing slips between the two.
    q = orch.bus.subscribe(session_id, maxsize=2000)
    pump = InputPump(orch, session_id, send)
    pump_task = asyncio.create_task(pump.run())
    receiver = asyncio.create_task(_ws_receiver(ws, send, pump))
    try:
        history = orch.events(session_id)
        last_seq = history[-1].seq if history else -1
        await send({"type": "history", "session": session_to_json(rec), "events": [e.to_wire() for e in history]})
        if rec.status.terminal:
            await send({"type": "end", "session": session_to_json(rec)})
            return
        while True:
            getter = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({getter, receiver}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                receiver.result()  # raises WebSocketDisconnect
                return
            if getter not in done:
                getter.cancel()
                current = orch.get_session(session_id)
                if current and current.status.terminal:  # missed the close marker (slow consumer)
                    await send({"type": "end", "session": session_to_json(current)})
                    return
                await send({"type": "heartbeat", "session": session_to_json(current) if current else None})
                continue
            event = getter.result()
            if event is None:
                current = orch.get_session(session_id) or rec
                await send({"type": "end", "session": session_to_json(current)})
                return
            if event.seq <= last_seq and event.type != EventType.PREVIEW:
                continue
            payload: dict[str, Any] = {"type": "event", "event": event.to_wire()}
            if event.type in (EventType.SESSION_STARTED, EventType.SESSION_PAUSED, EventType.SESSION_RESUMED,
                              EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED, EventType.SESSION_ENDED,
                              EventType.MODEL_CALLED, EventType.ACTION_EXECUTED, EventType.OPERATOR_CONTROL):
                current = orch.get_session(session_id)
                if current:
                    payload["session"] = session_to_json(current)
            await send(payload)
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        receiver.cancel()
        pump_task.cancel()
        orch.bus.unsubscribe(q, session_id)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/events/ws")
async def global_ws(ws: WebSocket) -> None:
    """Session-list feed: every non-preview event across sessions, with the session snapshot."""
    orch = _orch(ws)
    await ws.accept()
    q = orch.bus.subscribe(None, maxsize=2000)
    receiver = asyncio.create_task(_ws_receiver(ws, _locked_sender(ws)))
    try:
        while True:
            getter = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({getter, receiver}, timeout=25, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                receiver.result()
                return
            if getter not in done:
                getter.cancel()
                await ws.send_json({"type": "heartbeat"})
                continue
            event = getter.result()
            if event is None or event.type in (EventType.PREVIEW, EventType.FRAME):
                continue
            rec = orch.get_session(event.session_id)
            await ws.send_json({"type": "event", "event": {**event.to_wire(), "data": {}},
                                "session": session_to_json(rec) if rec else None})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiver.cancel()
        orch.bus.unsubscribe(q, None)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# -- metrics ------------------------------------------------------------------


@router.get("/metrics/summary")
def metrics_summary(request: Request, since_s: float | None = None, backend: str | None = None,
                    include_evals: bool = True) -> dict[str, Any]:
    return metrics.summarize(_orch(request).store, since_s=since_s, backend=backend, include_evals=include_evals)


@router.get("/metrics/timeseries")
def metrics_timeseries(request: Request, days: int = Query(14, ge=1, le=365),
                       backend: str | None = None) -> list[dict[str, Any]]:
    return metrics.timeseries(_orch(request).store, days=days, backend=backend)


@router.get("/metrics/readout", response_class=PlainTextResponse)
def metrics_readout(request: Request, since_s: float | None = None, backend: str | None = None) -> str:
    return metrics.readout_markdown(metrics.summarize(_orch(request).store, since_s=since_s, backend=backend))


# -- evals --------------------------------------------------------------------


@router.get("/evals/suites")
def eval_suites() -> list[dict[str, Any]]:
    return [suite_to_json(s) for s in list_suites()]


@router.post("/evals/run", status_code=202)
async def eval_run(request: Request, req: EvalRunRequest) -> dict[str, Any]:
    orch = _orch(request)
    try:
        suite = find_suite(req.suite)
    except (FileNotFoundError, KeyError) as e:
        raise HTTPException(404, f"unknown suite {req.suite!r}") from e
    backend = req.backend or suite.backend
    try:
        info = orch.registry.info(backend)
    except KeyError as e:
        raise HTTPException(404, f"unknown backend {backend!r}") from e
    if not info.available:
        raise HTTPException(400, f"backend {backend!r} unavailable: {info.reason}")
    if req.model == "scripted":
        missing = [t.id for t in suite.tasks if (not req.task_ids or t.id in req.task_ids) and not t.script]
        if missing:
            raise HTTPException(400, f"no reference script for tasks: {missing}")
    elif not req.model.startswith("replay:"):
        ok, reason = orch.catalog.check(req.model)
        if not ok:
            raise HTTPException(400, f"{reason}; run with model=scripted for an offline eval")
    run_id = eval_runner.new_run_id()

    async def runner() -> None:
        try:
            await eval_runner.run_suite(orch, suite, model=req.model, repeats=req.repeats, task_ids=req.task_ids,
                                        concurrency=req.concurrency, backend=backend, run_id=run_id)
        except Exception:  # noqa: BLE001
            log.exception("eval run %s failed", run_id)

    task = asyncio.create_task(runner(), name=f"eval-{run_id}")
    request.app.state.background.add(task)
    task.add_done_callback(request.app.state.background.discard)
    return {"run_id": run_id, "suite": suite.name, "backend": backend, "model": req.model,
            "tasks": len([t for t in suite.tasks if not req.task_ids or t.id in req.task_ids]) * req.repeats}


@router.get("/evals/runs")
def eval_runs(request: Request, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return _orch(request).store.list_eval_runs(limit=limit)


@router.get("/evals/runs/{run_id}")
def eval_run_detail(request: Request, run_id: str) -> dict[str, Any]:
    orch = _orch(request)
    run = orch.store.get_eval_run(run_id)
    if not run:
        raise HTTPException(404, "unknown eval run")
    run["sessions"] = [session_to_json(r) for r in orch.list_sessions(limit=10000, eval_run_id=run_id)]
    return run


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)
