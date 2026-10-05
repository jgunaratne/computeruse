"""`DockerComputer` against a fake `docker` CLI backed by the real control daemon.

Docker is not available on every dev machine, but the container client's whole
control flow (argument composition, port discovery, bearer token, health wait,
teardown) can be verified against the genuine daemon protocol by substituting the
`docker` binary with a small script that starts `computeruse.computer.daemon` on a
private Xvfb instead of a container.
"""

import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from computeruse.computer.actions import parse_action
from computeruse.computer.docker_vm import DockerComputer

pytestmark = pytest.mark.skipif(not shutil.which("Xvfb"), reason="Xvfb not installed")

FAKE_DOCKER = textwrap.dedent(
    r'''
    #!{python}
    """Fake `docker` CLI: `run` starts the real daemon, `port` reports it, `rm` kills it."""
    import json, os, signal, subprocess, sys, time, uuid

    STATE = {state!r}
    args = sys.argv[1:]
    cmd = args[0] if args else ""

    def load():
        return json.load(open(STATE)) if os.path.exists(STATE) else {{}}

    def save(d):
        json.dump(d, open(STATE, "w"))

    if cmd == "run":
        env = dict(os.environ)
        env_args = {{}}
        for i, a in enumerate(args):
            if a == "-e":
                k, _, v = args[i + 1].partition("=")
                env_args[k] = v
        env["COMPUTERUSE_DAEMON_TOKEN"] = env_args["COMPUTERUSE_DAEMON_TOKEN"]
        size = env_args.get("SCREEN_SIZE", "1280x800")
        cid = uuid.uuid4().hex
        log = open(STATE + f".{{cid}}.log", "w+b")
        proc = subprocess.Popen(
            [sys.executable, "-m", "computeruse.computer.daemon", "--driver", "x11",
             "--host", "127.0.0.1", "--port", "0", "--size", size],
            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        port = None
        for _ in range(300):
            log.seek(0)
            for line in log.read().decode(errors="replace").splitlines():
                if line.startswith("LISTENING"):
                    port = int(line.split()[2])
            if port or proc.poll() is not None:
                break
            time.sleep(0.1)
        if not port:
            sys.stderr.write("fake daemon failed to start\n")
            sys.exit(1)
        d = load(); d[cid] = {{"pid": proc.pid, "port": port, "args": args, "image": args[-1]}}; save(d)
        print(cid)
    elif cmd == "port":
        cid, cport = args[1], args[2]
        info = load().get(cid)
        if not info:
            sys.stderr.write("no such container\n"); sys.exit(1)
        if cport in ("8800", "8800/tcp"):
            print(f"127.0.0.1:{{info['port']}}")
        else:
            sys.stderr.write(f"port {{cport}} not published\n"); sys.exit(1)
    elif cmd == "rm":
        cid = args[-1]
        d = load(); info = d.pop(cid, None); save(d)
        if info:
            try:
                os.killpg(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        print(cid)
    else:
        sys.stderr.write(f"fake docker: unsupported {{cmd}}\n"); sys.exit(2)
    '''
).lstrip()


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(FAKE_DOCKER.format(python=sys.executable, state=str(tmp_path / "containers.json")))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # The daemon subprocess must import the package from the repo checkout.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    return tmp_path / "containers.json"


async def test_docker_computer_lifecycle_against_real_daemon(fake_docker):
    comp = DockerComputer("computeruse-sandbox:test", size=(640, 480), start_url="about:blank")
    await comp.start()
    try:
        assert comp.container_id
        state = json.loads(fake_docker.read_text())
        run_args = state[comp.container_id]["args"]
        # Argument composition: detached, auto-removed, both ports published on loopback,
        # token + screen size + start url passed through, image last.
        assert run_args[:3] == ["run", "-d", "--rm"]
        assert "-p" in run_args and "127.0.0.1::8800" in run_args and "127.0.0.1::6080" in run_args
        assert any(a.startswith("COMPUTERUSE_DAEMON_TOKEN=") for a in run_args)
        assert "SCREEN_SIZE=640x480" in run_args and "START_URL=about:blank" in run_args
        assert run_args[-1] == "computeruse-sandbox:test"
        # Port discovery + token auth + health against the genuine daemon.
        assert comp.base_url.startswith("http://127.0.0.1:")
        assert comp.live_view_url() is None  # noVNC port deliberately "not published" by the fake
        health = await comp.health()
        assert health.ok and comp.display.size == (640, 480)
        frame = await comp.screenshot()
        assert (frame.width, frame.height) == (640, 480)
        res = await comp.execute(parse_action({"action": "mouse_move", "coordinate": [50, 60]}))
        assert res.ok
        pos = await comp.execute(parse_action({"action": "cursor_position"}))
        assert pos.output == "(50, 60)"
        assert await comp.devtools_tabs() is None  # no Chrome in the fake sandbox
    finally:
        await comp.stop()
    # Teardown removed the "container" and the daemon is gone.
    assert comp.container_id is None
    assert json.loads(fake_docker.read_text()) == {}
    health = await comp.health()
    assert not health.ok and "not started" in health.detail


async def test_docker_computer_rejects_wrong_token(fake_docker):
    comp = DockerComputer("computeruse-sandbox:test", size=(320, 240))
    await comp.start()
    try:
        import httpx

        bad = httpx.AsyncClient(base_url=comp.base_url, headers={"Authorization": "Bearer nope"})
        try:
            assert (await bad.get("/health")).status_code == 401
            assert (await bad.post("/action", json={"action": "screenshot"})).status_code == 401
        finally:
            await bad.aclose()
    finally:
        await comp.stop()
