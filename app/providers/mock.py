"""A pretend model provider for local development, tests, load tests and chaos tests.

- Deterministic: the same request always produces the same answer (needed for cache tests).
  The answer is generated from a fingerprint of the request; it never echoes user text.
- Configurable latency (before the first token, and per token) and failure rates.
- "Tokens" are whitespace-separated words. These counts are fake and must never be used for
  billing (docs/DECISIONS.md, D19).
"""

import asyncio
import hashlib
import json
import random
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass

from app.providers.base import (
    FinishReason,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    StreamEvent,
)

_VOCABULARY = (
    "gateway", "token", "latency", "cache", "stream", "budget", "replica", "shard", "quorum",
    "backoff", "jitter", "circuit", "breaker", "lease", "partition", "offset", "schema",
    "pipeline", "window", "bucket", "queue", "batch", "index", "vector", "embedding", "tenant",
    "quota", "retry", "timeout", "failover",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class MockConfig:
    first_token_delay_s: float = 0.0
    per_token_delay_s: float = 0.0
    error_rate: float = 0.0  # chance the request fails before any output
    mid_stream_error_rate: float = 0.0  # chance a stream fails after its first token
    seed: int | None = None  # seeds the failure dice, so chaos runs are reproducible


class MockProvider:
    name = "mock"

    def __init__(self, config: MockConfig | None = None) -> None:
        self.config = config or MockConfig()
        # Not used for anything security-related: only decides simulated failures.
        self._dice = random.Random(self.config.seed)  # noqa: S311

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        words, finish_reason = self._answer(request)
        self._maybe_fail(self.config.error_rate, "simulated failure")
        await asyncio.sleep(
            self.config.first_token_delay_s + self.config.per_token_delay_s * len(words)
        )
        return ProviderResponse(
            text=" ".join(words),
            finish_reason=finish_reason,
            usage=ProviderUsage(_count_prompt_tokens(request), len(words)),
        )

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent, None]:
        words, finish_reason = self._answer(request)
        self._maybe_fail(self.config.error_rate, "simulated failure")
        await asyncio.sleep(self.config.first_token_delay_s)
        for i, word in enumerate(words):
            if i == 1:
                self._maybe_fail(self.config.mid_stream_error_rate, "simulated mid-stream failure")
            yield StreamEvent(text_delta=word if i == 0 else f" {word}")
            if self.config.per_token_delay_s:
                await asyncio.sleep(self.config.per_token_delay_s)
        yield StreamEvent(
            finish_reason=finish_reason,
            usage=ProviderUsage(_count_prompt_tokens(request), len(words)),
        )

    # ------------------------------------------------------------------ internals

    def _maybe_fail(self, rate: float, message: str) -> None:
        if rate > 0 and self._dice.random() < rate:
            raise ProviderError(self.name, message, kind="upstream_error", retryable=True)

    @staticmethod
    def _answer(request: ProviderRequest) -> tuple[list[str], FinishReason]:
        fingerprint = _fingerprint(request)
        picker = random.Random(fingerprint)  # noqa: S311 - deterministic text, not security
        length = 12 + fingerprint % 37  # 12..48 words
        words = [f"Mock answer {fingerprint % 0x10000:04x}:"]
        words += [picker.choice(_VOCABULARY) for _ in range(length)]
        words = " ".join(words).split()

        finish_reason: FinishReason = "stop"
        if request.stop:
            text = " ".join(words)
            cut = min((i for s in request.stop if (i := text.find(s)) != -1), default=-1)
            if cut != -1:
                words = text[:cut].split()
        if len(words) > request.max_output_tokens:
            words, finish_reason = words[: request.max_output_tokens], "length"
        return words, finish_reason


def _fingerprint(request: ProviderRequest) -> int:
    """Stable across processes (unlike hash()), so every replica gives the same answer."""
    canonical = json.dumps(asdict(request), sort_keys=True, separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:8], "big")


def _count_prompt_tokens(request: ProviderRequest) -> int:
    return sum(len(m.content.split()) for m in request.messages)
