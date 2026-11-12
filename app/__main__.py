"""Container entrypoint: ``python -m app``.

Reads ``PORT`` (Cloud Run sets it; default 8000) and the graceful-shutdown limit from settings,
then runs uvicorn with our JSON access log instead of uvicorn's (docs/DECISIONS.md, D22).
"""

import os
from collections.abc import Mapping
from typing import Any

import uvicorn

from app.config import Settings


def uvicorn_options(environ: Mapping[str, str], settings: Settings) -> dict[str, Any]:
    raw_port = environ.get("PORT", "8000")
    if not raw_port.isascii() or not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise ValueError(f"PORT must be a number from 1 to 65535, got {raw_port!r}")
    return {
        # Inside a container the app must listen on all interfaces to be reachable at all;
        # Cloud Run's front end is the only thing that can connect to it.
        "host": environ.get("HOST", "0.0.0.0"),  # noqa: S104
        "port": int(raw_port),
        "factory": True,
        "access_log": False,  # RequestContextMiddleware writes a richer JSON access log
        "timeout_graceful_shutdown": settings.graceful_shutdown_timeout_s,
        # Don't trust X-Forwarded-* by default; nothing uses the client IP yet.
        "proxy_headers": False,
        "server_header": False,  # don't advertise the server software
    }


def main() -> None:
    uvicorn.run("app.main:create_app", **uvicorn_options(os.environ, Settings()))


if __name__ == "__main__":
    main()
