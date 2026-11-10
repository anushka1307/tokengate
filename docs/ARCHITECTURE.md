# Architecture

TODO: request-path diagram (auth → rate limit → cache → provider → accounting) once Phase 1 is in place.

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
| 1 | Skeleton, OpenAI-compatible endpoint, MockProvider, health/metrics, container | in progress |
| 2 | Auth + rate limits + budgets | TODO |
| 3 | Real providers + resilience | TODO |
| 4 | Cost accounting | TODO |
| 5 | Exact + semantic cache, false-hit evals | TODO |
| 6 | OpenTelemetry, k6 load tests, Cloud Run | TODO |
