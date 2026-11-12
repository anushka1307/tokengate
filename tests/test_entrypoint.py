import pytest

from app.__main__ import uvicorn_options
from app.config import Settings


def test_defaults() -> None:
    options = uvicorn_options({}, Settings(env="test"))
    assert options["port"] == 8000
    assert options["host"] == "0.0.0.0"  # noqa: S104 - container default
    assert options["factory"] is True
    assert options["access_log"] is False
    assert options["proxy_headers"] is False
    assert options["server_header"] is False
    assert options["timeout_graceful_shutdown"] == 8.0


def test_reads_port_from_cloud_run() -> None:
    assert uvicorn_options({"PORT": "8080"}, Settings(env="test"))["port"] == 8080


def test_graceful_shutdown_comes_from_settings() -> None:
    settings = Settings(env="test", graceful_shutdown_timeout_s=5)
    assert uvicorn_options({}, settings)["timeout_graceful_shutdown"] == 5


# "\uff18\uff10" is full-width "80": both str.isdigit() and int() accept it, so only an
# explicit ASCII check rejects it (same bug class as the "²" Content-Length fix in step 1.3).
@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1", "", "\uff18\uff10", "80 "])
def test_bad_port_fails_fast(port: str) -> None:
    with pytest.raises(ValueError, match="PORT"):
        uvicorn_options({"PORT": port}, Settings(env="test"))
