# Measured results

Every number here must name the script and command that produced it, the git commit, and the
environment. Nothing is estimated. Unmeasured rows stay TODO.

| Metric | Value | Command | Commit | Environment |
|---|---|---|---|---|
| Gateway-added p50 / p99 latency @ 500 RPS (MockProvider) | TODO | TODO (`loadtest/`) | | |
| Rate-limit overshoot, 2 and 4 instances (shared Redis) | **0 requests (0%)**, worst of 5 runs each; naive per-instance counters: +100% (2) / +300% (4) | `uv run python -m loadtest.overshoot --instances 2 4 --repeat 5` | gateway at 311f88b (script added in step 2.5) | Darwin arm64, 10 CPUs; Python 3.12.16; local Redis 8.10.3 + Postgres 17; MockProvider, 20 ms to first token |
| Daily-budget overshoot, 2 and 4 instances, normal text | **0 tokens (0%)**, worst of 5 runs each; naive: +98.13% (2) / +290.8% (4) | `uv run python -m loadtest.overshoot --instances 2 4 --repeat 5` | gateway at 311f88b (script added in step 2.5) | Darwin arm64, 10 CPUs; Python 3.12.16; local Redis 8.10.3 + Postgres 17; MockProvider, 20 ms to first token |
| Daily-budget overshoot, 2 and 4 instances, adversarial one-letter-word prompt | **+1,480 tokens on a 3,000 budget (+49.33%)**; naive: +198.67% (2) / +333.07% (4) | `uv run python -m loadtest.overshoot --instances 2 4 --repeat 5` | gateway at 311f88b (script added in step 2.5) | Darwin arm64, 10 CPUs; Python 3.12.16; local Redis 8.10.3 + Postgres 17; MockProvider, 20 ms to first token |
| Failover recovery time, primary down | TODO | TODO | | |
| Semantic cache hit rate / false-hit rate | TODO | TODO (`evals/`) | | |

## Overshoot (step 2.5)
Setup: one tenant per run, a burst of 240 requests, 50 at a time, spread round-robin across N
gateway processes in prod mode. Rate-limit runs: 60 requests per minute. Budget runs: 3,000
tokens a day, 50 output tokens per request. "Naive" gives each instance its own Redis key
prefix, i.e. its own counters, as in-memory limiting would (D45). Full JSON per run is printed
by the command above.

- **Rate limits hold exactly across instances.** One atomic Lua script on one Redis means 60
  admitted out of 240 with 2 or 4 instances. Separate counters admit 60 per instance.
- **Budgets hold for normal text.** Real usage stayed at or under 3,000 tokens; the gateway's
  counters matched the usage the clients saw exactly.
- **Budgets can be overshot with input the estimate undercounts.** The gateway estimates input
  at 4 characters per token; a prompt of 200 one-letter words is estimated at 100 tokens but
  counted as 200. Each request reserved 150 and used 224, so 20 concurrent requests used
  4,480 tokens. Overshoot is bounded by how many requests are in flight at once (settling
  corrects the counter, then later requests are refused), and it didn't grow from 2 to 4
  instances. Not fixed yet: see the follow-up in D42.

## Known measurement caveats (to verify before publishing numbers)
- **Overhead on cancelled streams:** in a manual run (step 1.4, mock with 50 ms per-token delay), one stream cut off by the client logged `overhead_ms` ≈ 53, about one token interval, while completed requests logged 1–4 ms. Cancelled streams may count part of a provider wait as gateway overhead. Not yet investigated; check this before reporting overhead percentiles in Phase 6, and exclude or fix cancelled streams.
