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
- **Choice:** cap total characters (`TOKENGATE_MAX_INPUT_CHARS`), message count and `max_completion_tokens`; only model aliases in `TOKENGATE_MODEL_ROUTES` are accepted (was `TOKENGATE_ALLOWED_MODELS` until step 1.4; see D16).
- **Why:** tokenizers are model-specific and cost CPU on the hot path; a character cap is a cheap first guard against cost and memory abuse. Accurate token counts come from the provider's usage data (Phase 4). Aliases (`mock-1`, later e.g. `fast`) let us change the real model behind a name, including failover, without client changes.

## D13. Validation errors: 400 in OpenAI's envelope, never echoing input
- **Options:** FastAPI's default 422 with pydantic's error list; OpenAI-style 400.
- **Choice:** 400 with `{"error": {"message", "type", "param", "code"}}`, reporting the first error and the count of others. 404/405/413/500 use the same envelope.
- **Why:** OpenAI SDKs turn this envelope into typed exceptions, so clients behave correctly. FastAPI's default includes the rejected `input` value, which could copy a user's prompt into responses and logs; ours names the field only. A test proves that by planting a canary string. 500s never include exception text.

## D14. Body-size limit as ASGI middleware that counts bytes as they arrive
- **Options:** trust `Content-Length`; limit at a reverse proxy only; count bytes in middleware.
- **Choice:** middleware that rejects early on a large `Content-Length`, but also counts bytes while reading (chunked uploads have no header, and the header can lie), then replays the buffered body to the app.
- **Why:** rejects a 50 MB body after reading ~1 MB instead of after parsing all of it. Buffering costs nothing extra because the app buffers the body to parse JSON anyway. Cloud Run caps request size too, but defence in depth keeps the limit testable and the same everywhere.

## D15. Provider interface uses neutral internal types
- **Options:** pass OpenAI-shaped requests straight to every adapter; define our own request/response types.
- **Choice:** `ProviderRequest`, `ProviderResponse`, `StreamEvent`, `ProviderError(kind, retryable)` in `app/providers/base.py`; each adapter converts to and from its provider's wire format.
- **Why:** failover (Phase 3) and caching (Phase 5) must not care which provider answered. `ProviderError.retryable` lets the resilience layer decide on retries without knowing provider-specific error codes. Every request carries `max_output_tokens`, so the gateway never sends an unbounded request (a cost guardrail).

## D16. Model aliases routed by a config table
- **Options:** clients name real provider models; aliases mapped in code; aliases mapped in config.
- **Choice:** `TOKENGATE_MODEL_ROUTES`, a JSON map such as `{"mock-1": "mock/echo-v1"}`. The allowed models are exactly the aliases in the table (this replaced `TOKENGATE_ALLOWED_MODELS`). A route to an unknown provider stops startup.
- **Why:** swapping the model behind `fast` is a config change with no client changes, and a typo fails at deploy time instead of on the first request that uses it.

## D17. Streaming: own SSE response, first event before 200, in-stream errors, prompt cancellation
- **Options:** Starlette's `StreamingResponse` as-is; a custom ASGI response.
- **Choice:** `EventStreamResponse` always watches for client disconnects and always closes the provider stream. The endpoint waits for the provider's first event before sending headers. Failures after that are sent as an SSE `{"error": ...}` event with no `[DONE]`.
- **Why:** since Starlette 1.x, `StreamingResponse` only watches for disconnects when the server speaks ASGI < 2.4 (uvicorn sends 2.3 today, which could change on an upgrade). Without that, a client that hangs up keeps a provider generating, and billing, tokens nobody reads. A test with an endless provider proves the cancellation; it times out if the watcher is removed. Fetching the first event first means immediate provider failures get a real 5xx status.

## D18. HTTP status for provider trouble says whose problem it is
- **Options:** always 500; map by failure kind.
- **Choice:** 502 `upstream_error` (provider failed), 504 `upstream_timeout` (provider too slow), 503 `upstream_unavailable` (nothing available; used by the Phase 3 circuit breaker). Messages are generic; provider details go to logs only.
- **Why:** on-call can tell a gateway bug (500) from an upstream incident (502/504) at a glance, and clients/SDKs retry 5xx appropriately.

