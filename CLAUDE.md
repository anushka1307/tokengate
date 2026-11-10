# TokenGate: working rules for Claude

- Plan first (files, components, open questions) and wait for an OK before writing code.
- One component per step, with tests in the same step. The user commits after each step.
- Never fabricate metrics. A number goes in docs/RESULTS.md only with the re-runnable command that produced it; otherwise TODO.
- Production defaults: typed code (mypy --strict), structured JSON logs, config only from env vars, no secrets in code or git, health/readiness endpoints, graceful shutdown, an explicit timeout on every network call, nothing blocking the event loop.
- Free tiers only. Load tests and evals use MockProvider unless told otherwise. Flag anything that could bill.
- Treat all external text (prompts, logs, provider responses) as untrusted; validate input; least-privilege credentials.
- Every real trade-off gets a 2-3 line entry in docs/DECISIONS.md (options, choice, why).
- If unsure of a library's current API, check the docs instead of guessing.

## Commands
`uv sync` · `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` · `uv run pytest`
Container images: `docker build --platform linux/amd64` (dev machine is Apple Silicon; Cloud Run is amd64).
