"""Flock Fantasy provider via Playwright headless login.

Extracts expert consensus rankings (8 named rankers) and trade calculator
results from Flock Fantasy's React SPA. Auth is AWS Cognito. Data lives in
React component state, accessed via __reactFiber traversal on child DOM
elements.

Flock provides ranks, tiers, and trade calculator verdicts. This data
enriches the trade verdict alongside FantasyCalc values. Non-fatal: if
Flock fails, the trade pipeline falls back to FantasyCalc-only.

ToS note: Flock Fantasy's terms were not verified; automated login may
violate them and risk account suspension. This provider exists at the
owner's explicit direction and accepted risk.
"""

from __future__ import annotations

import asyncio
import logging

from trade_scout import cache
from trade_scout.config import flock_email, flock_password

log = logging.getLogger(__name__)

TIER_MAP: dict[int, str] = {
    1: "S", 2: "A", 3: "B", 4: "C", 5: "D", 6: "E", 7: "F",
    8: "G", 9: "H", 10: "I", 11: "J", 12: "K", 13: "L", 14: "M",
    15: "N", 16: "O", 17: "P", 18: "Q", 19: "R",
}


class FlockError(Exception):
    """Raised when Flock data extraction fails."""


# JavaScript to extract ranking rows from React fiber tree.
# Searches root and all child elements for a fiber entry point,
# then walks the tree looking for props with `rows` array.
_EXTRACT_JS = """
() => {
    function findFiber(node) {
        for (const key of Object.keys(node)) {
            if (key.startsWith('__reactFiber$')
                || key.startsWith('__reactInternalInstance$'))
                return node[key];
        }
        return null;
    }

    function walkFiber(fiber, depth) {
        if (!fiber || depth > 60) return null;
        const p = fiber.memoizedProps;
        if (p && p.rows && Array.isArray(p.rows) && p.rows.length > 10) {
            return p.rows;
        }
        return walkFiber(fiber.child, depth + 1)
            || walkFiber(fiber.sibling, depth + 1);
    }

    // Try root element first
    const root = document.getElementById('root')
        || document.getElementById('__next');
    if (!root) return null;

    let fiber = findFiber(root);
    if (fiber) {
        const result = walkFiber(fiber, 0);
        if (result) return result;
    }

    // Fallback: search child elements for a fiber entry point
    for (const el of root.querySelectorAll('*')) {
        fiber = findFiber(el);
        if (fiber) {
            const result = walkFiber(fiber, 0);
            if (result) return result;
        }
    }
    return null;
}
"""


async def _scrape_flock(format: str = "superflex") -> list[dict]:
    """Launch headless Chromium, log in, extract ranking rows."""
    try:
        from playwright.async_api import async_playwright  # pyright: ignore[reportMissingImports]
    except ImportError:
        raise FlockError("playwright not installed (pip install 'trade-scout[flock]')")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await _login_flock(page)

            # Navigate to rankings
            rankings_url = f"https://flockfantasy.com/rankings?format={format}"
            await page.goto(rankings_url, wait_until="networkidle")

            # Poll for React data (rankings load async after hydration)
            rows = None
            for attempt in range(6):
                await page.wait_for_timeout(3000)
                rows = await page.evaluate(_EXTRACT_JS)
                if rows and isinstance(rows, list) and len(rows) > 0:
                    break
                log.debug(
                    "Flock extraction attempt %d: no data yet", attempt + 1,
                )
                rows = None

            if not rows:
                raise FlockError(
                    "Could not extract ranking rows from React state"
                )

            return rows
        finally:
            await browser.close()


def _parse_rows(rows: list[dict]) -> dict[str, dict]:
    """Parse raw React rows into the keyed dict format."""
    result: dict[str, dict] = {}
    for row in rows:
        name = row.get("playerName")
        if not name:
            continue
        tier_int = row.get("averageTier")
        tier_str = TIER_MAP.get(tier_int, "?") if isinstance(tier_int, int) else "?"
        result[name.lower()] = {
            "flock_rank": row.get("overallAverageRank"),
            "flock_tier": tier_str,
            "flock_pos_rank": row.get("averagePositionalRank"),
            "rank_delta": row.get("rankDelta", 0),
            "position": (row.get("position") or "").upper(),
        }
    return result


