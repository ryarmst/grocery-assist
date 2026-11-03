"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    store_id: str
    banner: str
    pcx_api_key: str
    pcx_api_base: str
    host: str
    port: int
    log_level: str
    mcp_token: str
    allowed_hosts: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "session"

    @property
    def raw_dir(self) -> Path:
        """Recent raw retailer responses, kept for debugging (secrets are never in these)."""
        return self.data_dir / "raw"


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("SHOPPING_ENV_FILE", ".env")))
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    hosts = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1")
    return Settings(
        data_dir=data_dir,
        store_id=os.environ.get("STORE_ID", "3728"),
        banner=os.environ.get("BANNER", "nofrills"),
        pcx_api_key=os.environ.get("PCX_API_KEY", ""),
        pcx_api_base=os.environ.get("PCX_API_BASE", "https://api.pcexpress.ca/pcx-bff/api"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        mcp_token=os.environ.get("MCP_TOKEN", ""),
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
    )


settings = load_settings()
