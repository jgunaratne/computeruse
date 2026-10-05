"""Docker-hosted sandbox VM: runs the control daemon (x11 driver + Chromium) in a container.

This backend is the portable deployment path for machines that have Docker.
It is intentionally thin: `docker run` the image from `docker/Dockerfile`,
map the daemon port, then behave exactly like `RemoteComputer`.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil

from computeruse.computer.base import ComputerError, ComputerHealth
from computeruse.computer.remote import RemoteComputer

DAEMON_PORT = 8800
NOVNC_PORT = 6080
PROFILE_MOUNT = "/profile"


class DockerComputer(RemoteComputer):
    name = "docker"

    def __init__(self, image: str, size: tuple[int, int] = (1280, 800), start_url: str = "about:blank",
                 profile_dir: str | None = None) -> None:
        self._token = secrets.token_urlsafe(24)
        super().__init__("http://127.0.0.1:0", token=self._token, name="docker")
        self.image = image
        self.size = size
        self.start_url = start_url
        self.profile_dir = profile_dir  # host directory mounted as the Chromium profile (persistent logins)
        self.container_id: str | None = None

    async def _docker(self, *args: str, timeout: float = 120) -> str:
        docker = shutil.which("docker")
        if not docker:
            raise ComputerError("docker CLI not found on PATH")
        proc = await asyncio.create_subprocess_exec(
            docker, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise ComputerError(f"docker {' '.join(args[:2])} timed out") from None
        if proc.returncode != 0:
            raise ComputerError(f"docker {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()}")
        return out.decode().strip()

    def run_args(self) -> list[str]:
        w, h = self.size
        args = [
            "run", "-d", "--rm", "--shm-size=1g",
            "-p", f"127.0.0.1::{DAEMON_PORT}", "-p", f"127.0.0.1::{NOVNC_PORT}",
            "-e", f"COMPUTERUSE_DAEMON_TOKEN={self._token}",
            "-e", f"SCREEN_SIZE={w}x{h}", "-e", f"START_URL={self.start_url}",
        ]
        if self.profile_dir:
            args += ["-v", f"{self.profile_dir}:{PROFILE_MOUNT}", "-e", f"PROFILE_DIR={PROFILE_MOUNT}"]
        return [*args, self.image]

    async def start(self) -> None:
        if self.container_id:
            return
        self.container_id = await self._docker(*self.run_args())
        port = await self._mapped_port(DAEMON_PORT)
        novnc = await self._mapped_port(NOVNC_PORT, required=False)
        self.base_url = f"http://127.0.0.1:{port}"
        import httpx

        await self._client.aclose()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0,
                                         headers={"Authorization": f"Bearer {self._token}"})
        if novnc:
            self._live_view = f"http://127.0.0.1:{novnc}/vnc.html?autoconnect=1&resize=scale"
        deadline = asyncio.get_event_loop().time() + 90
        while True:
            health = await self.health()
            if health.ok:
                return
            if asyncio.get_event_loop().time() > deadline:
                await self.stop()
                raise ComputerError(f"sandbox container never became healthy: {health.detail}")
            await asyncio.sleep(1.0)

    async def _mapped_port(self, container_port: int, required: bool = True) -> int | None:
        assert self.container_id
        try:
            out = await self._docker("port", self.container_id, str(container_port))
        except ComputerError:
            if required:
                raise
            return None
        # "127.0.0.1:49153" (possibly multiple lines)
        for line in out.splitlines():
            if ":" in line:
                return int(line.rsplit(":", 1)[1])
        if required:
            raise ComputerError(f"container port {container_port} is not published")
        return None

    async def health(self) -> ComputerHealth:
        if not self.container_id:
            return ComputerHealth(ok=False, backend=self.name, detail="container not started")
        return await super().health()

    async def stop(self) -> None:
        cid, self.container_id = self.container_id, None
        if cid:
            try:
                await self._docker("rm", "-f", cid, timeout=60)
            except ComputerError:
                pass
        await super().stop()
