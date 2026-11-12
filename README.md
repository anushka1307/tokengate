# TokenGate

A multi-tenant LLM gateway: one chokepoint in front of LLM providers that handles auth, rate limits,
budgets, caching, failover, streaming, and cost attribution. It exposes an OpenAI-compatible
`POST /v1/chat/completions`, so any OpenAI SDK can point at it.

**Status:** Phase 1 in progress: OpenAI-compatible endpoint with streaming, backed by a mock model. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Measured numbers live only in [docs/RESULTS.md](docs/RESULTS.md); design trade-offs in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Development
Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself.

```bash
uv sync                      # create .venv from uv.lock
uv run pre-commit install    # ruff, mypy, gitleaks on every commit
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

## Run locally
```bash
uv run uvicorn app.main:create_app --factory --no-access-log
curl -i localhost:8000/healthz
curl -i localhost:8000/readyz

# Ask the mock model (no API keys, no cost)
curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "mock-1", "messages": [{"role": "user", "content": "hello"}]}'

# Streaming
curl -N localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "mock-1", "stream": true, "messages": [{"role": "user", "content": "hello"}]}'
```

Any OpenAI SDK works too: point `base_url` at `http://localhost:8000/v1` (any API key until Phase 2).
