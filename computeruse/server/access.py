"""Access control for a server that other machines can reach.

`computeruse serve` binds to 127.0.0.1 by default, and then needs no login: only this machine can
connect. Bound to any other address (``--host 0.0.0.0``; asyncio makes a ``::`` listener IPv6-only) it
can be reached over the network, and the API drives real computers — a browser profile that may be
signed in, the live desktop — so a request from another machine must carry the server's **access
token**, as one of:

* the ``computeruse_token`` cookie, which the console gets from a one-time ``?token=…`` link (the
  server answers that link with the cookie and a redirect to the same page without the token);
* an ``Authorization: Bearer <token>`` header (scripts);
* a ``token`` query parameter (scripts, WebSocket clients that cannot set headers).

Requests from this machine itself (loopback) are always let through, so local tools and a browser on
the same machine keep working unchanged. The token is ``COMPUTERUSE_ACCESS_TOKEN`` if set; otherwise
`computeruse serve` generates one the first time it binds beyond loopback and keeps it in
``<data_dir>/access_token`` (mode 0600), so the cookie in your browser survives restarts.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
from pathlib import Path
from urllib.parse import urlencode

from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "computeruse_token"
COOKIE_MAX_AGE_S = 90 * 24 * 3600
TOKEN_FILE = "access_token"

_DENIED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>computeruse</title>
<style>body{font:15px/1.5 system-ui,sans-serif;background:#111;color:#ddd;display:grid;place-items:center;
height:100vh;margin:0}main{max-width:34rem;padding:2rem}code{background:#222;padding:.1em .35em;border-radius:4px}
</style></head><body><main><h1>This console needs its access link</h1>
<p>The server accepts other machines only with its access token. Open the link it printed when it started
(it ends in <code>?token=…</code>), or read the token from <code>&lt;data dir&gt;/access_token</code> on the
machine that runs it. Opening the link once is enough: your browser keeps a cookie.</p></main></body></html>"""


def is_loopback(host: str | None) -> bool:
    """Whether `host` (an address or "localhost") names this machine's loopback interface."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def load_or_create_token(data_dir: Path) -> str:
    """The token kept in `<data_dir>/access_token`, created (owner-only) on first use."""
    path = Path(data_dir) / TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if token:
        return token
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def access_url(host: str, port: int, token: str, path: str = "/") -> str:
    """The link to hand out: wildcard binds are named by this machine's fully-qualified host name."""
    name = socket.getfqdn() if host in ("", "0.0.0.0", "::") else host
    if ":" in name:
        name = f"[{name}]"
    return f"http://{name}:{port}{path}?{urlencode({'token': token})}"


class AccessGate:
    """ASGI middleware that applies the rules above to HTTP requests and WebSocket handshakes."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("AccessGate needs a non-empty token")
        self.app = app
        self._token = token.encode()

    def _matches(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if conn.client is not None and is_loopback(conn.client.host):
            await self.app(scope, receive, send)
            return
        auth = conn.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        query_token = conn.query_params.get("token")
        if not any(self._matches(t) for t in (conn.cookies.get(COOKIE), bearer, query_token)):
            await self._deny(scope, receive, send, conn)
            return
        if (scope["type"] == "http" and query_token and scope["method"] in ("GET", "HEAD")
                and not conn.url.path.startswith("/api/")):
            # A shared link: remember the token in a cookie and take it out of the address bar.
            rest = [(k, v) for k, v in conn.query_params.multi_items() if k != "token"]
            response = RedirectResponse(conn.url.path + (f"?{urlencode(rest)}" if rest else ""), status_code=303)
            response.set_cookie(COOKIE, query_token, max_age=COOKIE_MAX_AGE_S, path="/", httponly=True,
                                samesite="lax")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _deny(self, scope: Scope, receive: Receive, send: Send, conn: HTTPConnection) -> None:
        if scope["type"] == "websocket":
            await receive()  # the websocket.connect event; closing before accept refuses the handshake
            await send({"type": "websocket.close", "code": 1008})
            return
        if conn.url.path.startswith("/api/"):
            response = JSONResponse({"detail": "this server needs its access token: open the console with the "
                                     "link it printed at startup, or send 'Authorization: Bearer <token>'"},
                                    status_code=401)
        else:
            response = HTMLResponse(_DENIED_PAGE, status_code=401)
        await response(scope, receive, send)
