# TokenGate

A multi-tenant LLM gateway: one chokepoint in front of LLM providers that handles auth, rate limits,
budgets, caching, failover, streaming, and cost attribution. It exposes an OpenAI-compatible
`POST /v1/chat/completions`, so any OpenAI SDK can point at it.

**Status:** Phase 1 done (OpenAI-compatible endpoint with streaming, mock model, metrics, container). Phase 2 (auth, rate limits, budgets) in progress. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Measured numbers live only in [docs/RESULTS.md](docs/RESULTS.md); design trade-offs in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Development
Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself.

```bash
uv sync                      # create .venv from uv.lock
uv run pre-commit install    # ruff, mypy, gitleaks + commit-message checks on every commit
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

### Database (Postgres 17)
```bash
brew install postgresql@17 && brew services start postgresql@17
createdb tokengate_dev && createdb tokengate_test
TOKENGATE_DATABASE_URL=postgresql://localhost/tokengate_dev uv run python -m app.db.migrate
TOKENGATE_TEST_DATABASE_URL=postgresql://localhost/tokengate_test uv run pytest   # runs DB tests too
```
Without `TOKENGATE_TEST_DATABASE_URL` the database tests are skipped locally; CI always runs them.

## Run locally
```bash
uv run python -m app            # listens on $PORT (default 8000)
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

## Container
The image is built and smoke-tested by CI on every pull request (`container` job), for
`linux/amd64` like Cloud Run. Locally, with Docker running:
```bash
docker compose up --build        # gateway on localhost:8000
scripts/smoke.sh                 # BASE_URL / METRICS_TOKEN env vars to point it elsewhere
```
`deploy/cloudrun.yaml` describes the Cloud Run service (scale to zero, max 2 instances). It is
not deployed yet.

## Metrics
`GET /metrics` (Prometheus format). Requires `Authorization: Bearer $TOKENGATE_METRICS_TOKEN`
when that is set; without a token it is open locally and disabled in prod.
