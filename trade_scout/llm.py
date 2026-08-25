"""Is an LLM available? The one place that decides.

The engine's math -- parsing the structured grammar, pricing, consensus,
consult enumeration -- needs no LLM. Three extras use one, via the `claude`
CLI when it is installed and authenticated:

- natural-language trade/consult parsing (fallback behind the structured
  grammar)
- consult skeleton generation (fallback: the deterministic enumerator)
- the prose verdict (fallback: the numeric summary; the source table IS the
  verdict either way)

Every one of those call sites asks llm_available() first and degrades when
it says no. The answer is memoised for the process: the probe shells out to
`claude` once, which is too slow to repeat per call.

Order of authority: TRADE_SCOUT_LLM env var ("off" disables, anything else
auto), then config llm.mode ("off" | "auto"), then the probe.
"""

from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger(__name__)

_CACHE: dict[str, bool] = {}


def llm_available() -> bool:
    """True when the `claude` CLI is installed and authenticated."""
    if "ok" in _CACHE:
        return _CACHE["ok"]

    mode = (os.environ.get("TRADE_SCOUT_LLM") or "").strip().lower()
    if not mode:
        from trade_scout.config import AppConfig

        mode = str(AppConfig.load().llm.get("mode", "auto")).lower()
    if mode == "off":
        _CACHE["ok"] = False
        return False

    if shutil.which("claude") is None:
        log.info("`claude` CLI not on PATH; AI extras off")
        _CACHE["ok"] = False
        return False

    from trade_scout.report import ClaudeAuthError, check_claude_auth

    try:
        check_claude_auth()
        _CACHE["ok"] = True
    except ClaudeAuthError as e:
        log.info("`claude` CLI present but not usable (%s); AI extras off", e)
        _CACHE["ok"] = False
    return _CACHE["ok"]


def reset() -> None:
    """Forget the probe result. For tests and long-lived servers."""
    _CACHE.clear()
