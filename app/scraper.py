"""
Playwright orchestration: launches a memory-constrained Chromium instance,
navigates to the target URL, waits for render, then hands the DOM off to
the asset inliner. The browser and context are fully torn down after every
single request -- on a 512MB instance you cannot afford to keep a browser
warm between requests if you also want headroom for the next job.
"""

import asyncio
import logging
import resource

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from app.asset_inliner import inline_all_assets
from app.config import settings
from app.stealth_utils import apply_stealth_patches, random_context_options

logger = logging.getLogger("scraper")


def _log_peak_memory(label: str) -> None:
    """
    Logs the process's peak resident memory so far (ru_maxrss, in MB on
    Linux). This is the whole Python process's peak, not just Chromium's,
    but a sudden jump right before a failure is a strong signal that
    memory pressure (not a code bug) caused it -- useful for confirming
    or ruling out the OOM theory from Render's Logs tab without needing
    any extra dependency.
    """
    try:
        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        logger.info(f"[mem] {label}: peak RSS so far = {peak_kb / 1024:.1f} MB")
    except Exception:
        pass

# Flags tuned for a 512MB container.
#
# IMPORTANT: --single-process is deliberately NOT used here. It collapses
# the renderer and browser into one OS process to save memory, but
# Chromium's own maintainers have long discouraged it for real page loads
# -- it is known to crash under heavier JS workloads (many workers,
# iframes, ad/analytics scripts), which is exactly the "light pages work,
# heavy pages throw a weird internal error" pattern this flag causes. When
# the single process dies mid-render, Playwright's driver can end up
# calling a method on an already-torn-down connection, which surfaces as
# a confusing 'NoneType' object is not callable error rather than a clean
# crash message. The flags below still meaningfully reduce memory use
# without that instability.
CHROMIUM_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI,site-per-process",
    "--js-flags=--max-old-space-size=256",
]


class ScrapeError(Exception):
    pass


async def _render_page(url: str, block_images: bool = True, device_type: str = "desktop"):
    """
    Shared rendering step: launches Chromium, navigates, settles, and
    returns (rendered_html, final_url, context, browser, playwright_cm).
    Caller is responsible for closing context/browser when done -- kept
    open here because the asset-inlining path needs the live context to
    fetch stylesheets/scripts/images through it.

    block_images: when True (default), <img>/background-image/font
    network requests are aborted during the *initial navigation*. This is
    a significant memory saver on image-heavy pages (Flipkart-style
    e-commerce listings can have 50-100+ product images), because
    Chromium never has to decode all that pixel data into memory just to
    give us the DOM. Blocking the network request does NOT remove the
    `src`/`srcset` attribute values from the DOM -- JS still sets those
    strings even if the actual image fetch is aborted -- so the asset
    inliner can still fetch and embed the real image bytes afterward via
    a plain HTTP GET (which uses far less memory than Chromium's decode
    pipeline).
    """
    p = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        context = await browser.new_context(**random_context_options(device_type, playwright=p))

        blocked_types = {"media", "image", "font"} if block_images else {"media"}

        async def _route_filter(route):
            if route.request.resource_type in blocked_types:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", _route_filter)

        page = await context.new_page()
        await apply_stealth_patches(page)

        _log_peak_memory(f"before goto({url})")
        try:
            await page.goto(url, wait_until=settings.WAIT_UNTIL, timeout=45000)
        except PWTimeoutError:
            logger.warning(f"Navigation wait_until='{settings.WAIT_UNTIL}' timed out; continuing with current DOM state.")
        _log_peak_memory(f"after goto({url})")

        await page.wait_for_timeout(settings.POST_LOAD_SETTLE_MS)
        await _autoscroll(page)
        _log_peak_memory(f"after autoscroll({url})")

        rendered_html = await page.content()
        final_url = page.url
        return rendered_html, final_url, context, browser, p
    except Exception:
        # Clean up partial state before re-raising -- caller's finally
        # block never runs since it never received context/browser/p.
        if context:
            await context.close()
        if browser:
            await browser.close()
        await p.stop()
        raise


