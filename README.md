# Trade Scout

A local dynasty fantasy football trade evaluator for [Sleeper](https://sleeper.com)
leagues. Every trade is priced independently against **multiple value
sources**, and the verdict shows where they agree and where they don't —
because one calculator's opinion is a data point, and four disagreeing is
the actual answer.

```
FantasyCalc     give   10,071 | get   13,226 |   +3,155 (23.9%) WIN   as of 2026-08-24
DynastyProcess  give    7,995 | get    9,445 |   +1,450 (15.4%) WIN   as of 2026-08-21
KeepTradeCut    give    9,999 | get   15,535 |   +5,536 (35.6%) WIN   as of 2026-08-24
Dynasty Daddy   give    9,998 | get   15,481 |   +5,483 (35.4%) WIN   as of 2026-08-24
Consensus: wins under 4 of 4 sources (gate: majority -> PASS)
```

Runs entirely on your machine. No accounts, no server, no database — your
league config lives in `~/.trade-scout/config.json` and values are fetched
straight from the sources.

## Value sources

| Source | Basis | Access |
|---|---|---|
| [FantasyCalc](https://fantasycalc.com) | real completed dynasty trades | open API |
| [DynastyProcess](https://dynastyprocess.com) | FantasyPros expert consensus | open weekly CSVs |
| [KeepTradeCut](https://keeptradecut.com) | crowdsourced rankings | page scrape |
| [Dynasty Daddy](https://dynasty-daddy.com) | their in-house model | unofficial API |

Each source prices the whole trade **on its own scale** — raw numbers are
never mixed across sources. A normalised, weighted composite exists for
sorting and summary, and a configurable consensus gate (`all` / `majority` /
`any`, with an even-band so 0.2% edges don't cast votes) turns the rows into
one verdict. Draft picks are priced from each source's own pick sheet.
A source that's down or stale is excluded and *said to be excluded* — never
silently zeroed.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/Bjepp77/trade-scout
cd trade-scout
uv sync
uv run trade-scout setup     # your Sleeper username -> pick your leagues
uv run trade-scout serve     # open http://127.0.0.1:8177
```

Or from the terminal:

```sh
uv run trade-scout eval DD "give: Bijan Robinson | get: Jahmyr Gibbs, 2027 1st"
uv run trade-scout rosters   # full priced roster board as JSON
```

`DD` is whatever shortname you gave your league during setup.

## The web UI

- **Trade evaluator** — punch in both sides, get the per-source verdict
  table, consensus banner, and per-asset breakdown.
- **Trade ideas** — "Mike wants Caleb Williams": candidate packages are
  generated from the partner's roster, then **every candidate is re-priced
  by the engine against all sources**; only priceable, non-losing options
  come back, labelled BEST / FAIR / STRETCH by their *measured* deltas.
- **Roster board** — every roster priced and scored (market blend × roster
  fit, with per-source values kept visible).
- **Sources** — which calculators are live, how fresh, and what failed.

## Optional extras

**AI assist.** If the [Claude Code CLI](https://claude.com/claude-code)
(`claude`) is installed and authenticated, you also get natural-language
input ("my CMC and a 2027 1st for Bijan"), AI-drafted trade-idea structures,
and a prose verdict. Without it everything above still works — the
structured grammar, a deterministic idea enumerator, and the numeric
summary stand in. The engine's numbers are never LLM-authored either way.

**Flock Fantasy.** If you have a Flock account, its expert rankings and
trade calculator can be attached as an independent second opinion (shown
beside the verdict, never blended into it). Uses your own credentials via a
headless browser: put `FLOCK_EMAIL` / `FLOCK_PASSWORD` in `.env` (see
`.env.example`) and run `uv sync --extra flock && uv run playwright install
chromium`.

## Principles

1. **Never a confident verdict on stale, missing, or wrong-format data.**
   Every row states its source, its as-of date, and the league format it
   priced under. Unmatched players are named, not zeroed.
2. **Disagreement is the signal.** Sources are never collapsed into one
   number that agrees with itself; the composite is a convenience, the rows
   are the truth.
3. **The engine owns the numbers.** Anything an LLM proposes is re-priced
   by code before you see it.

## Development

```sh
uv sync --extra dev
uv run pytest -q       # hermetic: the suite never touches the network
uv run ruff check
uv run pyright
```

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture.

## Disclaimer

Not affiliated with Sleeper, FantasyCalc, DynastyProcess, KeepTradeCut,
Dynasty Daddy, or Flock. Values are fetched politely (cached, low
frequency) from public endpoints; unofficial sources can change or
disappear, in which case the tool degrades and says so. For personal use.

MIT licensed.
