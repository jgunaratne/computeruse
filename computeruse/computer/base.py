"""Computer backend interface.

A `Computer` is anything the agent can observe (screenshot) and act on
(execute an Action). Implementations:

* `SimulatedComputer`  – in-process fake desktop, deterministic; used for evals/tests.
* `X11Computer`        – drives a real X display (local Xvfb or inside the sandbox VM).
* `RemoteComputer`     – HTTP client for the daemon running inside a VM/container.
* `DockerComputer`     – provisions a sandbox container then delegates to RemoteComputer.

The agent loop only depends on this interface, so swapping the sandbox
technology (Docker → Firecracker → cloud VM) never touches the harness.
"""

from __future__ import annotations

import abc
import hashlib
import io
from dataclasses import dataclass, field

from PIL import Image
from pydantic import BaseModel

from computeruse.computer.actions import Action, ActionResult, action_coordinates


class DisplayInfo(BaseModel):
    """Physical size of the VM display (pixels)."""

    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


class ComputerHealth(BaseModel):
    ok: bool
    backend: str
    detail: str | None = None
    live_view_url: str | None = None


@dataclass
class Frame:
    """A captured screenshot, always stored as PNG bytes."""

    png: bytes
    width: int
    height: int
    sha1: str = field(default="")

    def __post_init__(self) -> None:
        if not self.sha1:
            self.sha1 = hashlib.sha1(self.png).hexdigest()

    @classmethod
    def from_image(cls, img: Image.Image) -> Frame:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False, compress_level=3)
        return cls(png=buf.getvalue(), width=img.width, height=img.height)

    @classmethod
    def from_png(cls, png: bytes) -> Frame:
        with Image.open(io.BytesIO(png)) as img:
            return cls(png=png, width=img.width, height=img.height)

    def image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.png)).convert("RGB")


class ComputerError(RuntimeError):
    """Backend is unavailable or failed to perform an action."""


class Computer(abc.ABC):
    """Abstract VM/desktop the agent operates."""

    name: str = "abstract"

    @property
    @abc.abstractmethod
    def display(self) -> DisplayInfo: ...

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Boot/connect the backend. Idempotent."""

    async def stop(self) -> None:  # noqa: B027 - optional hook
        """Tear down the backend. Idempotent."""

    @abc.abstractmethod
    async def screenshot(self) -> Frame: ...

    @abc.abstractmethod
    async def execute(self, action: Action) -> ActionResult: ...

    async def health(self) -> ComputerHealth:
        return ComputerHealth(ok=True, backend=self.name)

    async def run_shell(self, command: str, timeout: float = 30.0) -> tuple[int, str]:
        """Run a shell command inside the computer (used by eval checkers).

        Not every backend supports this; the default raises.
        """
        raise ComputerError(f"{self.name} backend does not support shell execution")

    def live_view_url(self) -> str | None:
        """URL of a human-viewable live stream (e.g. noVNC), if any."""
        return None

    # -- helpers -----------------------------------------------------------

    def validate_coordinates(self, action: Action) -> str | None:
        """Return an error string if the action references out-of-bounds pixels."""
        w, h = self.display.size
        for x, y in action_coordinates(action):
            if not (0 <= x < w and 0 <= y < h):
                return (
                    f"coordinate ({x}, {y}) is outside the {w}x{h} display; "
                    f"valid x in [0, {w - 1}], y in [0, {h - 1}]"
                )
        return None
