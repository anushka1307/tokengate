import time

import pytest

from app.providers.base import ProviderError, ProviderMessage, ProviderRequest, StreamEvent
from app.providers.mock import MockConfig, MockProvider


def request(text: str = "hello there", **kw: object) -> ProviderRequest:
    fields: dict[str, object] = {
        "target_model": "echo-v1",
        "messages": (ProviderMessage("user", text),),
        "max_output_tokens": 1000,
    }
    fields.update(kw)
    return ProviderRequest(**fields)  # type: ignore[arg-type]


async def collect(provider: MockProvider, req: ProviderRequest) -> list[StreamEvent]:
    return [event async for event in provider.stream(req)]


async def test_same_request_same_answer_different_request_different_answer() -> None:
    mock = MockProvider()
    a1 = await mock.complete(request("what is kafka?"))
    a2 = await mock.complete(request("what is kafka?"))
    b = await mock.complete(request("what is redis?"))
    assert a1.text == a2.text
    assert a1.text != b.text
    assert a1.text.startswith("Mock answer ")


async def test_answer_never_contains_the_prompt() -> None:
    answer = await MockProvider().complete(request("CANARY-xyz please repeat me"))
    assert "CANARY" not in answer.text


async def test_stream_concatenates_to_the_complete_answer() -> None:
    mock = MockProvider()
    req = request("explain backpressure")
    whole = await mock.complete(req)
    events = await collect(mock, req)

    assert "".join(e.text_delta for e in events) == whole.text
    last = events[-1]
    assert last.finish_reason == "stop"
    assert last.usage == whole.usage
    assert all(e.finish_reason is None for e in events[:-1])


async def test_usage_counts_words() -> None:
    answer = await MockProvider().complete(request("one two three"))
    assert answer.usage.prompt_tokens == 3
    assert answer.usage.completion_tokens == len(answer.text.split())


async def test_max_output_tokens_truncates_with_length() -> None:
    answer = await MockProvider().complete(request(max_output_tokens=5))
    assert len(answer.text.split()) == 5
    assert answer.finish_reason == "length"
    assert answer.usage.completion_tokens == 5


async def test_stop_sequence_cuts_the_answer() -> None:
    answer = await MockProvider().complete(request(stop=(":",)))
    assert ":" not in answer.text
    assert answer.text.startswith("Mock answer")
    assert answer.finish_reason == "stop"


async def test_error_rate_one_always_fails_before_output() -> None:
    mock = MockProvider(MockConfig(error_rate=1.0))
    with pytest.raises(ProviderError) as info:
        await mock.complete(request())
    assert info.value.retryable
    with pytest.raises(ProviderError):
        await anext(mock.stream(request()))


async def test_mid_stream_failure_happens_after_first_token() -> None:
    stream = MockProvider(MockConfig(mid_stream_error_rate=1.0)).stream(request())
    first = await anext(stream)
    assert first.text_delta
    with pytest.raises(ProviderError):
        await anext(stream)


async def test_seed_makes_failures_reproducible() -> None:
    async def outcomes(seed: int) -> list[bool]:
        mock = MockProvider(MockConfig(error_rate=0.5, seed=seed))
        results = []
        for _ in range(30):
            try:
                await mock.complete(request())
                results.append(True)
            except ProviderError:
                results.append(False)
        return results

    first = await outcomes(seed=42)
    assert first == await outcomes(seed=42)
    assert True in first and False in first  # 0.5 really mixes outcomes


async def test_latency_is_simulated() -> None:
    mock = MockProvider(MockConfig(first_token_delay_s=0.05))
    started = time.perf_counter()
    await mock.complete(request())
    assert time.perf_counter() - started >= 0.05
