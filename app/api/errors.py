"""Errors in OpenAI's shape: ``{"error": {"message", "type", "param", "code"}}``.

OpenAI SDKs parse this envelope into typed exceptions, so clients get useful errors from us too.
Validation errors return 400 (not FastAPI's default 422) and never echo the rejected input back,
since that input may be someone's prompt (docs/DECISIONS.md, D13).
"""

from collections.abc import Sequence
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger("tokengate.errors")

_STATUS_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    405: "invalid_request_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
}


class APIError(Exception):
    """Raise from anywhere in request handling to return an OpenAI-shaped error."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.param = param
        self.error_type = error_type or _STATUS_TYPES.get(status_code, "api_error")


def error_body(
    message: str, *, error_type: str, code: str | None = None, param: str | None = None
) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "param": param, "code": code}}


def error_response(error: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=error_body(
            error.message, error_type=error.error_type, code=error.code, param=error.param
        ),
    )


# For a field typed `str | list[...]`, pydantic adds the union branch it tried to the error
# location, e.g. ("messages", 0, "content", "str"). Clients should see "messages[0].content".
_UNION_BRANCH_TAGS = frozenset({"str", "int", "float", "bool", "list", "dict", "bytes", "none"})


def _is_union_branch_tag(part: str) -> bool:
    return part in _UNION_BRANCH_TAGS or "[" in part


def _param_from_loc(loc: Sequence[Any], ctx: dict[str, Any] | None) -> str | None:
    parts = [str(p) for p in loc if p != "body" and not _is_union_branch_tag(str(p))]
    field = (ctx or {}).get("field")
    if field is not None:
        parts.append(str(field))
    if not parts:
        return None
    # messages, 0, role -> messages[0].role
    out = parts[0]
    for part in parts[1:]:
        out += f"[{part}]" if part.isdigit() else f".{part}"
    return out


def validation_error_to_api_error(errors: Sequence[Any]) -> APIError:
    """Report the first problem. The pydantic ``input`` value is deliberately never used."""
    first = errors[0]
    kind = first.get("type", "")
    loc = first.get("loc", ())
    ctx = first.get("ctx")

    if kind == "json_invalid":
        return APIError(400, "Request body is not valid JSON.", code="invalid_json")
    if kind == "missing" and tuple(loc) == ("body",):
        return APIError(400, "Request body is required.", code="missing_required_parameter")

    param = _param_from_loc(loc, ctx)
    if kind == "missing":
        message, code = f"Missing required parameter: '{param}'.", "missing_required_parameter"
    elif kind == "unsupported_parameter":
        message, code = f"{first['msg']}.", "unsupported_parameter"
    else:
        message, code = f"Invalid value for '{param}': {first['msg']}.", "invalid_value"
        if param is None:
            message = f"{first['msg']}."

    extra = len(errors) - 1
    if extra > 0:
        message += f" ({extra} more validation error{'s' if extra > 1 else ''})"
    return APIError(400, message, code=code, param=param)


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    return error_response(validation_error_to_api_error(exc.errors()))


async def _handle_api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)  # noqa: S101 - registered for this type only
    return error_response(exc)


async def _handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for this type only
    error = APIError(exc.status_code, str(exc.detail))
    response = error_response(error)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    # Details go to the log, never to the client. Starlette runs this outermost (outside our
    # request-context middleware), so correlate via the access-log line with status 500.
    log.error("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=error_body("Internal server error.", error_type="api_error"),
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(APIError, _handle_api_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
