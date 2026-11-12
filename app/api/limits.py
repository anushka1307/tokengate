"""Request size and policy limits.

- ``BodySizeLimitMiddleware`` rejects oversized bodies with 413 *before* they are parsed, and
  doesn't trust ``Content-Length`` (chunked uploads, or a client that lies) (D14).
- ``enforce_request_limits`` applies the config-dependent rules to a parsed request: model
  allowlist, message count, input size, and max output tokens (D12).
"""

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import APIError, error_response
from app.api.schemas import ChatCompletionRequest
from app.config import Settings

# ASCII digits only, at most 18 of them (fits in an int64; anything longer is nonsense anyway).
_CONTENT_LENGTH = re.compile(rb"[0-9]{1,18}")


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared == -1:
            await self._reject(
                scope,
                receive,
                send,
                APIError(400, "Invalid Content-Length header.", code="invalid_header"),
            )
            return
        if declared is not None and declared > self.max_bytes:
            await self._reject_too_large(scope, receive, send)
            return

        # Read the body ourselves, stopping as soon as it exceeds the cap. The app would buffer
        # it anyway to parse JSON, so this costs no extra memory beyond max_bytes.
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # client left before finishing the upload; nothing to answer
            chunk: bytes = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._reject_too_large(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # After the body, pass through so streaming responses still see client disconnects.
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        """None if absent, -1 if malformed, else the declared byte count."""
        for name, value in scope["headers"]:
            if name == b"content-length":
                # Not str.isdigit(): it accepts "²" and other Unicode digits that int() rejects.
                return int(value) if _CONTENT_LENGTH.fullmatch(value) else -1
        return None

    async def _reject_too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = f"Request body exceeds the {self.max_bytes}-byte limit."
        await self._reject(scope, receive, send, APIError(413, message, code="request_too_large"))

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, error: APIError) -> None:
        response = error_response(error)
        response.headers["Connection"] = "close"  # don't keep reading an oversized upload
        await response(scope, receive, send)


def enforce_request_limits(request: ChatCompletionRequest, settings: Settings) -> None:
    """Raise ``APIError(400)`` if the request breaks a configured limit."""
    if request.model not in settings.allowed_models:
        # The client's value isn't echoed back; the allowlist is not secret.
        raise APIError(
            400,
            "The requested model is not available. Allowed: " + ", ".join(settings.allowed_models),
            code="model_not_found",
            param="model",
        )
    if len(request.messages) > settings.max_messages:
        raise APIError(
            400,
            f"Too many messages: at most {settings.max_messages} are allowed.",
            code="invalid_value",
            param="messages",
        )
    if request.input_chars > settings.max_input_chars:
        raise APIError(
            400,
            f"Input too long: at most {settings.max_input_chars} characters are allowed.",
            code="context_length_exceeded",
            param="messages",
        )
    requested = request.requested_max_tokens
    if requested is not None and requested > settings.max_completion_tokens:
        raise APIError(
            400,
            f"max_completion_tokens may be at most {settings.max_completion_tokens}.",
            code="invalid_value",
            param="max_completion_tokens",
        )
