"""Application entry point: web UI + MCP (streamable HTTP at /mcp) in one process."""

from __future__ import annotations

import contextlib
import hmac
import logging
import socket

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from shopping import context
from shopping.config import settings
from shopping.database import run_migrations
from shopping.logging_setup import setup_logging
from shopping.mcp_server.server import mcp
from shopping.web.routes import SameOriginPostMiddleware, router

setup_logging(settings.log_level)
log = logging.getLogger("shopping")


class MCPBearerAuth:
    """Optional shared-secret bearer token for the MCP endpoint (set MCP_TOKEN)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(supplied, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _transport_security() -> TransportSecuritySettings:
    hosts, origins = [], []
    for h in settings.allowed_hosts:
        hosts += [h, f"{h}:*"]
        origins += [f"http://{h}", f"http://{h}:*", f"https://{h}", f"https://{h}:*"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


mcp_app = mcp.streamable_http_app(streamable_http_path="/", transport_security=_transport_security(), host=settings.host.split(",")[0].strip())


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    run_migrations()
    if not settings.pcx_api_key:
        log.warning("PCX_API_KEY is not set; retailer calls will fail (see README).")
    async with mcp.session_manager.run():
        log.info("shopping assistant ready: store=%s data=%s mcp=/mcp auth=%s", settings.store_id, settings.data_dir, context.auth.status().state)
        yield
    await context.shutdown()


app = FastAPI(title="Shopping Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SameOriginPostMiddleware)
app.mount("/static", StaticFiles(directory=str(__import__("pathlib").Path(__file__).parent / "web" / "static")), name="static")
app.mount("/mcp", MCPBearerAuth(mcp_app, settings.mcp_token))
app.include_router(router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "auth": context.auth.status().state}


def _listen_sockets() -> list[socket.socket]:
    """HOST may list several addresses (e.g. "127.0.0.1,192.168.56.10") so the app can be reached
    on loopback plus one specific interface without binding to every interface."""
    socks = []
    for host in [h.strip() for h in settings.host.split(",") if h.strip()]:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, settings.port))
        s.set_inheritable(True)
        socks.append(s)
        log.info("listening on http://%s:%s", f"[{host}]" if family == socket.AF_INET6 else host, settings.port)
    return socks


def main() -> None:
    config = uvicorn.Config("shopping.app:app", port=settings.port, log_config=None, proxy_headers=True)
    uvicorn.Server(config).run(sockets=_listen_sockets())


if __name__ == "__main__":
    main()