## D19. MockProvider: deterministic, fingerprint-based, word "tokens"
- **Options:** echo the prompt; random text; text derived from a hash of the request.
- **Choice:** text derived from a SHA-256 fingerprint of the request (stable across processes, unlike `hash()`); configurable latency and failure rates, with an optional seed so chaos runs are reproducible. Token counts are word counts.
- **Why:** the same question always gets the same answer, which cache tests need; the prompt is never echoed into responses or logs. Word counts are explicitly fake and never used for billing; real counts come from provider usage data (Phase 4).

## D20. Metrics: Prometheus, bounded labels, one registry per app
- **Options:** Prometheus pull (`prometheus-client`); OpenTelemetry metrics; logs-only.
- **Choice:** `prometheus-client` with a per-app `CollectorRegistry`. Labels come only from fixed sets: known routes (else `"other"`), known HTTP methods (else `"other"`), status class, provider name, configured model alias, outcome. Overhead buckets start at 0.5 ms.
- **Why:** an unbounded label (raw URL, user ID, prompt) lets any scanner create unlimited time series and copies private data into dashboards; tests prove random paths and a prompt canary never appear. A per-app registry keeps tests isolated. Per-tenant labels wait for Phase 2, and only with a bounded tenant set. OpenTelemetry joins in Phase 6.

## D21. Protecting /metrics
- **Options:** open endpoint; separate port; bearer token; push instead of pull.
- **Choice:** `TOKENGATE_METRICS_TOKEN` (a `SecretStr`, ≥16 chars, compared in constant time). No token: open in local/test, 404 in prod.
- **Why:** Cloud Run exposes one public port, so a separate metrics port isn't available, and an open page would publish traffic and error rates. Prod fails closed if the token is forgotten. Push to Grafana Cloud via OpenTelemetry is the Phase 6 option.

## D22. Container: multi-stage uv build, non-root, digest-pinned, `python -m app`
- **Options:** single-stage image; distroless; slim multi-stage.
- **Choice:** `python:3.12-slim` (pinned by digest) with `uv sync --locked --no-dev` in a build stage; the runtime stage has only the venv and `app/`, runs as UID 10001, and starts with `python -m app` (exec form, so Python gets SIGTERM directly).
- **Why:** reproducible (lockfile + digests), small attack surface (no build tools, no tests, no `.env` thanks to an allow-list `.dockerignore`), and graceful shutdown inside Cloud Run's 10 s SIGTERM window (`TOKENGATE_GRACEFUL_SHUTDOWN_TIMEOUT_S=8`). `python -m app` reads `PORT`, which an exec-form CMD can't expand. Distroless was set aside for now because debugging without a shell is harder at this stage. The image is built and smoke-tested in CI only (no local Docker yet); CI writes the measured image size to the job summary.

## D23. Cloud Run cost guardrails, decided before the first deploy
- **Options:** defaults; min-instances 1 for no cold starts; scale-to-zero with caps.
- **Choice:** `minScale 0`, `maxScale 2`, 1 vCPU / 512 MiB, CPU billed only during requests, startup probe on `/readyz`, liveness on `/healthz`, metrics token from Secret Manager (pinned version), dedicated runtime service account with only `secretAccessor` on that secret.
- **Why:** an idle portfolio service should cost nothing, and a traffic spike or loop can't scale the bill past two instances. Cold starts are the accepted trade-off. Field names were checked against Google's current Cloud Run YAML reference. Not deployed until Phase 6, after a budget alert exists.

