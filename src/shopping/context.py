"""Process-wide singletons shared by the web UI and MCP server."""

from __future__ import annotations

import os

from shopping.auth.session import AuthSession
from shopping.config import settings
from shopping.retailer.client import PCExpressClient

auth = AuthSession(settings.session_dir)
_client: PCExpressClient | None = None

PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{settings.port}").rstrip("/")


def client() -> PCExpressClient:
    global _client
    if _client is None:
        _client = PCExpressClient(
            api_base=settings.pcx_api_base,
            api_key=settings.pcx_api_key,
            banner=settings.banner,
            store_id=settings.store_id,
            auth=auth,
            raw_dir=settings.raw_dir,
        )
    return _client


async def shutdown() -> None:
    if _client is not None:
        await _client.aclose()