async def _async_get_flock_rankings(format: str = "superflex") -> dict[str, dict]:
    """Async entrypoint: scrape + parse."""
    rows = await _scrape_flock(format)
    log.info("Flock: extracted %d ranking rows", len(rows))
    return _parse_rows(rows)


# Expert consensus moves daily at most, and getting it costs a headless browser
# login measured at 13 seconds. Scraping that per trade turned a 25s verdict
# into 40s and logged into Flock every single time.
RANKINGS_CACHE_MAX_AGE = 6 * 3600
RANKINGS_CACHE_NAME = "flock-rankings-{}.json"


def get_flock_rankings(format: str = "superflex") -> dict[str, dict]:
    """Synchronous wrapper for the async scraper.

    Returns dict keyed by lowercased player name:
        {
            "josh allen": {
                "flock_rank": 2.13,
                "flock_tier": "S",
                "flock_pos_rank": 1,
                "rank_delta": 0,
                "position": "QB",
            },
            ...
        }

    Raises FlockError on login failure, timeout, or missing data.
    """
    if not flock_email() or not flock_password():
        raise FlockError(
            "FLOCK_EMAIL and FLOCK_PASSWORD are not set. Put them in "
            "Projects/trade-scout/.env or the environment.")

    cache_name = RANKINGS_CACHE_NAME.format(format)
    cached = cache.read_json(cache_name, RANKINGS_CACHE_MAX_AGE)
    if isinstance(cached, dict) and cached:
        log.info("Flock rankings from disk cache: %d players", len(cached))
        return cached

    def _scrape():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run, _async_get_flock_rankings(format),
                ).result()
        return asyncio.run(_async_get_flock_rankings(format))

    data = _scrape()
    if data:
        cache.write_json(cache_name, data)
    return data


# --- Trade Calculator ---

# JavaScript to extract trade calculator values from React fiber tree.
# Walks fiber nodes looking for the component that holds userValue and
# opponentValue (the numeric scores for each side of the trade).
_CALC_EXTRACT_JS = """
() => {
    function findFiber(node) {
        for (const key of Object.keys(node)) {
            if (key.startsWith('__reactFiber$')
                || key.startsWith('__reactInternalInstance$'))
                return node[key];
        }
        return null;
    }

    function walkFiber(fiber, depth, result) {
        if (!fiber || depth > 100) return;
        const p = fiber.memoizedProps;
        if (p && typeof p === 'object') {
            if (p.userValue !== undefined && p.opponentValue !== undefined) {
                result.userValue = p.userValue;
                result.opponentValue = p.opponentValue;
                if (p.hasPlayers !== undefined) result.hasPlayers = p.hasPlayers;
            }
        }
        walkFiber(fiber.child, depth + 1, result);
        walkFiber(fiber.sibling, depth + 1, result);
    }

    const root = document.getElementById('root')
        || document.getElementById('__next');
    if (!root) return null;

    // Find fiber on first child (Flock's root doesn't have fiber directly)
    const firstChild = root.querySelector('*');
    if (!firstChild) return null;

    const fiber = findFiber(firstChild);
    if (!fiber) return null;

    const result = {};
    walkFiber(fiber, 0, result);

    // Also grab the verdict text and breakdown from DOM
    const allEls = document.querySelectorAll('div, span, p, section');
    for (const el of allEls) {
        const t = el.textContent?.trim();
        if (!t) continue;
        if ((t === 'You lose!' || t === 'You win!'
             || t === 'Fair trade!' || t === 'Even!')
            && t.length < 20) {
            result.verdict = t;
        }
    }

    // Get Value Breakdown and Roster Fit text (smallest matching elements)
    let bestBreakdown = null;
    let bestRoster = null;
    for (const el of allEls) {
        const t = el.textContent?.trim();
        if (!t) continue;
        if (t.startsWith('Value Breakdown') && t.includes('Expert rankings')) {
            if (!bestBreakdown || t.length < bestBreakdown.length) {
                bestBreakdown = t;
            }
        }
        if (t.startsWith('Roster Fit') && t.includes('ranked')) {
            if (!bestRoster || t.length < bestRoster.length) {
                bestRoster = t;
            }
        }
    }
    if (bestBreakdown) result.valueBreakdown = bestBreakdown;
    if (bestRoster) result.rosterFit = bestRoster;

    return result;
}
"""


