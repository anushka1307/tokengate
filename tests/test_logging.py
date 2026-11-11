import io
import json
import logging
from typing import Any

import httpx
import structlog

from app.telemetry.middleware import REQUEST_ID_HEADER


def _lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    """Every line must be a standalone JSON object; json.loads fails the test otherwise."""
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_structlog_lines_are_json_with_bound_context(log_output: io.StringIO) -> None:
    with structlog.contextvars.bound_contextvars(request_id="req-1"):
        structlog.get_logger("test").info("hello", answer=42)

    (line,) = _lines(log_output)
    assert line["event"] == "hello"
    assert line["answer"] == 42
    assert line["level"] == "info"
    assert line["logger"] == "test"
    assert line["request_id"] == "req-1"
    assert "timestamp" in line


def test_stdlib_loggers_use_the_same_json_format(log_output: io.StringIO) -> None:
    logging.getLogger("some.library").warning("from stdlib %s", "logger")

    (line,) = _lines(log_output)
    assert line["event"] == "from stdlib logger"
    assert line["level"] == "warning"


def test_exceptions_are_logged_as_structured_json(log_output: io.StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("test").exception("failed")

    (line,) = _lines(log_output)
    assert line["event"] == "failed"
    assert line["exception"][0]["exc_type"] == "ValueError"


def test_logs_below_configured_level_are_dropped(log_output: io.StringIO) -> None:
    structlog.get_logger("test").debug("noise")
    assert _lines(log_output) == []


async def test_access_log_line_per_request(
    client: httpx.AsyncClient, log_output: io.StringIO
) -> None:
    resp = await client.get("/healthz?api_key=should-not-be-logged")

    access = [line for line in _lines(log_output) if line.get("logger") == "tokengate.access"]
    assert len(access) == 1
    line = access[0]
    assert line["request_id"] == resp.headers[REQUEST_ID_HEADER]
    assert line["method"] == "GET"
    assert line["path"] == "/healthz"
    assert line["status"] == 200
    assert line["duration_ms"] >= line["overhead_ms"] >= 0
    assert "should-not-be-logged" not in log_output.getvalue()
