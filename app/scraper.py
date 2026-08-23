"""
Playwright orchestration: launches a memory-constrained Chromium instance,
navigates to the target URL, waits for render, then hands the DOM off to
the asset inliner. The browser and context are fully torn down after every
single request -- on a 512MB instance you cannot afford to keep a browser
warm between requests if you also want headroom for the next job.
"""

import asyncio
import logging

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from app.asset_inliner import inline_all_assets
from app.config import settings
from app.stealth_utils import apply_stealth_patches, random_context_options

logger = logging.getLogger("scraper")

# Flags tuned for a 512MB container. --single-process is aggressive (it
# collapses the renderer and browser processes into one) and trades some
# stability for a meaningfully smaller memory footprint -- acceptable here
# because every job gets a fresh browser instance anyway.
CHROMIUM_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--no-zygote",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--disable-backgrounding-occluded-windows",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=256",
]


class ScrapeError(Exception):
    pass


async def scrape_and_inline(url: str) -> str:
    async with async_playwright() as p:
        browser = None
        context = None
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=CHROMIUM_LAUNCH_ARGS,
            )
            context = await browser.new_context(**random_context_options())

            # Block heavy, non-essential resource types up front to save
            # memory/time -- fonts and media are rarely essential to a
            # visually-accurate static clone and video in particular can be
            # enormous. Images/CSS/JS are always allowed through.
            async def _route_filter(route):
                if route.request.resource_type in ("media",):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _route_filter)

            page = await context.new_page()
            await apply_stealth_patches(page)

            try:
                await page.goto(url, wait_until=settings.WAIT_UNTIL, timeout=45000)
            except PWTimeoutError:
                # networkidle can legitimately never fire on pages with
                # persistent polling/analytics connections. Fall back to
                # whatever DOM state we have rather than failing outright.
                logger.warning(f"Navigation wait_until='{settings.WAIT_UNTIL}' timed out; continuing with current DOM state.")

            # Let lazy-loaded images / late JS paints settle.
            await page.wait_for_timeout(settings.POST_LOAD_SETTLE_MS)

            # Trigger lazy-loaded images by scrolling through the page --
            # many e-commerce sites (Flipkart included) only populate
            # <img src> once an element enters the viewport.
            await _autoscroll(page)

            rendered_html = await page.content()
            final_url = page.url  # after redirects, for correct relative-URL resolution

            html_with_assets = await inline_all_assets(rendered_html, final_url, context)
            return html_with_assets

        except PWTimeoutError as e:
            raise ScrapeError(f"Timed out loading page: {e}")
        except Exception as e:
            raise ScrapeError(f"Failed to render/clone page: {e}")
        finally:
            # Explicit, ordered teardown -- do not rely on GC here.
            if context:
                await context.close()
            if browser:
                await browser.close()


async def _autoscroll(page, step_px: int = 800, max_steps: int = 25, pause_ms: int = 150):
    """Scroll to the bottom in increments to trigger lazy-loaded content."""
    for _ in range(max_steps):
        reached_bottom = await page.evaluate(
            """(step) => {
                const before = window.scrollY;
                window.scrollBy(0, step);
                return window.scrollY === before;
            }""",
            step_px,
        )
        await page.wait_for_timeout(pause_ms)
        if reached_bottom:
            break
    await page.evaluate("window.scrollTo(0, 0)")