async def _login_flock(page) -> None:
    """Log in to Flock Fantasy. Shared by rankings and calculator scrapers."""
    email = flock_email()
    password = flock_password()
    if not email or not password:
        raise FlockError(
            "FLOCK_EMAIL and FLOCK_PASSWORD are not set. Put them in "
            "Projects/trade-scout/.env or the environment.")

    await page.goto("https://flockfantasy.com/login", wait_until="networkidle")
    await page.wait_for_selector(
        'input[placeholder="Email Address"]', timeout=15000,
    )
    await page.fill('input[placeholder="Email Address"]', email)
    await page.fill('input[placeholder="Password"]', password)
    await page.click('button:has-text("Log In")')

    try:
        await page.wait_for_url(
            lambda url: "/login" not in url, timeout=15000,
        )
    except Exception:
        raise FlockError("Login failed: did not redirect from login page")


async def _add_player_to_side(page, search_el, player_name: str) -> bool:
    """Type a player name into a search box and click the first result card.

    Args:
        page: Playwright page
        search_el: ElementHandle for the search input
        player_name: Player name to search for

    Returns True if the player was found and clicked, False otherwise.
    """
    await search_el.click()
    await search_el.fill("")
    await search_el.fill(player_name)
    await page.wait_for_timeout(1500)

    # The search filters player cards shown below the input.
    # After typing, only matching cards remain visible.
    # We click the first visible card. Cards use relative+cursor styling.
    try:
        # Use page-level JS to find and click the first visible player
        # card that is NOT a draft pick (PK). The card area refreshes
        # after each search.
        clicked = await page.evaluate("""
            () => {
                // Find all player card containers with images
                const cards = document.querySelectorAll(
                    'div[class*="cursor-pointer"]'
                );
                for (const card of cards) {
                    // Skip if not visible
                    const rect = card.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    // Skip draft picks (they have PK badge)
                    const text = card.textContent || '';
                    if (text.includes('PK') && text.includes("'")) continue;
                    // Must have an image (player headshot)
                    if (card.querySelector('img')) {
                        card.click();
                        return true;
                    }
                }
                // Fallback: click first card with image regardless
                for (const card of cards) {
                    const rect = card.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (card.querySelector('img')) {
                        card.click();
                        return true;
                    }
                }
                return false;
            }
        """)
        if clicked:
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    return False


async def _select_league(page, league: str) -> None:
    """Select the correct league in the calculator dropdown."""
    try:
        league_btn = await page.query_selector(
            'button:has-text("Dirty Doz"), button:has-text("Champions")'
        )
        if league_btn:
            btn_text = await league_btn.text_content()
            need_cl = league == "CL"
            is_cl = "champion" in (btn_text or "").lower()
            if need_cl != is_cl:
                await league_btn.click()
                await page.wait_for_timeout(500)
                target = "Champions" if need_cl else "Dirty"
                option = await page.query_selector(
                    f'[role="option"]:has-text("{target}"), '
                    f'li:has-text("{target}"), '
                    f'div[class*="cursor"]:has-text("{target}")'
                )
                if option:
                    await option.click()
                    await page.wait_for_timeout(1000)
    except Exception as e:
        log.warning("League selection failed (non-fatal): %s", e)


