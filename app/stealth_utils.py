"""
Fingerprint randomization + stealth patching helpers.

Important honesty note: none of this guarantees bypassing modern anti-bot
systems (Cloudflare, PerimeterX, Akamai, DataDome, Flipkart's own defenses,
etc). Sites that actively defend against scraping will still detect and
block headless Chromium a meaningful fraction of the time, with or without
these tricks -- and doing this against a site's explicit Terms of Service
may violate those terms regardless of technical success. Treat this module
as "reduce obvious tells", not "guaranteed evasion". Only run it against
sites you have the right to scrape (your own pages, or ones whose ToS/robots
policy permits it).
"""

import random

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

LOCALES = ["en-US", "en-GB", "en-IN"]
TIMEZONES = ["America/New_York", "Europe/London", "Asia/Kolkata"]


def random_context_options() -> dict:
    """Return a randomized set of new_context() kwargs for Playwright."""
    return {
        "user_agent": random.choice(USER_AGENTS),
        "viewport": random.choice(VIEWPORTS),
        "locale": random.choice(LOCALES),
        "timezone_id": random.choice(TIMEZONES),
        "device_scale_factor": random.choice([1, 1, 1.5, 2]),
        "is_mobile": False,
        "has_touch": False,
    }


async def apply_stealth_patches(page) -> None:
    """
    Inject the common navigator/plugin/webdriver patches that
    playwright-stealth also applies, as an init script so it runs before
    any page JS. If the `playwright_stealth` package is installed and
    importable, prefer that (it's more comprehensive and maintained);
    this function is the fallback/supplement.
    """
    stealth_js = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    window.chrome = { runtime: {} };
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
      parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
    );
    """
    await page.add_init_script(stealth_js)

    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
    except ImportError:
        # playwright-stealth not installed / not applicable to this
        # Playwright version -- the manual init script above still applies.
        pass


def human_delay_ms() -> int:
    """Small randomized delay to avoid perfectly uniform request timing."""
    return random.randint(300, 1200)
