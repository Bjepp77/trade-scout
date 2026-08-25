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
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

SENTINEL = "TRADE_SCOUT_REPLY>>>"

# A capture sink, when a wrapping caller (the web app) wants the reply as
# data instead of stdout. ContextVar so concurrent web jobs in different
# threads never see each other's sink.
_SINK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "trade_scout_delivery_sink", default=None)


@contextmanager
def capture() -> Iterator[list[str]]:
    """Collect every reply sent inside the block instead of printing it."""
    messages: list[str] = []
    token = _SINK.set(messages.append)
    try:
        yield messages
    finally:
        _SINK.reset(token)


def send_message(chat_id: str, text: str) -> dict:
    """Deliver one reply: to the active capture sink, else stdout.

    Returns {"ok": True} for call-site compat.
    """
    sink = _SINK.get()
    if sink is not None:
        sink(text)
        return {"ok": True}
    if os.environ.get("TRADE_SCOUT_LOCAL") == "1":
        print(SENTINEL, flush=True)
    print(text, flush=True)
    return {"ok": True}
