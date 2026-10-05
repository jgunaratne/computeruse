"""Shared fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from computeruse.agent.guardrails import GuardrailPolicy
from computeruse.agent.loop import AgentLoop, RunConfig, SessionController
from computeruse.agent.model import ScriptedModelClient
from computeruse.computer.simulated import SimulatedComputer
from computeruse.config import Settings
from computeruse.telemetry.events import EventType


class MemRecorder:
    """In-memory Recorder for loop-level tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.frames = 0

    async def emit(self, type: EventType, data: dict[str, Any]) -> None:
        self.events.append((type.value, data))

    async def save_frame(self, frame, meta):
        self.frames += 1
        return {"seq": self.frames, "url": f"mem://{self.frames}", "width": frame.width, "height": frame.height,
                "sha1": frame.sha1, **meta}

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == kind]

    @property
    def types(self) -> list[str]:
        return [t for t, _ in self.events]


def default_policy(**kw) -> GuardrailPolicy:
    base = dict(blocked_keys=["ctrl+alt+Delete"], desktop_blocked_keys=["alt+F4"], allowed_domains=[],
                blocked_domains=[], max_type_length=4000)
    base.update(kw)
    return GuardrailPolicy.default(**base)


def make_loop(script, *, policy=None, controller=None, latency_ms=0, model_cls=ScriptedModelClient, **cfg):
    """Loop over SimOS with a scripted model. `model_cls(script, locator=, latency_ms=)` may wrap the client."""
    sim = SimulatedComputer()
    rec = MemRecorder()
    model = model_cls(script, locator=sim.locate, latency_ms=latency_ms)
    config = RunConfig(task="test task", settle_ms=0, settle_checks=0, **cfg)
    loop = AgentLoop(session_id="t", computer=sim, model=model, config=config, recorder=rec,
                     policy=policy or default_policy(), backend="simulated",
                     controller=controller or SessionController())
    return sim, rec, loop


class HttpRecorder:
    """`httpx.MockTransport` handler: records every request, plays scripted responses (or raises exceptions)."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {request.url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


async def fake_token() -> str:
    return "tok"


async def no_backoff(attempt: int) -> None:
    """Replaces a client's `_backoff` so retry tests do not sleep."""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # ``model_provider="none"`` keeps the suite hermetic: the catalog must not pick up
    # whatever ADC credentials / API keys happen to exist on the developer's machine.
    return Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "nodist", anthropic_api_key=None,
                    gemini_api_key=None, model_provider="none", _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
