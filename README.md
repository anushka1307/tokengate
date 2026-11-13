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

### Redis (rate limits)
```bash
brew install redis && brew services start redis     # listens on localhost only
TOKENGATE_TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest   # runs Redis tests too
```
With `TOKENGATE_REDIS_URL` set, each API key and each tenant gets a token bucket holding one
minute of requests (`--rpm`), refilled continuously. Over the limit: `429` with `Retry-After`.
Every successful response carries `X-RateLimit-Remaining`. If Redis is down, requests are
allowed (fail open) and `tokengate_ratelimit_decisions_total{result="redis_error"}` counts it.

### API keys
`/v1/chat/completions` requires `Authorization: Bearer tg_...` unless `TOKENGATE_AUTH=disabled`
(local development only). With `TOKENGATE_DATABASE_URL` and `TOKENGATE_API_KEY_PEPPER` set:
```bash
uv run python -m app.admin create-tenant --name pipelinemedic --rpm 60 --daily-tokens 200000
uv run python -m app.admin create-key --tenant pipelinemedic --name laptop   # shown once
uv run python -m app.admin list-keys --tenant pipelinemedic
uv run python -m app.admin revoke-key --prefix <prefix>
```

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

Any OpenAI SDK works too: point `base_url` at `http://localhost:8000/v1`, with a `tg_...` key as the API key
(any string when `TOKENGATE_AUTH=disabled`).

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
