"""Placeholder test so CI exercises the full toolchain; real tests arrive with step 1.2."""

import app


def test_version_is_semver() -> None:
    parts = app.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
