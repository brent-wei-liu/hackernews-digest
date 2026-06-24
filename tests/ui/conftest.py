"""Shared fixtures for the Playwright UI test suite.

Assumes `python3 api.py` is already running. Tests do NOT spawn the
server themselves (port-binding races + zombie-process pain). If the
server isn't reachable, every test is skipped with a clear reason.
"""
import os
import urllib.error
import urllib.request

import pytest


def _server_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/stats", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("HACKERNEWS_URL", "http://127.0.0.1:8084")


@pytest.fixture(scope="session", autouse=True)
def _require_server(base_url: str):
    if not _server_reachable(base_url):
        pytest.skip(
            f"hackernews-digest API not reachable at {base_url} — "
            "run `python3 api.py` in another terminal first.",
            allow_module_level=True,
        )
