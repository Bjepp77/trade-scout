"""Trade Scout configuration.

Two layers, two jobs:

- Secrets (Flock credentials) come from the ENVIRONMENT, optionally seeded
  from a repo-local `.env`. Never from the config file, so the config file is
  always safe to look at, copy, or back up.
- Everything else -- leagues, sources, consensus policy -- lives in a JSON
  config file at ~/.trade-scout/config.json (override with TRADE_SCOUT_CONFIG),
  created by `trade-scout setup` or the web /setup flow.

Environment variables still win over the file where both name the same thing
(league ids via SLEEPER_LEAGUE_<SHORT>), so a wrapping process can pin the
engine to a league without touching the user's config.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _load_env_file() -> None:
    """Load the repo-local .env into the environment, once, at import.

    A REAL environment variable always wins. In CI the secrets are already in
    the environment and this file does not exist; locally it is the only
    source. Never the other way around, or a stale local file would silently
    override a rotated CI secret.

    Deliberately not python-dotenv: twenty lines against a dependency, for a
    file with two keys in it.
    """
    if os.environ.get("TRADE_SCOUT_SKIP_ENV_FILE"):
        return
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        raw = path.read_text()
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue  # a real environment variable wins
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ[key] = value


_load_env_file()


def get_env(key: str, default: str | None = None) -> str:
    """Read an env var. Raise if missing, EMPTY, or whitespace.

    Empty counts as missing on purpose. os.environ.get treats a present-but-
    blank variable as set, and SleeperClient then does
    `league_id or ...`, where the empty string is falsy: a request for one
    league silently priced against another while the reply named the first.
    Observed, not theorised.
    """
    val = os.environ.get(key, default)
    if val is None or not str(val).strip():
        raise RuntimeError(f"Missing or empty environment variable: {key}")
    return val.strip()


def explicit_league() -> str | None:
    """The league the CALLER selected, out of band.

    A wrapping console used to send its league choice alongside the command
    text, the middle layer dropped it, and the engine fell back to parsing a
    league token out of the text -- which the board never wrote -- so every
    trade built on one board was priced against the other league.

    Out of band on purpose: smuggling a league token into the command text
    would be the same in-band signalling that made text detection unfixable.
    """
    raw = (os.environ.get("TRADE_SCOUT_LEAGUE") or "").strip().upper()
    return raw or None


# ---------------------------------------------------------------------------
# File-backed application config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_DIR = Path.home() / ".trade-scout"

# ValueSources vote in the consensus; OpinionSources are shown alongside it.
DEFAULT_SOURCES: dict[str, dict] = {
    "fantasycalc": {"enabled": True, "weight": 1.0},
    "dynastyprocess": {"enabled": True, "weight": 1.0},
    "ktc": {"enabled": True, "weight": 1.0},
    "dynastydaddy": {"enabled": False, "weight": 1.0},
    "flock": {"enabled": "auto"},
}


@dataclass
class LeagueEntry:
    short: str            # "DD" -- the shortname used in commands and URLs
    league_id: str        # Sleeper league id
    name: str             # human label
    owner_display_name: str = ""   # the user's Sleeper handle in this league


@dataclass
class AppConfig:
    version: int = 1
    sleeper_username: str = ""
    leagues: list[LeagueEntry] = field(default_factory=list)
    default_league: str = ""
    sources: dict[str, dict] = field(default_factory=lambda: {
        k: dict(v) for k, v in DEFAULT_SOURCES.items()})
    consensus: dict = field(default_factory=lambda: {
        "gate": "majority", "even_band_pct": 3.0, "exclude_stale": True})
    llm: dict = field(default_factory=lambda: {"mode": "auto"})

    # -- persistence --------------------------------------------------------

    @staticmethod
    def path() -> Path:
        override = os.environ.get("TRADE_SCOUT_CONFIG")
        if override:
            return Path(override)
        return DEFAULT_CONFIG_DIR / "config.json"

    @classmethod
    def load(cls) -> "AppConfig":
        """Read the config file. A missing or unreadable file is an empty
        config, not an error: first-run is a real state, and every consumer
        must already cope with 'no leagues configured'."""
        try:
            raw = json.loads(cls.path().read_text())
        except (OSError, ValueError):
            return cls()
        leagues = [
            LeagueEntry(
                short=str(e.get("short", "")).upper(),
                league_id=str(e.get("league_id", "")),
                name=str(e.get("name", "")),
                owner_display_name=str(e.get("owner_display_name", "")),
            )
            for e in raw.get("leagues", [])
            if e.get("short") and e.get("league_id")
        ]
        cfg = cls(
            version=int(raw.get("version", 1)),
            sleeper_username=str(raw.get("sleeper_username", "")),
            leagues=leagues,
            default_league=str(raw.get("default_league", "")).upper(),
        )
        # Merge saved source/consensus settings over the defaults, so a new
        # provider added in an upgrade appears with its default posture
        # instead of vanishing from an old config file.
        for key, saved in (raw.get("sources") or {}).items():
            if isinstance(saved, dict):
                cfg.sources.setdefault(key, {}).update(saved)
        cfg.consensus.update(raw.get("consensus") or {})
        cfg.llm.update(raw.get("llm") or {})
        return cfg

    def save(self) -> Path:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        tmp.replace(path)
        return path

    # -- lookups ------------------------------------------------------------

    def league(self, short: str) -> LeagueEntry | None:
        key = short.strip().upper()
        for entry in self.leagues:
            if entry.short == key:
                return entry
        return None


def _league_id_from_env(short: str) -> str | None:
    """Environment override for a league id.

    SLEEPER_LEAGUE_<SHORT> wins for any league. SLEEPER_LEAGUE_ID is honoured
    as a legacy alias for DD only, because that is the exact contract the old
    wrapper was built against.
    """
    for key in (f"SLEEPER_LEAGUE_{short}",
                *(("SLEEPER_LEAGUE_ID",) if short == "DD" else ())):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


def get_league_config(shortname: str | None = None) -> dict[str, str]:
    """Return {league_id, display_name, league_name, short} for a shortname.

    Raises RuntimeError when no league is named, when the name is unknown, or
    when no id can be resolved. There is deliberately NO default league: a
    default equal to a legal value makes "the owner chose it" indistinguishable
    from "nobody chose anything", and leagues score differently enough that the
    wrong guess prices the trade in the wrong currency.
    """
    if not shortname or not str(shortname).strip():
        raise RuntimeError(
            "No league specified. Name one of your configured leagues, for "
            "example: /trade DD give: ... | get: ...")
    key = str(shortname).strip().upper()

    cfg = AppConfig.load()
    entry = cfg.league(key)
    env_id = _league_id_from_env(key)

    league_id = env_id or (entry.league_id if entry else "")
    if not league_id:
        known = ", ".join(e.short for e in cfg.leagues) or "none configured"
        raise RuntimeError(
            f"Unknown league shortname: {key} (known: {known}). "
            "Run `trade-scout setup` to configure your leagues.")

    display_name = (
        (entry.owner_display_name if entry else "")
        or cfg.sleeper_username
        or (os.environ.get("SLEEPER_DISPLAY_NAME") or "").strip()
    )
    return {
        "league_id": league_id,
        "display_name": display_name,
        "league_name": (entry.name if entry and entry.name else key),
        "short": key,
    }


def known_league_shorts() -> list[str]:
    """League shortnames the parser may recognise as a leading token.

    Config file first, then SLEEPER_LEAGUE_<SHORT> env vars (the wrapper
    contract, with SLEEPER_LEAGUE_ID as the legacy alias for DD). Longest
    first so a longer code is never shadowed by its own prefix.
    """
    shorts = {e.short for e in AppConfig.load().leagues}
    for key, val in os.environ.items():
        if key.startswith("SLEEPER_LEAGUE_") and val.strip():
            suffix = key[len("SLEEPER_LEAGUE_"):]
            shorts.add("DD" if suffix == "ID" else suffix)
    return sorted(shorts, key=lambda s: (-len(s), s))


def league_prompt_hint() -> str:
    """Shortname-to-league-name mapping text for the NL parse prompts."""
    entries = AppConfig.load().leagues
    if entries:
        return "; ".join(f"'{e.short}' = {e.name}" for e in entries if e.short)
    shorts = known_league_shorts()
    return ", ".join(f"'{s}'" for s in shorts)


def flock_email() -> str | None:
    return os.environ.get("FLOCK_EMAIL")


def flock_password() -> str | None:
    return os.environ.get("FLOCK_PASSWORD")
