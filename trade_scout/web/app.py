"""The local web UI. Localhost only, single user, no auth by design.

Server-rendered Jinja2 pages with htmx partial swaps; no build step, no Node.
Everything the pages show comes from the same engine the CLI uses:
evaluate_trade for the per-source verdict table, run_consult (captured) for
trade ideas, build_rosters for the board, the provider registry for /sources.

Consults can take minutes (LLM + optional headless browser), so they run as
background jobs in a thread pool and the page polls. Trade evaluations are
seconds and run inline.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trade_scout.config import AppConfig, LeagueEntry, get_league_config

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

# One consult at a time is plenty for a single-user tool; a second submission
# queues instead of racing the first for the same providers.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_IDS = itertools.count(1)
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def _leagues() -> list[LeagueEntry]:
    return AppConfig.load().leagues


def _eval_text(short: str, text: str) -> dict:
    """Parse + evaluate one structured/NL trade for the web. Raises
    ValueError with a user-facing message on every refusal."""
    from trade_scout.engine.evaluate import evaluate_trade
    from trade_scout.sleeper import SleeperClient, SleeperError
    from trade_scout.trade import TradeParseError, parse_trade_command
    from trade_scout.values import ValuesError

    try:
        trade = parse_trade_command(text)
    except TradeParseError as e:
        raise ValueError(str(e)) from e
    if trade.league_explicit and trade.league and trade.league != short:
        raise ValueError(
            f"league conflict: this page is {short} but the command says "
            f"{trade.league}.")
    try:
        league_cfg = get_league_config(short)
    except RuntimeError as e:
        raise ValueError(str(e)) from e
    try:
        sleeper = SleeperClient(league_id=league_cfg["league_id"])
        league = sleeper.get_league_settings()
        players_map = sleeper.get_players_map()
    except SleeperError as e:
        raise ValueError(f"Sleeper API error: {e}") from e
    try:
        result = evaluate_trade(
            trade=trade, league=league, players_map=players_map,
            sleeper=sleeper, display_name=league_cfg["display_name"])
    except ValuesError as e:
        raise ValueError(str(e)) from e
    result["league_cfg"] = league_cfg
    return result


def create_app() -> FastAPI:
    app = FastAPI(title="Trade Scout", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("leagues", _leagues())
        return templates.TemplateResponse(request, name, ctx)

    # ---- home / setup ----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        leagues = _leagues()
        if not leagues:
            return RedirectResponse("/setup", status_code=303)
        cfg = AppConfig.load()
        default = cfg.default_league or leagues[0].short
        return RedirectResponse(f"/league/{default}", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        return page(request, "setup.html",
                    username=AppConfig.load().sleeper_username)

    @app.post("/setup/lookup", response_class=HTMLResponse)
    def setup_lookup(request: Request, username: str = Form(...)):
        from datetime import date

        from trade_scout import sleeper as sleeper_mod

        try:
            user = sleeper_mod.get_user(username.strip())
            season = str(date.today().year)
            found = sleeper_mod.get_user_leagues(user["user_id"], season)
        except Exception as e:  # noqa: BLE001 - one message either way
            return page(request, "partials/setup_leagues.html",
                        error=f"Sleeper lookup failed: {e}", found=[],
                        username=username, display_name="")
        return page(request, "partials/setup_leagues.html",
                    error=None if found else "No NFL leagues found this season.",
                    found=found, username=username.strip(),
                    display_name=user.get("display_name") or username.strip())

    @app.post("/setup/save", response_class=HTMLResponse)
    async def setup_save(request: Request):
        form = await request.form()
        username = str(form.get("username") or "").strip()
        display_name = str(form.get("display_name") or username)
        cfg = AppConfig.load()
        cfg.sleeper_username = username
        cfg.leagues = []
        used: set[str] = set()
        for league_id in form.getlist("league_id"):
            name = str(form.get(f"name_{league_id}") or "League")
            short = str(form.get(f"short_{league_id}") or "").strip().upper()
            if not short:
                short = "".join(w[0] for w in name.split()[:3]).upper() or "L1"
            base, n = short, 2
            while short in used:
                short, n = f"{base}{n}", n + 1
            used.add(short)
            cfg.leagues.append(LeagueEntry(
                short=short, league_id=str(league_id), name=name,
                owner_display_name=display_name))
        if not cfg.leagues:
            return page(request, "setup.html", username=username,
                        error="Pick at least one league.")
        cfg.default_league = cfg.leagues[0].short
        cfg.save()
        return RedirectResponse("/", status_code=303)

    # ---- league pages ----------------------------------------------------

    @app.get("/league/{short}", response_class=HTMLResponse)
    def league_home(request: Request, short: str):
        entry = AppConfig.load().league(short)
        if entry is None:
            return RedirectResponse("/setup", status_code=303)
        return page(request, "league.html", league=entry)

    @app.get("/league/{short}/board", response_class=HTMLResponse)
    def board(request: Request, short: str):
        from trade_scout.rosters import build_rosters
        from trade_scout.sleeper import SleeperError

        entry = AppConfig.load().league(short)
        if entry is None:
            return RedirectResponse("/setup", status_code=303)
        try:
            data = build_rosters(entry.league_id)
        except SleeperError as e:
            return page(request, "board.html", league=entry, board=None,
                        error=f"Sleeper unavailable: {e}")
        return page(request, "board.html", league=entry, board=data, error=None)

    @app.get("/league/{short}/trade", response_class=HTMLResponse)
    def trade_page(request: Request, short: str):
        entry = AppConfig.load().league(short)
        if entry is None:
            return RedirectResponse("/setup", status_code=303)
        return page(request, "trade.html", league=entry)

    @app.post("/league/{short}/trade", response_class=HTMLResponse)
    def trade_eval(request: Request, short: str,
                   give: str = Form(""), get: str = Form("")):
        entry = AppConfig.load().league(short)
        if entry is None:
            return RedirectResponse("/setup", status_code=303)
        give, get = give.strip(), get.strip()
        if not give or not get:
            return page(request, "partials/trade_result.html", league=entry,
                        error="Both sides are required.", result=None)
        text = f"give: {give} | get: {get}"
        try:
            result = _eval_text(short, text)
        except ValueError as e:
            return page(request, "partials/trade_result.html", league=entry,
                        error=str(e), result=None)
        return page(request, "partials/trade_result.html", league=entry,
                    error=None, result=result)

    # ---- consult jobs ----------------------------------------------------

    @app.get("/league/{short}/consult", response_class=HTMLResponse)
    def consult_page(request: Request, short: str):
        entry = AppConfig.load().league(short)
        if entry is None:
            return RedirectResponse("/setup", status_code=303)
        return page(request, "consult.html", league=entry)

    @app.post("/league/{short}/consult", response_class=HTMLResponse)
    def consult_start(request: Request, short: str, text: str = Form(...)):
        entry = AppConfig.load().league(short)
        if entry is None:
            return RedirectResponse("/setup", status_code=303)

        job_id = str(next(_JOB_IDS))
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "running", "messages": []}

        def work() -> None:
            from trade_scout import delivery
            from trade_scout.consult import run_consult

            try:
                with delivery.capture() as messages:
                    run_consult("web", text, explicit=short)
                outcome = {"status": "done", "messages": messages}
            except Exception as e:  # noqa: BLE001 - job wall, not a crash
                log.exception("Consult job failed")
                outcome = {"status": "done",
                           "messages": [f"Consult failed: {e}"]}
            with _JOBS_LOCK:
                _JOBS[job_id] = outcome

        _EXECUTOR.submit(work)
        return page(request, "partials/consult_job.html", job_id=job_id,
                    league=entry)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_status(request: Request, job_id: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            return page(request, "partials/consult_result.html",
                        messages=["Unknown job."], done=True)
        if job["status"] != "done":
            return page(request, "partials/consult_poll.html", job_id=job_id)
        return page(request, "partials/consult_result.html",
                    messages=job["messages"], done=True)

    # ---- sources ---------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        from trade_scout.providers import REGISTRY
        from trade_scout.values import get_snapshots

        cfg = AppConfig.load()
        # Probe with the default league's format when one exists, else SF.
        league_settings = {"num_qbs": 2, "num_teams": 12, "ppr": 1}
        entry = cfg.league(cfg.default_league) if cfg.default_league else None
        if entry is not None:
            try:
                from trade_scout.sleeper import SleeperClient
                league_settings = SleeperClient(
                    entry.league_id).get_league_settings()
            except Exception as e:  # noqa: BLE001 - probe with defaults instead
                log.warning("Could not read league settings for probe: %s", e)
        snapshots, errors = get_snapshots(league_settings, cfg)

        rows = []
        for source in REGISTRY:
            key = source.key
            enabled = source.enabled(cfg)
            snap = snapshots.get(key)
            rows.append({
                "key": key,
                "display_name": source.display_name,
                "enabled": enabled,
                "requires": source.requires,
                "ok": snap is not None,
                "error": errors.get(key),
                "as_of": snap.as_of if snap else None,
                "stale": snap.stale if snap else None,
                "players": len(snap.players) if snap else 0,
                "picks": len(snap.picks) if snap else 0,
            })
        from trade_scout.config import flock_email
        from trade_scout.llm import llm_available

        return page(request, "sources.html", rows=rows,
                    flock_configured=bool(flock_email()),
                    llm_on=llm_available())

    return app


def serve(host: str = "127.0.0.1", port: int = 8177) -> None:
    """`trade-scout serve`: run the web UI. Binds localhost only."""
    import uvicorn

    print(f"Trade Scout on http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