## D29. Database migrations: numbered plain-SQL files and a small runner
- **Options:** Alembic (SQLAlchemy's migration tool); plain SQL files with a small runner.
- **Choice:** `app/db/migrations/NNNN_name.sql`, applied by `python -m app.db.migrate`. Each file runs in one transaction with its `schema_migrations` row; applied files are checksummed (SHA-256) and an edit stops the run; a Postgres advisory lock serialises concurrent runners.
- **Why:** with a handful of tables, plain SQL is easier to read, review and defend than generated Alembic scripts, and it keeps SQLAlchemy out of a project that uses asyncpg directly. The three guarantees that matter (all-or-nothing per file, no silent edits, no concurrent runs) are each covered by a test. Revisit Alembic if the schema grows large or needs autogenerated diffs.

## D30. asyncpg pool: small, lazy, and bounded twice
- **Options:** SQLAlchemy async; asyncpg directly. Pool sizing for Cloud Run.
- **Choice:** asyncpg directly, `max_size=5`, `min_size=init_size=0`. Every query is bounded by both a client timeout (`command_timeout`) and Postgres's own `statement_timeout` (both `TOKENGATE_DB_QUERY_TIMEOUT_S`, default 2 s); connecting is bounded separately.
- **Why:** the gateway's queries are a few simple lookups, so an ORM adds overhead without much benefit. Small pools matter because connections are a scarce resource on free Postgres tiers and multiply by instance count (2 instances × 5 = 10). The server-side timeout still protects the database if a client gives up without cancelling; a test shows each timeout firing on its own.

## D31. A database outage makes an instance "not ready", never "dead"
- **Options:** fail startup when Postgres is down; fail liveness; fail readiness only.
- **Choice:** the pool opens no connections at startup, so the app always boots; `/readyz` runs `SELECT 1` with a 1 s limit and returns 503 when it fails; `/healthz` never touches the database (D9). The 503 body doesn't say which dependency failed; the log does.
- **Why:** if a database blip killed liveness or startup, Cloud Run would restart every instance at once and turn a short outage into a longer one. Not-ready instances just stop receiving traffic and recover by themselves when Postgres returns.

## D32. Key fingerprints: HMAC-SHA256 with a server-side pepper, not bcrypt/argon2
- **Options:** store keys as-is; plain SHA-256; bcrypt/argon2; HMAC-SHA256 with a pepper.
- **Choice:** store `HMAC-SHA256(pepper, secret)` (32 bytes). The pepper is `TOKENGATE_API_KEY_PEPPER` (≥32 chars, a `SecretStr`), held outside the database. Comparison is constant-time.
- **Why:** bcrypt/argon2 are deliberately slow to protect *low-entropy human passwords*. Our secrets are ~256 random bits, so a fast hash is already unguessable, and a slow hash would add tens to hundreds of ms to every request. The pepper means a leaked database alone can't be used to test guesses. Trade-off: rotating the pepper invalidates every key, so rotation means reissuing keys (a versioned pepper can be added if that becomes routine).

## D33. Auth cache: 60 s, bounded, caches misses too
- **Options:** query Postgres on every request; cache hits only; cache hits and misses.
- **Choice:** in-process cache keyed by prefix, TTL `TOKENGATE_AUTH_CACHE_TTL_S` (60 s), at most `TOKENGATE_AUTH_CACHE_MAX_ENTRIES` (LRU eviction). Misses ("no such prefix") are cached as well.
- **Why:** keeps the database off the hot path. Caching misses stops a flood of made-up keys from becoming a flood of queries; the size cap stops that flood from exhausting memory instead. The cost: **a revoked key or disabled tenant can keep working for up to 60 s per instance**. The admin CLI says so when revoking, and a fake-clock test pins the behaviour. Concurrent misses for the same prefix can each query once (no request coalescing); acceptable at this scale.

## D34. Key format `tg_<prefix>_<secret>`, plus a secret-scanner rule
- **Options:** opaque random string; UUID; prefixed key with a lookup part.
- **Choice:** `tg_` + 12-char public prefix + 43-char secret (alphabet A-Z a-z 0-9). `.gitleaks.toml` adds a `tokengate-api-key` rule on top of gitleaks' defaults.
- **Why:** the prefix gives an indexed lookup without scanning and is safe to show in logs and listings; the `tg_` marker makes a leaked key recognisable, so our own pre-commit hook and CI catch it before it reaches git history (verified against a freshly generated key).

## D35. One identical 401 for every auth failure; fail closed when the database is down
- **Options:** specific error per reason; one generic 401.
- **Choice:** every failure (missing, malformed, unknown, wrong secret, revoked, tenant disabled) returns the same OpenAI-style `invalid_api_key` body with `WWW-Authenticate: Bearer`. The reason goes only to logs and `tokengate_auth_failures_total{reason}`. The secret is verified *before* revocation/disabled status, so status is only revealed to someone holding the real key. If the key lookup itself fails (database down), the answer is 503 `auth_unavailable`: nobody gets in, but clients aren't told their key is bad.
- **Why:** distinct messages let an attacker enumerate prefixes or learn which keys were revoked; operators still get full detail from logs and metrics. `TOKENGATE_AUTH=disabled` exists for local development and tests only and is refused when `TOKENGATE_ENV=prod`, as is prod without a database or pepper.
