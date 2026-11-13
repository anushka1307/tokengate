"""Entry point: ``python -m app.admin <command>``. See app/admin/cli.py."""

import asyncio
import sys

from app.admin.cli import run
from app.config import Settings
from app.telemetry.logging import configure_logging


def main() -> int:
    settings = Settings()
    configure_logging(settings.log_level, stream=sys.stderr)  # keep stdout for results only
    return asyncio.run(run(sys.argv[1:], settings, sys.stdout))


if __name__ == "__main__":
    sys.exit(main())
