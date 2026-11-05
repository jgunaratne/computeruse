"""File persistence for flows and flow runs.

Flows are small JSON documents under ``<data_dir>/flows/<id>.json``. Each run gets a directory
``<data_dir>/flow_runs/<run_id>/`` holding ``run.json`` (written when the run starts and when it ends)
and the journals its journal nodes keep (``journal-<node>.md``) — memory that survives the run.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from computeruse.flows.spec import ID_RE, Flow


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _check_id(value: str) -> str:
    if not ID_RE.match(value or ""):
        raise KeyError(value)
    return value


class FlowStore:
    def __init__(self, data_dir: Path) -> None:
        self.flows_dir = Path(data_dir) / "flows"
        self.runs_dir = Path(data_dir) / "flow_runs"

    # -- flows ---------------------------------------------------------------------------------------

    def _flow_path(self, flow_id: str) -> Path:
        return self.flows_dir / f"{_check_id(flow_id)}.json"

    def list(self) -> list[Flow]:
        flows = []
        for path in self.flows_dir.glob("*.json") if self.flows_dir.exists() else []:
            data = _read_json(path)
            if data is None:
                continue
            try:
                flows.append(Flow.model_validate(data))
            except ValueError:
                continue  # a hand-edited file that no longer validates is skipped, not fatal
        return sorted(flows, key=lambda f: f.updated_at, reverse=True)

    def get(self, flow_id: str) -> Flow | None:
        try:
            data = _read_json(self._flow_path(flow_id))
        except KeyError:
            return None
        return Flow.model_validate(data) if data is not None else None

    def save(self, flow: Flow) -> Flow:
        _write_json(self._flow_path(flow.id), flow.model_dump(mode="json"))
        return flow

    def delete(self, flow_id: str) -> bool:
        try:
            path = self._flow_path(flow_id)
        except KeyError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- runs ----------------------------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / _check_id(run_id)

    def save_run(self, run: dict[str, Any]) -> None:
        _write_json(self.run_dir(run["id"]) / "run.json", run)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            return _read_json(self.run_dir(run_id) / "run.json")
        except KeyError:
            return None

    def list_runs(self, flow_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        runs = []
        for path in self.runs_dir.glob("*/run.json") if self.runs_dir.exists() else []:
            data = _read_json(path)
            if data and (flow_id is None or data.get("flow_id") == flow_id):
                runs.append(data)
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]
