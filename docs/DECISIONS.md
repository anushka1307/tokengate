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
