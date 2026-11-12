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

## D11. Request strictness: reject meaning-changing features, ignore unknown fields
- **Options:** reject any unknown field; accept everything and forward it; reject only what we can't honour.
- **Choice:** recognised-but-unsupported features (`tools`, `response_format`, `logprobs`, `n>1`, image/audio content, ...) get a 400 `unsupported_parameter`; truly unknown fields are ignored and never forwarded.
- **Why:** silently dropping `response_format: json_object` would return prose to a client expecting JSON, which is worse than an error. Rejecting unknown fields would break clients whenever the SDK adds a field. Not forwarding them keeps untrusted input away from providers. Tool calling is planned for Phase 3, before PipelineMedic needs it.

## D12. Input size limits in characters, model access by alias allowlist
- **Options:** count tokens with each model's tokenizer; cap characters; no cap.
- **Choice:** cap total characters (`TOKENGATE_MAX_INPUT_CHARS`), message count and `max_completion_tokens`; only model aliases in `TOKENGATE_ALLOWED_MODELS` are accepted.
- **Why:** tokenizers are model-specific and cost CPU on the hot path; a character cap is a cheap first guard against cost and memory abuse. Accurate token counts come from the provider's usage data (Phase 4). Aliases (`mock-1`, later e.g. `fast`) let us change the real model behind a name, including failover, without client changes.

## D13. Validation errors: 400 in OpenAI's envelope, never echoing input
- **Options:** FastAPI's default 422 with pydantic's error list; OpenAI-style 400.
- **Choice:** 400 with `{"error": {"message", "type", "param", "code"}}`, reporting the first error and the count of others. 404/405/413/500 use the same envelope.
- **Why:** OpenAI SDKs turn this envelope into typed exceptions, so clients behave correctly. FastAPI's default includes the rejected `input` value, which could copy a user's prompt into responses and logs; ours names the field only. A test proves that by planting a canary string. 500s never include exception text.

## D14. Body-size limit as ASGI middleware that counts bytes as they arrive
- **Options:** trust `Content-Length`; limit at a reverse proxy only; count bytes in middleware.
- **Choice:** middleware that rejects early on a large `Content-Length`, but also counts bytes while reading (chunked uploads have no header, and the header can lie), then replays the buffered body to the app.
- **Why:** rejects a 50 MB body after reading ~1 MB instead of after parsing all of it. Buffering costs nothing extra because the app buffers the body to parse JSON anyway. Cloud Run caps request size too, but defence in depth keeps the limit testable and the same everywhere.