async def scrape_and_inline(url: str, device_type: str = "desktop") -> str:
    try:
        rendered_html, final_url, context, browser, p = await _render_page(url, device_type=device_type)
    except PWTimeoutError as e:
        logger.exception(f"Playwright timeout while cloning {url}")
        raise ScrapeError(f"Timed out loading page: {e}")
    except Exception as e:
        logger.exception(f"Unhandled failure while rendering {url}")
        raise ScrapeError(f"Failed to render/clone page: {type(e).__name__}: {e}")

    try:
        return await inline_all_assets(rendered_html, final_url, context)
    except Exception as e:
        logger.exception(f"Unhandled failure while inlining assets for {url}")
        raise ScrapeError(f"Failed to inline assets: {type(e).__name__}: {e}")
    finally:
        await context.close()
        await browser.close()
        await p.stop()


async def _try_lightweight_fetch(url: str, device_type: str = "desktop") -> str | None:
    """
    Tier 1 for metadata extraction: a plain HTTP GET through Playwright's
    standalone API-request client -- NO browser process is launched at
    all. Many sites (including most e-commerce platforms) server-render
    Open Graph / JSON-LD metadata into the initial HTML for social-share
    previews, even when the rest of the page is a JS-heavy SPA. If that's
    present, we get title/price/image/description without ever paying
    Chromium's memory cost. Returns None on any failure so the caller can
    fall back to full browser rendering.
    """
    p = await async_playwright().start()
    request_context = None
    try:
        options = random_context_options(device_type, playwright=p)
        request_context = await p.request.new_context(
            extra_http_headers={
                "User-Agent": options["user_agent"],
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        resp = await request_context.get(url, timeout=12000)
        if not resp.ok:
            return None
        return await resp.text()
    except Exception as e:
        logger.info(f"Lightweight fetch failed for {url}, will fall back to full render: {e}")
        return None
    finally:
        if request_context:
            await request_context.dispose()
        await p.stop()


def _metadata_looks_sufficient(metadata: dict) -> bool:
    """Heuristic: is this enough to skip the expensive full browser render?"""
    return bool(metadata.get("title")) and bool(metadata.get("price") or metadata.get("images"))


async def scrape_and_extract_metadata(url: str, device_type: str = "desktop") -> dict:
    """
    Two-tier metadata extraction:
      Tier 1 (cheap): plain HTTP fetch, no browser -- works when the site
      server-renders enough metadata into the initial HTML.
      Tier 2 (expensive): full Playwright render with images/fonts
      blocked, used only if Tier 1 didn't find enough.
    """
    from app.metadata_extractor import extract_metadata

    raw_html = await _try_lightweight_fetch(url, device_type=device_type)
    if raw_html:
        try:
            metadata = extract_metadata(raw_html, url)
            if _metadata_looks_sufficient(metadata):
                metadata["_extraction_tier"] = "lightweight_fetch"
                return metadata
        except Exception as e:
            logger.info(f"Tier-1 metadata parse failed for {url}, falling back to full render: {e}")

    # Tier 2 fallback: full browser render, images/fonts blocked for memory.
    try:
        rendered_html, final_url, context, browser, p = await _render_page(url, block_images=True, device_type=device_type)
    except PWTimeoutError as e:
        logger.exception(f"Playwright timeout while extracting metadata from {url}")
        raise ScrapeError(f"Timed out loading page: {e}")
    except Exception as e:
        logger.exception(f"Unhandled failure while rendering {url}")
        raise ScrapeError(f"Failed to render page: {type(e).__name__}: {e}")

    try:
        metadata = extract_metadata(rendered_html, final_url)
        metadata["_extraction_tier"] = "full_render"
        return metadata
    except Exception as e:
        logger.exception(f"Unhandled failure while extracting metadata for {url}")
        raise ScrapeError(f"Failed to extract metadata: {type(e).__name__}: {e}")
    finally:
        await context.close()
        await browser.close()
        await p.stop()


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
