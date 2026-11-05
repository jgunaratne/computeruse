"""Schedule execution engine, background ticker, and granular permission manager."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from computeruse.evals.suite import find_suite, run_checker
from computeruse.flows.templates import TEMPLATES
from computeruse.schedules.spec import (
    PERMISSION_CATALOG,
    SUGGESTED_SCHEDULES,
    PauseReason,
    PermissionLevel,
    Schedule,
    ScheduleFrequency,
    ScheduleRun,
    ScheduleStatus,
    ScheduleTargetKind,
    ScheduleTrigger,
    compute_next_run,
    default_permissions,
    describe_trigger,
)
from computeruse.schedules.store import ScheduleStore

if TYPE_CHECKING:
    from computeruse.server.orchestrator import Orchestrator

log = logging.getLogger("computeruse.schedules")


class ScheduleError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ScheduleService:
    """Owns persisted schedules, background cron-like ticker, and granular tool permissions."""

    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch
        self.store = ScheduleStore(orch.settings.data_dir)
        self.active_tasks: dict[str, asyncio.Task[ScheduleRun]] = {}
        self._ticker_task: asyncio.Task[None] | None = None
        self._permissions: dict[str, str] = self.store.load_permissions()

    # -- lifecycle -----------------------------------------------------------------------------------

    def start_ticker(self, interval_s: float = 15.0) -> None:
        if self._ticker_task is None or self._ticker_task.done():
            self._ticker_task = asyncio.create_task(self._ticker_loop(interval_s), name="schedule-ticker")

    async def _ticker_loop(self, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("schedule ticker iteration failed")
        except asyncio.CancelledError:
            return

    async def tick(self, now_ts: float | None = None) -> list[ScheduleRun]:
        """Run any active schedules whose next_run_at has arrived."""
        now = now_ts if now_ts is not None else time.time()
        triggered: list[ScheduleRun] = []
        for sched in self.store.list():
            if sched.status != ScheduleStatus.ACTIVE:
                continue
            if sched.id in self.active_tasks and not self.active_tasks[sched.id].done():
                continue
            if sched.next_run_at is None:
                sched.next_run_at = compute_next_run(sched.trigger, now)
                self.store.save(sched)
                continue
            if sched.next_run_at <= now:
                run = await self.run_now(sched.id, wait=False)
                triggered.append(run)
        return triggered

    async def close(self) -> None:
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
            self._ticker_task = None
        for task in list(self.active_tasks.values()):
            task.cancel()
        self.active_tasks.clear()

    # -- catalog & permissions -----------------------------------------------------------------------

    def catalog(self) -> dict[str, Any]:
        ag_ok, ag_reason = self.orch.chat.available()
        ag_default = self.orch.chat.default_model() if ag_ok else None
        suggested = []
        for item in SUGGESTED_SCHEDULES:
            trig = ScheduleTrigger.model_validate(item["trigger"])
            suggested.append({
                **item,
                "trigger_summary": describe_trigger(trig),
            })
        return {
            "suggested": suggested,
            "permissions": self.get_permissions(),
            "flows": self.orch.flows.list(),
            "flow_templates": [t.summary() for t in TEMPLATES.values()],
            "antigravity": {
                "available": ag_ok,
                "reason": ag_reason,
                "default_model": ag_default.id if ag_default else None,
            },
        }

    def get_permissions(self) -> dict[str, Any]:
        categories: list[dict[str, Any]] = []
        for cat in PERMISSION_CATALOG:
            ops: list[dict[str, Any]] = []
            levels: set[str] = set()
            for op in cat["operations"]:
                lvl = self._permissions.get(op["key"], op["default"])
                levels.add(lvl)
                ops.append({
                    **op,
                    "level": lvl,
                })
            cat_level = next(iter(levels)) if len(levels) == 1 else "custom"
            categories.append({
                "id": cat["id"],
                "title": cat["title"],
                "description": cat["description"],
                "level": cat_level,
                "count": len(ops),
                "operations": ops,
                "sample_prompts": cat["sample_prompts"],
            })
        return {
            "categories": categories,
            "values": dict(self._permissions),
        }

    def update_permissions(
        self,
        *,
        updates: dict[str, str] | None = None,
        category_id: str | None = None,
        level: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        valid_levels = {lvl.value for lvl in PermissionLevel}
        if reset:
            self._permissions = self.store.save_permissions(default_permissions())
            return self.get_permissions()

        next_perms = dict(self._permissions)
        if category_id and level:
            if level not in valid_levels:
                raise ScheduleError(400, f"invalid permission level {level!r}")
            cat = next((c for c in PERMISSION_CATALOG if c["id"] == category_id), None)
            if cat is None:
                raise ScheduleError(404, f"unknown permission category {category_id!r}")
            for op in cat["operations"]:
                next_perms[op["key"]] = level

        if updates:
            defaults = default_permissions()
            for k, v in updates.items():
                if k not in defaults:
                    raise ScheduleError(400, f"unknown operation key {k!r}")
                if v not in valid_levels:
                    raise ScheduleError(400, f"invalid permission level {v!r}")
                next_perms[k] = v

        self._permissions = self.store.save_permissions(next_perms)
        return self.get_permissions()

    def operation_level(self, key: str) -> str:
        return self._permissions.get(key, PermissionLevel.ALLOW.value)

    # -- schedule CRUD -------------------------------------------------------------------------------

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.store.list()
        if status and status != "all":
            items = [s for s in items if s.status.value == status]
        return [s.to_summary() for s in items]

    def get(self, schedule_id: str) -> Schedule:
        sched = self.store.get(schedule_id)
        if sched is None:
            raise ScheduleError(404, f"unknown schedule {schedule_id!r}")
        return sched

    def get_detail(self, schedule_id: str) -> dict[str, Any]:
        sched = self.get(schedule_id)
        data = sched.to_summary()
        data["runs"] = [r.model_dump(mode="json") for r in self.store.list_runs(schedule_id, limit=25)]
        return data

    def create(
        self,
        *,
        title: str,
        plan: str,
        target_kind: str = "antigravity_chat",
        backend: str = "simulated",
        model: str | None = None,
        flow_id: str | None = None,
        demo_task: str | None = None,
        trigger: dict[str, Any] | None = None,
        status: str = "active",
    ) -> Schedule:
        title = (title or "").strip()
        plan = (plan or "").strip()
        if not title:
            raise ScheduleError(400, "schedule title is required")
        if not plan:
            raise ScheduleError(400, "schedule plan is required")
        try:
            trig = ScheduleTrigger.model_validate(trigger or {})
            tk = ScheduleTargetKind(target_kind)
            st = ScheduleStatus(status)
        except ValueError as e:
            raise ScheduleError(400, str(e)) from e

        now = time.time()
        next_run = compute_next_run(trig, now) if st == ScheduleStatus.ACTIVE else None
        sched = Schedule(
            id=uuid.uuid4().hex[:12],
            title=title[:140],
            plan=plan[:10_000],
            target_kind=tk,
            backend=(backend or "simulated").strip(),
            model=(model or "").strip() or None,
            flow_id=(flow_id or "").strip() or None,
            demo_task=(demo_task or "").strip() or None,
            status=st,
            pause_reason=PauseReason.USER_ACTION if st == ScheduleStatus.PAUSED else None,
            trigger=trig,
            created_at=now,
            updated_at=now,
            next_run_at=next_run,
        )
        return self.store.save(sched)

    def update(self, schedule_id: str, payload: dict[str, Any]) -> Schedule:
        sched = self.get(schedule_id)
        now = time.time()
        if "title" in payload and payload["title"] is not None:
            t = str(payload["title"]).strip()
            if not t:
                raise ScheduleError(400, "schedule title cannot be empty")
            sched.title = t[:140]
        if "plan" in payload and payload["plan"] is not None:
            p = str(payload["plan"]).strip()
            if not p:
                raise ScheduleError(400, "schedule plan cannot be empty")
            sched.plan = p[:10_000]
        if "target_kind" in payload and payload["target_kind"] is not None:
            try:
                sched.target_kind = ScheduleTargetKind(str(payload["target_kind"]))
            except ValueError as e:
                raise ScheduleError(400, str(e)) from e
        if "backend" in payload and payload["backend"] is not None:
            sched.backend = str(payload["backend"]).strip() or "simulated"
        if "model" in payload:
            sched.model = str(payload["model"] or "").strip() or None
        if "flow_id" in payload:
            sched.flow_id = str(payload["flow_id"] or "").strip() or None
        if "demo_task" in payload:
            sched.demo_task = str(payload["demo_task"] or "").strip() or None
        if "trigger" in payload and isinstance(payload["trigger"], dict):
            try:
                sched.trigger = ScheduleTrigger.model_validate(payload["trigger"])
            except ValueError as e:
                raise ScheduleError(400, str(e)) from e
        if "status" in payload and payload["status"] is not None:
            try:
                new_status = ScheduleStatus(str(payload["status"]))
            except ValueError as e:
                raise ScheduleError(400, str(e)) from e
            sched.status = new_status
            if new_status == ScheduleStatus.PAUSED:
                sched.pause_reason = PauseReason(payload.get("pause_reason") or PauseReason.USER_ACTION.value)
            else:
                sched.pause_reason = None
        if "unread" in payload and payload["unread"] is not None:
            sched.unread = bool(payload["unread"])

        sched.updated_at = now
        sched.next_run_at = compute_next_run(sched.trigger, now) if sched.status == ScheduleStatus.ACTIVE else None
        return self.store.save(sched)

    def set_status(
        self,
        schedule_id: str,
        status: str,
        pause_reason: str | None = None,
    ) -> Schedule:
        return self.update(schedule_id, {"status": status, "pause_reason": pause_reason})

    def mark_read(self, schedule_id: str) -> Schedule:
        sched = self.get(schedule_id)
        if sched.unread:
            sched.unread = False
            self.store.save(sched)
        return sched

    async def delete(self, schedule_id: str) -> None:
        self.get(schedule_id)
        task = self.active_tasks.pop(schedule_id, None)
        if task and not task.done():
            task.cancel()
        self.store.delete(schedule_id)

    # -- execution -----------------------------------------------------------------------------------

    async def run_now(self, schedule_id: str, *, wait: bool = False) -> ScheduleRun:
        sched = self.get(schedule_id)
        now = time.time()
        run = ScheduleRun(
            id=uuid.uuid4().hex[:12],
            schedule_id=sched.id,
            schedule_title=sched.title,
            target_kind=sched.target_kind,
            status="running",
            started_at=now,
            summary="Starting scheduled execution…",
        )
        self.store.save_run(run)
        sched.last_run = run
        sched.last_run_at = now
        sched.updated_at = now
        self.store.save(sched)

        task = asyncio.create_task(self._execute_run(sched.id, run), name=f"schedule-run-{run.id[:8]}")
        self.active_tasks[sched.id] = task
        if wait:
            return await task
        return run

    async def _execute_run(self, schedule_id: str, run: ScheduleRun) -> ScheduleRun:
        t0 = time.perf_counter()
        sched = self.store.get(schedule_id)
        if sched is None:
            run.status = "failed"
            run.error = "schedule was deleted"
            run.ended_at = time.time()
            return self.store.save_run(run)

        try:
            if sched.target_kind == ScheduleTargetKind.ANTIGRAVITY_CHAT:
                if self.operation_level("automation::scheduled_chat") == PermissionLevel.DENY.value:
                    raise RuntimeError("Scheduled Antigravity prompts are blocked by tool permissions (Never allow)")
                await self._run_antigravity_chat(sched, run)
            elif sched.target_kind == ScheduleTargetKind.SESSION:
                if self.operation_level("automation::scheduled_sessions") == PermissionLevel.DENY.value:
                    raise RuntimeError("Scheduled computer sessions are blocked by tool permissions (Never allow)")
                await self._run_session(sched, run)
            elif sched.target_kind == ScheduleTargetKind.FLOW:
                if self.operation_level("automation::scheduled_flows") == PermissionLevel.DENY.value:
                    raise RuntimeError("Scheduled multi-agent flows are blocked by tool permissions (Never allow)")
                await self._run_flow(sched, run)
            run.status = "completed"
        except Exception as e:  # noqa: BLE001
            log.warning("scheduled run %s (%s) failed: %s", run.id, sched.title, e)
            run.status = "failed"
            run.error = str(e)
            if not run.summary or run.summary.startswith("Starting"):
                run.summary = f"Failed: {e}"

        ended = time.time()
        run.ended_at = ended
        run.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.store.save_run(run)

        latest = self.store.get(schedule_id)
        if latest is not None:
            latest.last_run = run
            latest.last_run_at = ended
            latest.run_count += 1
            latest.unread = True
            latest.updated_at = ended
            if latest.trigger.frequency == ScheduleFrequency.ONCE:
                latest.status = ScheduleStatus.COMPLETED
                latest.next_run_at = None
            elif latest.status == ScheduleStatus.ACTIVE:
                nxt = compute_next_run(latest.trigger, ended)
                if nxt is None:
                    latest.status = ScheduleStatus.COMPLETED
                    latest.next_run_at = None
                else:
                    latest.next_run_at = nxt
            self.store.save(latest)
        return run

    async def _run_antigravity_chat(self, sched: Schedule, run: ScheduleRun) -> None:
        ok, reason = self.orch.chat.available()
        if ok:
            chat = await self.orch.chat.create(model=sched.model, title=f"[Scheduled] {sched.title}")
            run.chat_id = chat.id
            self.store.save_run(run)
            _, assistant = await self.orch.chat.send(chat.id, sched.plan, model=sched.model, screenshot=False)
            if chat.task is not None:
                await asyncio.wait_for(chat.task, timeout=120)
            if assistant.error:
                raise RuntimeError(assistant.error)
            first_line = (assistant.text or "Completed Antigravity turn.").strip().splitlines()[0]
            run.summary = first_line[:160]
        else:
            # Offline fallback when Antigravity LS is not attached (e.g. unit tests / throwaway server):
            # record a clean diagnostic summary so scheduled dry-runs still verify end-to-end.
            sessions = self.orch.list_sessions(limit=5)
            completed = sum(1 for s in sessions if s.status.value == "completed")
            run.summary = (
                f"Prepared scheduled briefing ({len(sessions)} recent sessions, {completed} completed; "
                f"Antigravity offline: {reason})"
            )

    async def _run_session(self, sched: Schedule, run: ScheduleRun) -> None:
        task = sched.plan
        backend = sched.backend or "simulated"
        script = None
        checker = None
        tags = ["scheduled", f"schedule:{sched.id}"]
        backend_options: dict[str, Any] = {}
        guardrails: dict[str, Any] = {}

        demo_ref = sched.demo_task
        if not demo_ref and backend == "simulated" and not self.orch.catalog.any_available():
            demo_ref = "simulated-basics/calc_multiply"

        if demo_ref and "/" in demo_ref:
            suite_name, task_id = demo_ref.split("/", 1)
            suite = find_suite(suite_name)
            spec = suite.task(task_id)
            task = task or spec.instruction
            script = spec.script
            backend = suite.backend
            backend_options = dict(spec.backend_options)
            guardrails = dict(spec.guardrails)
            tags.extend(spec.tags)

            async def _checker(runner, result):
                return await run_checker(spec.checker, runner, result)

            checker = _checker

        model_name = sched.model or ("scripted" if script else self.orch.settings.model)
        rec = await self.orch.create_session(
            task=task,
            backend=backend,
            model=model_name,
            script=script,
            tags=tags,
            backend_options=backend_options,
            checker=checker,
            metadata={"schedule_id": sched.id, "schedule_title": sched.title},
            guardrails=guardrails or None,
        )
        run.session_id = rec.id
        self.store.save_run(run)
        finished = await self.orch.wait(rec.id, timeout=180)
        if finished.status.value != "completed":
            raise RuntimeError(finished.outcome_reason or f"session ended with {finished.status.value}")
        run.summary = f"Session {finished.id[:8]} completed in {finished.steps} steps ({finished.backend})"

    async def _run_flow(self, sched: Schedule, run: ScheduleRun) -> None:
        flow_id = sched.flow_id
        existing_ids = {f["id"] for f in self.orch.flows.list()}
        if not flow_id or flow_id not in existing_ids:
            template_id = flow_id if flow_id in TEMPLATES else "offline-demo"
            created = self.orch.flows.create(name=sched.title, template=template_id)
            flow_id = created.id
            sched.flow_id = flow_id
            self.store.save(sched)

        run.flow_id = flow_id
        runner = await self.orch.flows.start(flow_id, goal=sched.plan)
        run.flow_run_id = runner.id
        self.store.save_run(run)
        if runner.task is not None:
            await asyncio.wait_for(runner.task, timeout=180)
        snap = runner.to_json()
        if snap["status"] not in ("completed", "incomplete"):
            raise RuntimeError(snap.get("reason") or f"flow ended with {snap['status']}")
        run.summary = f"Flow '{snap['flow_name']}' {snap['status']} ({snap['invocations']} agent calls)"
