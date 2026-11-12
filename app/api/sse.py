"""Server-Sent Events delivery for streamed completions (docs/DECISIONS.md, D17).

Wire format, as OpenAI SDKs expect it::

    data: {"id": ..., "object": "chat.completion.chunk", "choices": [...]}\\n\\n
    ...
    data: [DONE]\\n\\n

Why not Starlette's StreamingResponse: since Starlette 1.x it only watches for client
disconnects when the server speaks ASGI spec < 2.4, and it doesn't promise to close the body
iterator promptly. Here, a disconnect always cancels the stream immediately and the upstream
provider stream is always closed, so we stop paying for tokens nobody will read.
"""

import json
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import anyio
import structlog
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

log = structlog.get_logger("tokengate.sse")

DONE = b"data: [DONE]\n\n"


def sse_event(payload: Mapping[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


class EventStreamResponse(StreamingResponse):
    def __init__(
        self, events: AsyncGenerator[bytes, None], headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(
            events,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # tell proxies not to buffer the stream
                **(headers or {}),
            },
        )
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        client_gone = False
        # True once the client has everything it needs ([DONE] sent). SDKs often close the
        # connection right after [DONE]; that is a normal ending, not a mid-stream hang-up.
        complete = False
        try:
            async with anyio.create_task_group() as task_group:

                async def watch_for_disconnect() -> None:
                    nonlocal client_gone
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            client_gone = not complete
                            task_group.cancel_scope.cancel()
                            return

                task_group.start_soon(watch_for_disconnect)
                try:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": self.status_code,
                            "headers": self.raw_headers,
                        }
                    )
                    async for chunk in self._events:
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                        complete = complete or chunk == DONE
                    complete = True
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except OSError:
                    client_gone = not complete  # ASGI >= 2.4 servers signal a gone client so
                task_group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await self._events.aclose()  # closes the provider stream too
            if client_gone:
                log.info("client_disconnected_mid_stream")
