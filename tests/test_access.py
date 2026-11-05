"""Access gate for a server that other machines can reach (computeruse/server/access.py)."""

from __future__ import annotations

import stat

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from computeruse.config import Settings
from computeruse.server.access import COOKIE, access_url, is_loopback, load_or_create_token
from computeruse.server.app import create_app

TOKEN = "s3cret-token"


@pytest.fixture
def gated(tmp_path) -> Settings:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>console</title>")
    return Settings(data_dir=tmp_path / "data", web_dist=dist, anthropic_api_key=None, gemini_api_key=None,  # type: ignore[call-arg]
                    model_provider="none", host="::", access_token=TOKEN, _env_file=None)


def test_loopback_addresses():
    assert all(is_loopback(h) for h in ("127.0.0.1", "127.8.9.1", "::1", "[::1]", "::ffff:127.0.0.1", "localhost"))
    assert not any(is_loopback(h) for h in ("0.0.0.0", "::", "", None, "10.1.2.3", "::ffff:10.1.2.3",
                                            "2607:f8b0::1", "testclient", "host.example.com"))


def test_the_token_is_generated_once_and_kept_owner_only(tmp_path):
    token = load_or_create_token(tmp_path / "data")
    path = tmp_path / "data" / "access_token"
    assert len(token) >= 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(tmp_path / "data") == token  # the browser's cookie survives restarts
    assert access_url("::", 8787, token, "/agents").endswith(f":8787/agents?token={token}")
    assert access_url("10.0.0.5", 8787, "a b") == "http://10.0.0.5:8787/?token=a+b"


def test_other_machines_need_the_token(gated):
    with TestClient(create_app(gated)) as c:  # the test client's peer is "testclient": not loopback
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert c.get(f"/api/health?token={TOKEN}").status_code == 200  # API calls are answered, not redirected
        page = c.get("/agents")
        assert page.status_code == 401 and "access link" in page.text

        # The link sets the cookie and takes the token out of the address bar.
        r = c.get(f"/agents?token={TOKEN}&x=1", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/agents?x=1"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE}={TOKEN};") and "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        c.cookies.set(COOKIE, TOKEN)
        assert c.get("/agents").status_code == 200 and c.get("/api/flows").status_code == 200


def test_websocket_handshakes_need_the_token(gated):
    with TestClient(create_app(gated)) as c:
        with pytest.raises(WebSocketDisconnect) as refused, c.websocket_connect("/api/events/ws"):
            pass
        assert refused.value.code == 1008
        with c.websocket_connect("/api/events/ws", headers={"Authorization": f"Bearer {TOKEN}"}):
            pass


def test_loopback_clients_and_loopback_servers_need_nothing(gated, settings):
    with TestClient(create_app(gated), client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/health").status_code == 200 and c.get("/agents").status_code == 200
    with TestClient(create_app(settings)) as c:  # no token configured (a loopback-only server): no gate
        assert c.get("/api/health").status_code == 200
