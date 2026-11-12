"""OpenAI-compatible chat-completions request, response and stream-chunk models.

Field names and shapes follow the official ``openai`` Python SDK (checked against openai 3.49).
Policy (docs/DECISIONS.md, D11):
- features that would change what the answer *means* (tools, JSON mode, images, n>1, ...) are
  rejected with ``unsupported_parameter`` rather than silently dropped;
- unknown fields are ignored and never forwarded to a provider.

Limits that depend on configuration (allowed models, sizes) live in ``app/api/limits.py``.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

# Request fields we recognise but don't support yet. Present-and-truthy -> 400.
UNSUPPORTED_REQUEST_FIELDS: tuple[str, ...] = (
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "functions",
    "function_call",
    "response_format",
    "logprobs",
    "top_logprobs",
    "logit_bias",
    "audio",
    "modalities",
    "prediction",
    "web_search_options",
)

SUPPORTED_CONTENT_PART_TYPES = frozenset({"text"})


def unsupported(field: str, detail: str = "") -> PydanticCustomError:
    """A validation error the error handler reports as code ``unsupported_parameter``."""
    suffix = f" ({detail})" if detail else ""
    return PydanticCustomError(
        "unsupported_parameter",
        "'{field}' is not supported by this gateway yet" + suffix,
        {"field": field},
    )


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# ---------------------------------------------------------------- request


class TextContentPart(_Model):
    type: Literal["text"]
    text: str


class ChatMessage(_Model):
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextContentPart] = Field(union_mode="left_to_right")
    name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _reject_unsupported(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        role = data.get("role")
        if role in ("tool", "function"):
            raise unsupported("role", f"role '{role}' requires tool calling")
        for key in ("tool_calls", "function_call", "audio"):
            if data.get(key):
                raise unsupported(key)
        content = data.get("content")
        if isinstance(content, list):
            for part in content:
                kind = part.get("type") if isinstance(part, dict) else None
                if kind is not None and kind not in SUPPORTED_CONTENT_PART_TYPES:
                    raise unsupported("content", f"only text content is supported, got '{kind}'")
        return data

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text for part in self.content)


class StreamOptions(_Model):
    include_usage: bool = False


class ChatCompletionRequest(_Model):
    model: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1)

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)  # deprecated by OpenAI; still accepted
    stop: str | list[str] | None = Field(default=None)
    seed: int | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    user: str | None = Field(default=None, max_length=256)
    n: int | None = None

    stream: bool = False
    stream_options: StreamOptions | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_unsupported(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in UNSUPPORTED_REQUEST_FIELDS:
            if data.get(key):
                raise unsupported(key)
        if data.get("n") not in (None, 1):
            raise unsupported("n", "only n=1 is supported")
        return data

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if isinstance(self.stop, list) and len(self.stop) > 4:
            raise PydanticCustomError(
                "invalid_value", "'stop' accepts at most 4 sequences", {"field": "stop"}
            )
        if self.stream_options is not None and not self.stream:
            raise PydanticCustomError(
                "invalid_value",
                "'stream_options' is only allowed when 'stream' is true",
                {"field": "stream_options"},
            )
        if (
            self.max_tokens is not None
            and self.max_completion_tokens is not None
            and self.max_tokens != self.max_completion_tokens
        ):
            raise PydanticCustomError(
                "invalid_value",
                "set 'max_completion_tokens' or 'max_tokens', not both with different values",
                {"field": "max_tokens"},
            )
        return self

    @property
    def requested_max_tokens(self) -> int | None:
        return (
            self.max_completion_tokens
            if self.max_completion_tokens is not None
            else (self.max_tokens)
        )

    @property
    def input_chars(self) -> int:
        return sum(len(m.text) + len(m.name or "") for m in self.messages)


# ---------------------------------------------------------------- response

FinishReason = Literal["stop", "length", "content_filter"]


class Usage(_Model):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AssistantMessage(_Model):
    role: Literal["assistant"] = "assistant"
    content: str | None
    refusal: str | None = None


class Choice(_Model):
    index: int = Field(ge=0)
    message: AssistantMessage
    finish_reason: FinishReason
    logprobs: None = None


class ChatCompletionResponse(_Model):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int  # unix seconds
    model: str  # the alias the client asked for
    choices: list[Choice]
    usage: Usage | None = None


class ChunkDelta(_Model):
    role: Literal["assistant"] | None = None
    content: str | None = None


class ChunkChoice(_Model):
    index: int = Field(ge=0)
    delta: ChunkDelta
    finish_reason: FinishReason | None = None
    logprobs: None = None


class ChatCompletionChunk(_Model):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    # Only on the final chunk, and only when stream_options.include_usage is true.
    usage: Usage | None = None
