"""Structured JSON logging: one JSON object per line on stdout.

structlog and stdlib loggers (uvicorn, httpx) share one formatter, so every line has the same shape.
Context bound with ``structlog.contextvars`` (e.g. ``request_id``) is added to every line logged
during that request, including lines from stdlib loggers.
"""

import logging
import sys
from typing import TextIO

import structlog
from structlog.typing import Processor

_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Route all logging through a JSON renderer. Safe to call more than once (tests do)."""
    structlog.configure(
        processors=[*_SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers; send its records through ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
    # Our middleware writes the access log (with request_id and timings); silence uvicorn's.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every request at INFO, which would put full upstream URLs in our logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
