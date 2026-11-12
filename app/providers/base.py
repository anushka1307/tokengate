"""The contract every model provider adapter implements (docs/DECISIONS.md, D15).

These types are provider-neutral on purpose: they are neither OpenAI's wire format nor Gemini's.
The API layer converts client requests into ``ProviderRequest``; each adapter converts that into
its own provider's format. Nothing outside ``app/providers`` knows which provider answered.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal, Protocol

FinishReason = Literal["stop", "length", "content_filter"]
ProviderErrorKind = Literal["upstream_error", "timeout", "unavailable"]


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    role: Literal["system", "developer", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    target_model: str  # the provider's own model name, e.g. "echo-v1" or "gemini-2.0-flash"
    messages: tuple[ProviderMessage, ...]
    max_output_tokens: int  # always set: the gateway never sends an unbounded request
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    finish_reason: FinishReason
    usage: ProviderUsage


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One step of a streamed answer. The last event carries finish_reason and usage."""

    text_delta: str = ""
    finish_reason: FinishReason | None = None
    usage: ProviderUsage | None = None


class ProviderError(Exception):
    """A provider failed. ``retryable`` tells the resilience layer (Phase 3) whether to retry."""

    def __init__(
        self, provider: str, message: str, *, kind: ProviderErrorKind, retryable: bool
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.kind = kind
        self.retryable = retryable


class Provider(Protocol):
    name: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent, None]:
        """Yield events as they arrive. Callers must ``aclose()`` it if they stop early."""
        ...
