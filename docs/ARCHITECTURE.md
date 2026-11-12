# Architecture

TODO: request-path diagram (auth → rate limit → cache → provider → accounting) at the end of Phase 1.

Today (end of Phase 1): request context (request ID, timing, metrics) → body-size limit → schema validation → request limits → model router → provider (MockProvider) → JSON or SSE response. Also: `/healthz`, `/readyz`, protected `/metrics`; container built and smoke-tested in CI.

## Request path (target)
1. Auth: hashed tenant API key lookup
2. Rate limit + daily budget (Redis, single Lua script)
3. Cache tier 1: exact match (Redis)
4. Cache tier 2: semantic (pgvector), deterministic requests only
5. Provider adapter with timeouts, retries, circuit breaker (Gemini → Groq; MockProvider for tests)
6. Cost accounting: async batch writes, off the hot path

## Phases
| Phase | Scope | Status |
|---|---|---|
| 1 | Skeleton, OpenAI-compatible endpoint, MockProvider, health/metrics, container | done |
| 2 | Auth + rate limits + budgets | in progress |
| 3 | Real providers + resilience | TODO |
| 4 | Cost accounting | TODO |
| 5 | Exact + semantic cache, false-hit evals | TODO |
| 6 | OpenTelemetry, k6 load tests, Cloud Run | TODO |
