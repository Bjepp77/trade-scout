"""Reply delivery. Stdout is the only channel.

The Telegram path is gone. Two consumers remain:

- A human at a terminal, who wants the reply text and nothing else.
- A wrapping process (the legacy satellite) that sets TRADE_SCOUT_LOCAL=1 and
  parses everything after the sentinel line as the reply. The sentinel is
  emitted only for that caller so direct CLI output stays clean.

`chat_id` is retained in the signature so the consult call sites did not all
have to change in the same commit that removed Telegram. It is unused and will
be dropped when consult is refactored.
"""

from __future__ import annotations

import os

SENTINEL = "TRADE_SCOUT_REPLY>>>"


def send_message(chat_id: str, text: str) -> dict:
    """Print the reply to stdout. Returns {"ok": True} for call-site compat."""
    if os.environ.get("TRADE_SCOUT_LOCAL") == "1":
        print(SENTINEL, flush=True)
    print(text, flush=True)
    return {"ok": True}
