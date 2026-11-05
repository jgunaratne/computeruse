"""HTTP API for agent flows (the console's Agents tab).

    GET    /api/flows/catalog              node types, ports, roles, templates, Antigravity models
    GET    /api/flows                      flows, newest first, with their latest run
    POST   /api/flows                      create (blank, from a template, or from nodes/edges)
    GET    /api/flows/{id}                 one flow
    PUT    /api/flows/{id}                 save the whole graph (the editor autosaves)
    DELETE /api/flows/{id}                 delete (stops its run first)
    POST   /api/flows/{id}/runs            start a run; 202 with the run
    GET    /api/flows/{id}/runs            recent runs
    GET    /api/flow-runs/{run_id}         the run; ?after=<version>&wait_s= long-polls for a newer version
    POST   /api/flow-runs/{run_id}/cancel  stop a run
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from computeruse.flows.engine import FlowError, FlowService

router = APIRouter(prefix="/api")


class FlowCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    template: str | None = None
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] | None = None
    settings: dict[str, Any] | None = None


class FlowRunRequest(BaseModel):
    goal: str | None = Field(default=None, max_length=20_000,
                             description="Replaces the text of every goal node for this run only.")


def _flows(request: Request) -> FlowService:
    return request.app.state.orch.flows


def _http(e: FlowError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("/flows/catalog")
def flow_catalog(request: Request) -> dict[str, Any]:
    return _flows(request).catalog()


@router.get("/flows")
def flow_list(request: Request) -> list[dict[str, Any]]:
    return _flows(request).list()


@router.post("/flows", status_code=201)
def flow_create(request: Request, req: FlowCreateRequest) -> dict[str, Any]:
    try:
        flow = _flows(request).create(name=req.name, template=req.template, nodes=req.nodes, edges=req.edges,
                                      settings=req.settings)
    except FlowError as e:
        raise _http(e) from e
    return flow.model_dump(mode="json")


@router.get("/flows/{flow_id}")
def flow_get(request: Request, flow_id: str) -> dict[str, Any]:
    try:
        return _flows(request).get(flow_id).model_dump(mode="json")
    except FlowError as e:
        raise _http(e) from e


@router.put("/flows/{flow_id}")
def flow_save(request: Request, flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _flows(request).save(flow_id, payload).model_dump(mode="json")
    except FlowError as e:
        raise _http(e) from e


@router.delete("/flows/{flow_id}", status_code=204)
async def flow_delete(request: Request, flow_id: str) -> Response:
    try:
        await _flows(request).delete(flow_id)
    except FlowError as e:
        raise _http(e) from e
    return Response(status_code=204)


@router.post("/flows/{flow_id}/runs", status_code=202)
async def flow_run_start(request: Request, flow_id: str, req: FlowRunRequest | None = None) -> dict[str, Any]:
    try:
        runner = await _flows(request).start(flow_id, goal=req.goal if req else None)
    except FlowError as e:
        raise _http(e) from e
    return runner.to_json()


@router.get("/flows/{flow_id}/runs")
def flow_run_list(request: Request, flow_id: str, limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    try:
        return _flows(request).list_runs(flow_id, limit=limit)
    except FlowError as e:
        raise _http(e) from e


@router.get("/flow-runs/{run_id}")
async def flow_run_get(request: Request, run_id: str, after: int = Query(-1),
                       wait_s: float = Query(0, ge=0, le=30)) -> dict[str, Any]:
    """The run; with `wait_s`, waits until its version is newer than `after` (or the run ends)."""
    try:
        return await _flows(request).wait_run(run_id, after, wait_s)
    except FlowError as e:
        raise _http(e) from e


@router.post("/flow-runs/{run_id}/cancel")
async def flow_run_cancel(request: Request, run_id: str) -> dict[str, Any]:
    try:
        return await _flows(request).cancel_run(run_id)
    except FlowError as e:
        raise _http(e) from e