async def _run_single_calc(
    page,
    give_players: list[str],
    get_players: list[str],
) -> dict:
    """Run a single trade through the calculator on an already-loaded page.

    Assumes the page is at /trade-calculator with the correct league selected.
    Clears any existing players, adds the new ones, and extracts results.
    """
    # Clear existing trade by clicking trash icons
    for _ in range(10):
        trash = await page.query_selector(
            'button:has(svg[class*="trash"]), '
            'button[aria-label*="clear"], '
            'button[aria-label*="delete"], '
            'button[aria-label*="remove"]'
        )
        if not trash:
            # Try the X buttons next to added players
            close_btn = await page.query_selector(
                'div:has(> span:has-text("OVR")) button, '
                'svg[class*="close"]'
            )
            if not close_btn:
                break
            await close_btn.click()
            await page.wait_for_timeout(300)
        else:
            await trash.click()
            await page.wait_for_timeout(300)

    # Reload the page to get a clean state (most reliable clear method)
    await page.reload(wait_until="networkidle")
    await page.wait_for_timeout(2000)

    # Verify we have search boxes
    search_boxes = await page.query_selector_all(
        'input[placeholder="Search..."]'
    )
    if len(search_boxes) < 2:
        return {"verdict": "error", "error": "search boxes not found"}

    # Add players the owner is giving away (They Receive = left side)
    for name in give_players:
        boxes = await page.query_selector_all(
            'input[placeholder="Search..."]'
        )
        if not boxes:
            break
        added = await _add_player_to_side(page, boxes[0], name)
        if not added:
            log.warning("Flock calc: could not add '%s' to give side", name)

    # Add players the owner is receiving (I Receive = right side)
    for name in get_players:
        boxes = await page.query_selector_all(
            'input[placeholder="Search..."]'
        )
        if len(boxes) < 2:
            break
        added = await _add_player_to_side(page, boxes[-1], name)
        if not added:
            log.warning("Flock calc: could not add '%s' to get side", name)

    # Wait for calculation
    await page.wait_for_timeout(2000)

    # Click "Explain Trade" if visible
    try:
        explain_btn = await page.query_selector(
            'button:has-text("Explain Trade")'
        )
        if explain_btn:
            await explain_btn.click()
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # Extract results
    calc_data = await page.evaluate(_CALC_EXTRACT_JS)
    if not calc_data:
        return {"verdict": "error", "error": "extraction failed"}

    return {
        "verdict": calc_data.get("verdict", "unknown"),
        "user_value": calc_data.get("userValue", 0),
        "opponent_value": calc_data.get("opponentValue", 0),
        "value_breakdown": calc_data.get("valueBreakdown", ""),
        "roster_fit": calc_data.get("rosterFit", ""),
    }


async def _scrape_trade_calc_batch(
    trades: list[dict],
    league: str = "DD",
) -> list[dict]:
    """Run multiple trades through the Flock calculator in a single session.

    Args:
        trades: List of dicts, each with "give" and "get" keys (player name lists)
        league: "DD" or "CL"

    Returns list of result dicts (one per trade), same order as input.
    """
    try:
        from playwright.async_api import async_playwright  # pyright: ignore[reportMissingImports]
    except ImportError:
        raise FlockError("playwright not installed")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await _login_flock(page)

            await page.goto(
                "https://flockfantasy.com/trade-calculator",
                wait_until="networkidle",
            )
            await page.wait_for_timeout(2000)

            await _select_league(page, league)

            results: list[dict] = []
            for trade in trades:
                try:
                    result = await _run_single_calc(
                        page,
                        trade.get("give", []),
                        trade.get("get", []),
                    )
                    results.append(result)
                except Exception as e:
                    log.warning("Flock calc failed for trade (non-fatal): %s", e)
                    results.append({"verdict": "error", "error": str(e)})

            return results
        finally:
            await browser.close()


async def _async_run_trade_calc_batch(
    trades: list[dict],
    league: str = "DD",
) -> list[dict]:
    """Async entrypoint for batch trade calculator."""
    return await _scrape_trade_calc_batch(trades, league)


def run_flock_trade_calc(
    trades: list[dict],
    league: str = "DD",
) -> list[dict]:
    """Run multiple trades through the Flock trade calculator in one session.

    Args:
        trades: List of dicts with "give" (player names the owner gives)
                and "get" (player names the owner receives) keys.
        league: "DD" or "CL"

    Returns list of dicts (one per trade):
        [
            {
                "verdict": "You lose!",
                "user_value": 32,
                "opponent_value": 48,
                "value_breakdown": "Based on Expert rankings, ...",
                "roster_fit": "You're ranked 2nd in QB ...",
            },
            ...
        ]

    Raises FlockError on login failure. Individual trade failures are
    returned as {"verdict": "error", "error": "..."}.
    """
    if not flock_email() or not flock_password():
        raise FlockError(
            "FLOCK_EMAIL and FLOCK_PASSWORD are not set. Put them in "
            "Projects/trade-scout/.env or the environment.")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                _async_run_trade_calc_batch(trades, league),
            ).result()
    else:
        return asyncio.run(
            _async_run_trade_calc_batch(trades, league)
        )
