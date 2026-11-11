# Design decisions

Each entry lists the options, the choice, and why. Newest at the bottom.

## D1. Language and framework: Python 3.12 + FastAPI (async)
- **Options:** FastAPI, Node/Express, Go.
- **Choice:** FastAPI on uvicorn, fully async.
- **Why:** pydantic v2 validation and an OpenAI-compatible schema come almost for free, and later phases (embeddings, evals) live in the Python ML ecosystem. Go would have lower overhead; we accept that and will *measure* gateway overhead rather than assume it.

## D2. Tooling: uv + ruff + mypy --strict + pytest
- **Options:** pip/poetry vs uv; flake8+black+isort vs ruff.
- **Choice:** uv with a committed `uv.lock`; ruff for lint and format; strict mypy.
- **Why:** one fast tool for each job, and `uv sync --locked` in CI fails when the lock is stale, so builds are reproducible.

## D3. Secret hygiene: gitleaks in pre-commit and in CI
- **Options:** pre-commit only; CI only; both.
- **Choice:** both. Pre-commit blocks secrets before they enter history; CI scans the full history in case a hook was skipped (`--no-verify`).
- **Why:** removing a secret from git history after a push is painful, and it has to be rotated anyway.

## D4. CI supply chain: actions pinned to commit SHAs, read-only token
- **Options:** pin by tag (`@v7`) or by commit SHA.
- **Choice:** SHA pins with the tag in a comment; `permissions: contents: read`; `persist-credentials: false`.
- **Why:** tags can be moved to malicious commits; SHAs cannot. A read-only token limits what a compromised step can do.

## D5. Logging: structlog rendering JSON, with stdlib logs routed through it
- **Options:** stdlib `logging` + a JSON formatter; structlog; loguru.
- **Choice:** structlog with `contextvars` binding, and one `ProcessorFormatter` so uvicorn and library logs come out as the same JSON shape.
- **Why:** binding `request_id` once per request and having it on every line (even across `await`s) is the main thing we need; structlog's processor chain is also where prompt redaction will go later. Cost: one dependency. Writing to stdout is synchronous; if profiling shows it on the hot path, switch to a `QueueHandler`.

## D6. Middleware: pure ASGI, not Starlette's `BaseHTTPMiddleware`
- **Options:** `@app.middleware("http")` / `BaseHTTPMiddleware`; a plain ASGI class.
- **Choice:** plain ASGI class that wraps `send` to add headers.
- **Why:** `BaseHTTPMiddleware` runs the endpoint in a separate task and wraps the response body, which adds per-request overhead and has known problems with streaming responses and context variables. We need SSE streaming and a low overhead number.

## D7. Defining "gateway overhead"
- **Options:** total request time; time-to-first-byte; total minus time awaiting the provider.
- **Choice:** total minus provider wait. Provider awaits are wrapped in `track_provider_wait()`, which adds to a per-request accumulator held in a `ContextVar`.
- **Why:** it is the number the gateway is responsible for, independent of how slow Gemini is that day. Limitation: response headers are sent before a streamed body finishes, so for SSE the `X-Gateway-Overhead-Ms` header covers overhead up to the first byte; the full value is in the access log (and metrics, step 1.5).

## D8. Inbound `X-Request-Id`: accept only if it matches `[A-Za-z0-9-]{1,64}`
- **Options:** always generate; always trust the client's; validate and fall back.
- **Choice:** validate, otherwise generate a UUID4.
- **Why:** propagating a caller's ID makes cross-service tracing work (PipelineMedic → TokenGate), but the header is untrusted input that ends up in logs, so arbitrary values would allow log injection or forged JSON fields.

## D9. Liveness vs. readiness
- **Options:** one health endpoint; separate liveness and readiness.
- **Choice:** `/healthz` is liveness (process answers, no dependency checks); `/readyz` is readiness (startup finished, not shutting down; Redis/Postgres checks join it in Phase 2).
- **Why:** if liveness checked Redis, one Redis outage would make the platform restart every instance at once. Readiness failing only stops traffic to that instance.

## D10. Graceful shutdown: rely on uvicorn's drain, then release resources in lifespan
- **Options:** custom SIGTERM handler; uvicorn's built-in behaviour.
- **Choice:** uvicorn's. On SIGTERM it stops accepting connections and waits for in-flight requests (bounded by `--timeout-graceful-shutdown`), then runs our lifespan shutdown, which marks the app not-ready and closes the shared HTTP client.
- **Why:** less code to get wrong. Cloud Run sends SIGTERM and allows 10 s before SIGKILL, so the timeout will be set below that in the Dockerfile (step 1.5).
