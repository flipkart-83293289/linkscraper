import os


class Settings:
    # Comma-separated list in env, e.g. "https://myfrontend.onrender.com,http://localhost:5173"
    CORS_ORIGINS: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")

    # Hard ceiling on a single scrape job. Render's free tier proxy has
    # roughly a 100s timeout of its own, so keep this comfortably under
    # that. Raised from 75 -> 90 to give the deeper autoscroll + larger
    # asset budget (see MAX_ASSETS) enough room on heavy pages.
    JOB_TIMEOUT_SECONDS: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "90"))

    # Max total size (in MB) an asset (image/font) can be before we skip
    # inlining it and leave a comment placeholder instead. Prevents a single
    # huge hero image from blowing up the output file / memory.
    MAX_ASSET_MB: float = float(os.getenv("MAX_ASSET_MB", "3.0"))

    # Max number of external assets to fetch+inline per page. Protects
    # against pages with hundreds of tracking pixels / icons eating the
    # whole timeout budget. Raised from 150 -> 400: for a "replicate the
    # whole page" goal, missing images (left as broken/external links)
    # matter more than the extra time/size a higher budget costs.
    MAX_ASSETS: int = int(os.getenv("MAX_ASSETS", "400"))

    # Navigation wait strategy: "load", "domcontentloaded", or "networkidle".
    #
    # Default changed from "networkidle" to "domcontentloaded": networkidle
    # waits for network activity to go quiet for 500ms, but heavy sites
    # with continuous polling/analytics/ads NEVER go fully idle -- so it
    # burns the full navigation timeout (45s) before falling back anyway.
    # "domcontentloaded" fires as soon as the DOM is parsed, which combined
    # with our explicit POST_LOAD_SETTLE_MS wait afterward still gives JS
    # time to finish painting, just without the long dead wait.
    WAIT_UNTIL: str = os.getenv("WAIT_UNTIL", "domcontentloaded")

    # Extra settle time (ms) after navigation for lazy-loaded content /
    # JS-driven rendering to finish painting.
    POST_LOAD_SETTLE_MS: int = int(os.getenv("POST_LOAD_SETTLE_MS", "2000"))

    # Which device profile to emulate: "desktop" or "mobile". Mobile often
    # renders faster and lighter (simpler layout, fewer heavy widgets) on
    # sites that serve a distinct responsive mobile experience.
    DEVICE_TYPE: str = os.getenv("DEVICE_TYPE", "desktop")

    # Max related/recommended product links to enrich with lightweight
    # metadata in /api/extract-metadata.
    RELATED_PRODUCTS_LIMIT: int = int(os.getenv("RELATED_PRODUCTS_LIMIT", "8"))


settings = Settings()
