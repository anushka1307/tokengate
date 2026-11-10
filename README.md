# TokenGate

A multi-tenant LLM gateway: one chokepoint in front of LLM providers that handles auth, rate limits,
budgets, caching, failover, streaming, and cost attribution. It exposes an OpenAI-compatible
`POST /v1/chat/completions`, so any OpenAI SDK can point at it.

**Status:** Phase 1 (skeleton) in progress. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Measured numbers live only in [docs/RESULTS.md](docs/RESULTS.md); design trade-offs in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Development
Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself.

```bash
uv sync                      # create .venv from uv.lock
uv run pre-commit install    # ruff, mypy, gitleaks on every commit
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
