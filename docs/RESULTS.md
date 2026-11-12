# Measured results

Every number here must name the script and command that produced it, the git commit, and the
environment. Nothing is estimated. Unmeasured rows stay TODO.

| Metric | Value | Command | Commit | Environment |
|---|---|---|---|---|
| Gateway-added p50 / p99 latency @ 500 RPS (MockProvider) | TODO | TODO (`loadtest/`) | | |
| Rate-limit overshoot, 2+ instances | TODO | TODO | | |
| Failover recovery time, primary down | TODO | TODO | | |
| Semantic cache hit rate / false-hit rate | TODO | TODO (`evals/`) | | |

## Known measurement caveats (to verify before publishing numbers)
- **Overhead on cancelled streams:** in a manual run (step 1.4, mock with 50 ms per-token delay), one stream cut off by the client logged `overhead_ms` ≈ 53, about one token interval, while completed requests logged 1–4 ms. Cancelled streams may count part of a provider wait as gateway overhead. Not yet investigated; check this before reporting overhead percentiles in Phase 6, and exclude or fix cancelled streams.
