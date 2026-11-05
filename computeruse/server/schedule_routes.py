"""HTTP API for scheduled automations and granular tool permissions (the console's Schedules tab).

    GET    /api/schedules/catalog           suggested starter templates, permissions, flows, Antigravity status
    GET    /api/schedules                   schedules, newest first (optional ?status=active|paused|completed)
    POST   /api/schedules                   create a schedule
    GET    /api/schedules/{id}              one schedule with recent run history
    PUT    /api/schedules/{id}              update schedule fields or trigger
    POST   /api/schedules/{id}/status       pause, resume, or complete a schedule
    POST   /api/schedules/{id}/read         clear unread indicator
    POST   /api/schedules/{id}/run          trigger an immediate execution
    DELETE /api/schedules/{id}              delete schedule
    GET    /api/permissions                 granular per-operation tool permissions & sample prompts
    PUT    /api/permissions                 update per-operation or category-level permissions
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from computeruse.schedules.engine import ScheduleError, ScheduleService

router = APIRouter(prefix="/api")


class ScheduleCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    plan: str = Field(min_length=1, max_length=10_000)
    target_kind: str = "antigravity_chat"
    backend: str = "simulated"
    model: str | None = None
    flow_id: str | None = None
    demo_task: str | None = None
    trigger: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"


class ScheduleStatusRequest(BaseModel):
    status: str
    pause_reason: str | None = None


class PermissionUpdateRequest(BaseModel):
    updates: dict[str, str] | None = None
    category_id: str | None = None
    level: str | None = None
    reset: bool = False


def _schedules(request: Request) -> ScheduleService:
    return request.app.state.orch.schedules


def _http(e: ScheduleError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("/schedules/catalog")
def schedule_catalog(request: Request) -> dict[str, Any]:
    return _schedules(request).catalog()


@router.get("/schedules")
def schedule_list(request: Request, status: str | None = Query(None)) -> list[dict[str, Any]]:
    return _schedules(request).list(status=status)


@router.post("/schedules", status_code=201)
def schedule_create(request: Request, req: ScheduleCreateRequest) -> dict[str, Any]:
    try:
        sched = _schedules(request).create(
            title=req.title,
            plan=req.plan,
            target_kind=req.target_kind,
            backend=req.backend,
            model=req.model,
            flow_id=req.flow_id,
            demo_task=req.demo_task,
            trigger=req.trigger,
            status=req.status,
        )
    except ScheduleError as e:
        raise _http(e) from e
    return sched.to_summary()


@router.get("/schedules/{schedule_id}")
def schedule_get(request: Request, schedule_id: str) -> dict[str, Any]:
    try:
        return _schedules(request).get_detail(schedule_id)
    except ScheduleError as e:
        raise _http(e) from e


@router.put("/schedules/{schedule_id}")
def schedule_update(request: Request, schedule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _schedules(request).update(schedule_id, payload).to_summary()
    except ScheduleError as e:
        raise _http(e) from e


@router.post("/schedules/{schedule_id}/status")
def schedule_status(request: Request, schedule_id: str, req: ScheduleStatusRequest) -> dict[str, Any]:
    try:
        return _schedules(request).set_status(schedule_id, req.status, req.pause_reason).to_summary()
    except ScheduleError as e:
        raise _http(e) from e


@router.post("/schedules/{schedule_id}/read")
def schedule_mark_read(request: Request, schedule_id: str) -> dict[str, Any]:
    try:
        return _schedules(request).mark_read(schedule_id).to_summary()
    except ScheduleError as e:
        raise _http(e) from e


@router.post("/schedules/{schedule_id}/run", status_code=202)
async def schedule_run_now(
    request: Request,
    schedule_id: str,
    wait: bool = Query(False),
) -> dict[str, Any]:
    try:
        run = await _schedules(request).run_now(schedule_id, wait=wait)
        sched = _schedules(request).get(schedule_id)
    except ScheduleError as e:
        raise _http(e) from e
    return {
        "run": run.model_dump(mode="json"),
        "schedule": sched.to_summary(),
    }


@router.delete("/schedules/{schedule_id}", status_code=204)
async def schedule_delete(request: Request, schedule_id: str) -> Response:
    try:
        await _schedules(request).delete(schedule_id)
    except ScheduleError as e:
        raise _http(e) from e
    return Response(status_code=204)


@router.get("/permissions")
def permissions_get(request: Request) -> dict[str, Any]:
    return _schedules(request).get_permissions()


@router.put("/permissions")
def permissions_update(request: Request, req: PermissionUpdateRequest) -> dict[str, Any]:
    try:
        return _schedules(request).update_permissions(
            updates=req.updates,
            category_id=req.category_id,
            level=req.level,
            reset=req.reset,
        )
    except ScheduleError as e:
        raise _http(e) from e
