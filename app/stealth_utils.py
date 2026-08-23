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

MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

MOBILE_VIEWPORTS = [
    {"width": 390, "height": 844},   # iPhone 12/13/14-class
    {"width": 412, "height": 915},   # common Android
    {"width": 360, "height": 800},
]

LOCALES = ["en-US", "en-GB", "en-IN"]
TIMEZONES = ["America/New_York", "Europe/London", "Asia/Kolkata"]


def random_context_options(device_type: str = "desktop") -> dict:
    """
    Return a randomized set of new_context() kwargs for Playwright.
    device_type: "desktop" (default) or "mobile" -- mobile emulation often
    loads faster on sites that serve a lighter responsive layout, and
    tends to produce simpler, more readable output HTML.
    """
    if device_type == "mobile":
        return {
            "user_agent": random.choice(MOBILE_USER_AGENTS),
            "viewport": random.choice(MOBILE_VIEWPORTS),
            "locale": random.choice(LOCALES),
            "timezone_id": random.choice(TIMEZONES),
            "device_scale_factor": 2,
            "is_mobile": True,
            "has_touch": True,
        }
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
    Inject the common navigator/plugin/webdriver patches manually via an
    init script, so it runs before any page JS.

    NOTE: we deliberately do NOT depend on the third-party
    `playwright-stealth` package here. Its public API has changed
    incompatibly between major versions (1.x exposed `stealth_async(page)`;
    2.x replaced it with a `Stealth().use_async(playwright)` context
    manager that wraps the Playwright driver, not the page) and pinning a
    version doesn't fully protect you from build-time resolution
    surprises. Calling the wrong-generation API silently produces
    "'NoneType' object is not callable" style crashes instead of a clean
    ImportError. The manual patches below cover the same well-known
    fingerprint tells (navigator.webdriver, plugins, permissions.query)
    without that fragility.
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


def human_delay_ms() -> int:
    """Small randomized delay to avoid perfectly uniform request timing."""
    return random.randint(300, 1200)
