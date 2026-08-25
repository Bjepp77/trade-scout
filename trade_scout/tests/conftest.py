"""Test-wide guards.

The unit suite must never touch the network. That is not a style preference:
it has already gone wrong twice here. The roster tests reached
keeptradecut.com on every run and took the suite from 0.2s to 10s, and the
Sleeper players-map cache made a test read whatever 15 MB file happened to be
on the developer's disk.

Flock is the next one waiting to happen. It fails fast today only because
FLOCK_EMAIL is unset; the moment real credentials land in the environment, any
test that reaches compute_trade_math would drive a headless browser through a
login. This makes that a loud failure rather than a slow surprise.

A test that genuinely wants one of these applies its own patch, which wins.
"""
from __future__ import annotations

import pytest


def _forbid(name: str):
    def boom(*_a, **_k):
        raise AssertionError(
            f"{name} was called in a test. The unit suite does not touch the "
            f"network. Patch it: patch('{name}', return_value=...)"
        )
    return boom


@pytest.fixture(autouse=True, scope="session")
def _no_env_file():
    """Keep the developer's real .env out of the test environment.

    config.py loads it at import, so without this a machine with credentials
    and a machine without one run different suites, and "is this unset?" stops
    being a question about the code.
    """
    import os
    os.environ["TRADE_SCOUT_SKIP_ENV_FILE"] = "1"
    for k in ("FLOCK_EMAIL", "FLOCK_PASSWORD"):
        os.environ.pop(k, None)


@pytest.fixture(autouse=True)
def _no_user_config(monkeypatch, tmp_path):
    """Keep the developer's real ~/.trade-scout/config.json out of tests.

    Same reasoning as _no_env_file: a machine with a configured league and a
    machine without one must run the same suite. Tests that want a config
    write one to this path.
    """
    monkeypatch.setenv("TRADE_SCOUT_CONFIG", str(tmp_path / "config.json"))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No process memo and no disk cache leaking between tests.

    The providers memoise within a process and cache to disk across them, both
    of which are right in production, where every request is a short-lived
    spawn. In a test session they are poison: one test populating the memo made
    a later "does this raise on an HTTP error" test pass without ever reaching
    the HTTP call, so the outcome depended on test ORDER.
    """
    monkeypatch.setenv("TRADE_SCOUT_CACHE_DIR", "")
    monkeypatch.setenv("TRADE_SCOUT_HISTORY_DIR", "")
    from trade_scout.providers import dynastyprocess, fantasycalc, keeptradecut
    for mod in (fantasycalc, keeptradecut, dynastyprocess):
        memo = getattr(mod, "_MEMO", None)
        if isinstance(memo, dict):
            memo.clear()
    raw = getattr(fantasycalc, "_LAST_RAW", None)
    if isinstance(raw, dict):
        raw.clear()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block every provider that leaves the machine."""
    for target in (
        "trade_scout.providers.flock.get_flock_rankings",
        "trade_scout.providers.flock.run_flock_trade_calc",
        "trade_scout.providers.keeptradecut.get_ktc_values",
        "trade_scout.providers.fantasycalc.fantasycalc_provider",
        "trade_scout.providers.dynastyprocess.get_dynastyprocess_snapshot",
    ):
        module, _, attr = target.rpartition(".")
        try:
            monkeypatch.setattr(target, _forbid(target), raising=True)
        except AttributeError:
            # A provider that does not exist yet is not a reason to fail every
            # test in the suite.
            continue

    # And the raw HTTP client, so a new provider cannot slip past the list.
    import httpx
    monkeypatch.setattr(httpx, "get", _forbid("httpx.get"), raising=True)
    monkeypatch.setattr(httpx, "post", _forbid("httpx.post"), raising=True)
