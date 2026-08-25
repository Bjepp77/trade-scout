"""A dumb JSON disk cache, shared by the expensive providers.

Two things here are expensive in a way an in-process cache cannot help with,
because every request is a FRESH PROCESS: the Sleeper players map (14.6 MB, and
Sleeper asks that it be pulled at most once a day) and the Flock rankings (a
headless browser login, measured at 13 seconds).

Deliberately dumb. One file per name, an mtime check, and a full refetch when
stale. No invalidation protocol, no locking, no partial updates. Every failure
path returns "no cache" and refetches, because a cache that can break its
caller is worse than no cache.

TRADE_SCOUT_CACHE_DIR overrides the location; setting it empty disables caching
entirely, which is what the test suite wants. That is not configurability for
its own sake: without it the unit tests read whatever happens to be on the
developer's disk, which is how a green suite starts depending on machine state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


class CacheDisabled(Exception):
    """TRADE_SCOUT_CACHE_DIR was set to empty: run without a disk cache."""


def cache_path(name: str) -> Path:
    """Where `name` is cached. Raises CacheDisabled when caching is off."""
    base = os.environ.get("TRADE_SCOUT_CACHE_DIR")
    if base is not None:
        if not base.strip():
            raise CacheDisabled()
        return Path(base) / name
    # Default under the user's home, not the repo tree: a clone should stay
    # byte-identical to the checkout after any amount of running.
    return Path.home() / ".trade-scout" / "cache" / name


def read_json(name: str, max_age_seconds: float):
    """The cached value, or None if absent, stale, disabled, or unreadable."""
    try:
        f = cache_path(name)
        if not f.exists():
            return None
        if (time.time() - f.stat().st_mtime) > max_age_seconds:
            return None
        data = json.loads(f.read_text())
        return data if data else None
    except CacheDisabled:
        return None
    except (OSError, ValueError):
        # Corrupt or unreadable is the same answer as absent: refetch.
        return None


def write_json(name: str, data) -> None:
    """Write via a temp file and rename.

    A crash between opening and finishing a direct write leaves a truncated
    JSON blob that every later run has to fail on and nothing ever repairs.
    The rename is atomic, so a reader sees either the old file or the new one.
    """
    try:
        f = cache_path(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer. Every request is a fresh process, so a bot
        # request and a satellite refresh can write the same name at the same
        # moment. A FIXED .tmp path lets them interleave and truncate each
        # other, and then both rename: the rename is atomic, the contents are
        # not, so a reader can get a cleanly-renamed corrupt file.
        fd, tmp_name = tempfile.mkstemp(dir=str(f.parent), prefix=f.name + ".", suffix=".tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.write_text(json.dumps(data))
        tmp.replace(f)
    except CacheDisabled:
        return
    except OSError as e:
        log.warning("Could not write the %s cache (non-fatal): %s", name, e)
