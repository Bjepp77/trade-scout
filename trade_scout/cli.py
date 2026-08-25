"""Command-line entrypoints.

  trade-scout eval <LEAGUE> <text...>   price one trade (or consult) and print
  trade-scout rosters                   dump the priced roster board as JSON
  trade-scout run                       legacy env-driven mode (TEXT/CHAT_ID,
                                        TRADE_SCOUT_LEAGUE), sentinel output --
                                        kept one release for the old satellite

`eval` and `run` share one pipeline; they differ only in where the command
text and league selection come from (argv vs environment).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger(__name__)

# Read by the wrapping satellite. Deliberately not part of the reply text.
PRICED_MARKER = "TRADE_SCOUT_PRICED_AS>>>"


def _send_error(chat_id: str, reason: str) -> None:
    """Best-effort error reply. Never raises."""
    from trade_scout.delivery import send_message

    try:
        send_message(chat_id, f"Couldn't run that trade: {reason}")
    except Exception:
        log.exception("Failed to deliver error message")


def process(chat_id: str, text: str, explicit: str | None) -> int:
    """Parse, price, and reply to one command. Returns a process exit code."""
    from trade_scout.config import get_league_config
    from trade_scout.consult import is_consult_command, run_consult
    from trade_scout.delivery import send_message
    from trade_scout.report import (
        ClaudeAuthError,
        ClaudeVerdictError,
        check_claude_auth,
        generate_verdict,
    )
    from trade_scout.sleeper import SleeperClient, SleeperError
    from trade_scout.trade import (
        TradeParseError,
        compute_trade_math,
        format_basis_line,
        parse_trade_command,
    )
    from trade_scout.values import ValuesError

    if not text.strip():
        _send_error(chat_id, "empty command")
        return 1

    try:
        if is_consult_command(text):
            run_consult(chat_id, text)
            return 0

        try:
            check_claude_auth()
        except ClaudeAuthError as e:
            _send_error(chat_id, str(e))
            return 1

        try:
            trade = parse_trade_command(text)
        except TradeParseError as e:
            _send_error(chat_id, str(e))
            return 1

        # The caller's explicit choice wins over anything in the text, but a
        # disagreement is REFUSED rather than silently resolved: a selector
        # that quietly loses to text is the same bug facing the other way.
        try:
            if explicit and trade.league_explicit and explicit != trade.league:
                _send_error(chat_id, (
                    f"league conflict: you selected {explicit} but the command "
                    f"says {trade.league}. Say which one and send it again."))
                return 1
            resolved_league = explicit or trade.league
            league_cfg = get_league_config(resolved_league)
            # Structured echo of what was ACTUALLY resolved, emitted BEFORE
            # the reply sentinel so "everything after the sentinel is the
            # reply" still parses.
            print(f"{PRICED_MARKER}{league_cfg['short']}", flush=True)
        except RuntimeError as e:
            _send_error(chat_id, str(e))
            return 1

        try:
            sleeper = SleeperClient(league_id=league_cfg["league_id"])
            league = sleeper.get_league_settings()
            players_map = sleeper.get_players_map()
        except SleeperError as e:
            _send_error(chat_id, f"Sleeper API error: {e}")
            return 1

        try:
            math_result = compute_trade_math(
                trade=trade,
                league=league,
                players_map=players_map,
                sleeper=sleeper,
                display_name=league_cfg["display_name"],
            )
        except ValuesError as e:
            _send_error(chat_id, f"Values error: {e}")
            return 1

        basis = format_basis_line(
            source=math_result["source"],
            as_of=math_result["as_of"],
            format_str=math_result["format_str"],
            league_name=league_cfg["league_name"],
        )

        try:
            verdict_text = generate_verdict(math_result)
        except ClaudeVerdictError:
            # Invariant: numeric summary without narrative rather than nothing.
            verdict_text = (
                f"Give total: {math_result['give_total']} | "
                f"Get total: {math_result['get_total']} | "
                f"Delta: {math_result['delta']:+d} ({math_result['pct_gap']})"
            )
            log.warning("Claude verdict failed; sending numeric summary")

        notes: list[str] = []
        if math_result.get("unmatched"):
            notes.append("Couldn't find: " + ", ".join(math_result["unmatched"]))
        if not math_result.get("flock_available"):
            notes.append("Flock rankings unavailable, using market values only.")
        notes_line = "\n" + "\n".join(notes) if notes else ""

        send_message(chat_id, f"{verdict_text}\n\n{basis}{notes_line}")
        return 0

    except Exception:
        log.exception("Unexpected error in Trade Scout")
        _send_error(chat_id, "unexpected internal error")
        return 1


def _cmd_run() -> int:
    """Legacy env-driven mode: TEXT/INPUT_TEXT, CHAT_ID/INPUT_CHAT_ID."""
    from trade_scout.config import explicit_league

    chat_id = os.environ.get("INPUT_CHAT_ID") or os.environ.get("CHAT_ID", "")
    text = os.environ.get("INPUT_TEXT") or os.environ.get("TEXT", "")
    return process(chat_id, text, explicit_league())


def _cmd_setup() -> int:
    """Interactive first-run setup: Sleeper username -> pick leagues -> save."""
    from datetime import date

    from trade_scout import sleeper
    from trade_scout.config import AppConfig, LeagueEntry

    username = input("Sleeper username: ").strip()
    if not username:
        print("No username given, nothing saved.", file=sys.stderr)
        return 1
    try:
        user = sleeper.get_user(username)
        season = str(date.today().year)
        leagues = sleeper.get_user_leagues(user["user_id"], season)
    except Exception as e:  # noqa: BLE001 - any API failure reads the same here
        print(f"Sleeper lookup failed: {e}", file=sys.stderr)
        return 1
    if not leagues:
        print(f"No {season} NFL leagues found for {username}.", file=sys.stderr)
        return 1

    print(f"\nLeagues for {username} ({season}):")
    for i, lg in enumerate(leagues, 1):
        print(f"  {i}. {lg.get('name', '?')}  ({lg.get('league_id')})")
    picked = input("\nNumbers to add (e.g. 1,3) or Enter for all: ").strip()
    if picked:
        try:
            idx = sorted({int(p) for p in picked.replace(" ", "").split(",")})
            chosen = [leagues[i - 1] for i in idx if 1 <= i <= len(leagues)]
        except ValueError:
            print("Couldn't read that selection, nothing saved.", file=sys.stderr)
            return 1
    else:
        chosen = leagues
    if not chosen:
        print("Nothing selected, nothing saved.", file=sys.stderr)
        return 1

    cfg = AppConfig.load()
    cfg.sleeper_username = username
    cfg.leagues = []
    used: set[str] = set()
    for lg in chosen:
        name = lg.get("name") or "League"
        default_short = "".join(w[0] for w in name.split()[:3]).upper() or "L1"
        short = default_short
        n = 2
        while short in used:
            short = f"{default_short}{n}"
            n += 1
        answer = input(f"Shortname for '{name}' [{short}]: ").strip().upper()
        short = answer or short
        used.add(short)
        cfg.leagues.append(LeagueEntry(
            short=short, league_id=str(lg.get("league_id")), name=name,
            owner_display_name=user.get("display_name") or username))
    cfg.default_league = cfg.leagues[0].short
    path = cfg.save()
    shorts = ", ".join(e.short for e in cfg.leagues)
    print(f"\nSaved {len(cfg.leagues)} league(s) ({shorts}) to {path}")
    print(f"Try: trade-scout eval {cfg.default_league} "
          "\"give: Player A | get: Player B\"")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    text = " ".join(args.text)
    return process("cli", text, (args.league or "").strip().upper() or None)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(name)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(prog="trade-scout")
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("eval", help="price one trade or consult")
    p_eval.add_argument("league", help="league shortname (e.g. DD)")
    p_eval.add_argument("text", nargs="+", help="the trade command text")

    sub.add_parser("rosters", help="dump the priced roster board as JSON")
    sub.add_parser("setup", help="interactive first-run league setup")
    sub.add_parser("run", help="legacy env-driven mode (TEXT/CHAT_ID)")

    args = parser.parse_args(argv)

    if args.command == "rosters":
        from trade_scout.rosters import run_rosters

        run_rosters()
        return
    if args.command == "setup":
        sys.exit(_cmd_setup())
    if args.command == "run":
        sys.exit(_cmd_run())
    sys.exit(_cmd_eval(args))
