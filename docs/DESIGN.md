# Trade Scout — design

## The one invariant (non-negotiable)

**Trade Scout never sends a confident verdict built on stale, missing, or
wrong-format data.** Every verdict states its basis — the sources, their
as-of dates, and the league format priced under. If a source cannot be
fetched it is excluded *and reported excluded*; if a player cannot be
matched he is named, never silently priced at zero; if no league is
specified the engine refuses rather than guessing (a default equal to a
legal value makes "the owner chose it" indistinguishable from "nobody chose
anything").

Corollary for the AI extras: prose composes strictly from the numbers the
code computed. Nothing an LLM writes is ever a value.

## Architecture

```
config.py           file-backed AppConfig (~/.trade-scout/config.json),
                    env overrides, league shortname registry
sleeper.py          read-only Sleeper client + user/league discovery
providers/
  base.py           the contract: LeagueFormat, SourceSnapshot, ValueSource
  fantasycalc.py    open API, sleeper_id join, reference scale
  dynastyprocess.py open weekly CSVs, joined via their id crosswalk
  keeptradecut.py   rankings-page scrape, name join
  dynastydaddy.py   unofficial frontend API, sleeper_id join
  flock.py          OPINION source (rank/tier + their calculator), never
                    blended, never votes; BYO credentials, Playwright
values.py           get_snapshots(): all enabled sources, concurrent,
                    per-source failure excluded not fatal
engine/
  resolve.py        one central name->sleeper_id resolution per snapshot
  evaluate.py       per-source pricing on RAW scales + normalised composite
                    + consensus gate (all/majority/any, even-band, stale)
model.py            quantile normalisation onto the reference curve,
                    N-source blend, roster scoring (VORP, fit)
picks.py            canonical pick keys, per-source pick sheets, calibration
trade.py            structured grammar + optional NL parse, asset resolution
consult.py          candidates (LLM or deterministic enumerator) ->
                    re-priced through evaluate_trade -> filtered/ranked
llm.py              one memoised probe; every AI call site degrades
web/                FastAPI + Jinja2 + vendored htmx, localhost only
cli.py              serve | eval | rosters | setup | run (legacy env mode)
```

## Scale discipline

Sources publish on wildly different scales (measured: at the median, KTC's
number is ~2.3x FantasyCalc's for the same player). Three rules keep that
from corrupting verdicts:

1. A **per-source row** mixes values from one source only, raw.
2. The **composite** first quantile-maps every source onto the reference
   curve (the first available source in registry priority order) by rank,
   then blends with configured weights. Ordering survives; arbitrary scale
   shape does not.
3. **Flock** publishes rank and tier, not values, so it is carried as a
   conviction signal beside the verdict and deliberately kept out of both
   the blend and the consensus vote — a second opinion must not be averaged
   into the first.

## The consult loop

Whatever proposes a trade skeleton — the LLM or the enumerator — proposes
structure only. Every candidate goes back through `evaluate_trade`; deltas
are code-authored per source; candidates with unpriceable assets or wild
composite losses are dropped; BEST/FAIR/STRETCH labels are assigned from the
measured deltas. This is load-bearing: an earlier design let the LLM write
`value_delta` and nothing ever checked it.

## Testing

The suite is hermetic: an autouse guard patches every provider fetch and the
raw httpx verbs to raise, the user's real `.env`, `~/.trade-scout` config,
and `claude` CLI are masked, and provider shaping is tested against small
checked-in samples. A test that wants the network or the LLM must patch its
way in explicitly.
