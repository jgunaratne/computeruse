"""File persistence for schedules, schedule runs, and granular operation permissions.

Schedules live under ``<data_dir>/schedules/<id>.json``, recent runs under
``<data_dir>/schedule_runs/<run_id>.json``, and per-operation permissions under
``<data_dir>/permissions.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from computeruse.schedules.spec import (
    ID_RE,
    PermissionLevel,
    Schedule,
    ScheduleRun,
    default_permissions,
)


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


class ScheduleStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.schedules_dir = self.data_dir / "schedules"
        self.runs_dir = self.data_dir / "schedule_runs"
        self.permissions_path = self.data_dir / "permissions.json"

    # -- schedules -----------------------------------------------------------------------------------

    def _schedule_path(self, schedule_id: str) -> Path:
        return self.schedules_dir / f"{_check_id(schedule_id)}.json"

    def list(self) -> list[Schedule]:
        out: list[Schedule] = []
        for path in self.schedules_dir.glob("*.json") if self.schedules_dir.exists() else []:
            data = _read_json(path)
            if data is None:
                continue
            try:
                out.append(Schedule.model_validate(data))
            except ValueError:
                continue
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    def get(self, schedule_id: str) -> Schedule | None:
        try:
            data = _read_json(self._schedule_path(schedule_id))
        except KeyError:
            return None
        if data is None:
            return None
        try:
            return Schedule.model_validate(data)
        except ValueError:
            return None

    def save(self, schedule: Schedule) -> Schedule:
        _write_json(self._schedule_path(schedule.id), schedule.model_dump(mode="json"))
        return schedule

    def delete(self, schedule_id: str) -> bool:
        try:
            path = self._schedule_path(schedule_id)
        except KeyError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- runs ----------------------------------------------------------------------------------------

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{_check_id(run_id)}.json"

    def save_run(self, run: ScheduleRun) -> ScheduleRun:
        _write_json(self._run_path(run.id), run.model_dump(mode="json"))
        return run

    def get_run(self, run_id: str) -> ScheduleRun | None:
        try:
            data = _read_json(self._run_path(run_id))
        except KeyError:
            return None
        if data is None:
            return None
        try:
            return ScheduleRun.model_validate(data)
        except ValueError:
            return None

    def list_runs(self, schedule_id: str | None = None, limit: int = 30) -> list[ScheduleRun]:
        runs: list[ScheduleRun] = []
        for path in self.runs_dir.glob("*.json") if self.runs_dir.exists() else []:
            data = _read_json(path)
            if not data:
                continue
            if schedule_id is not None and data.get("schedule_id") != schedule_id:
                continue
            try:
                runs.append(ScheduleRun.model_validate(data))
            except ValueError:
                continue
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    # -- permissions ---------------------------------------------------------------------------------

    def load_permissions(self) -> dict[str, str]:
        defaults = default_permissions()
        saved = _read_json(self.permissions_path)
        if not isinstance(saved, dict):
            return defaults
        valid_levels = {lvl.value for lvl in PermissionLevel}
        for k, v in saved.items():
            if k in defaults and v in valid_levels:
                defaults[k] = v
        return defaults

    def save_permissions(self, perms: dict[str, str]) -> dict[str, str]:
        defaults = default_permissions()
        valid_levels = {lvl.value for lvl in PermissionLevel}
        merged = dict(defaults)
        for k, v in perms.items():
            if k in merged and v in valid_levels:
                merged[k] = v
        _write_json(self.permissions_path, merged)
        return merged
