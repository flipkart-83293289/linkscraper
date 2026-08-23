"""
Asset Inlining Engine.

Takes a rendered DOM snapshot (already-evaluated HTML string) plus the
page's base URL, then:
  1. Downloads every linked stylesheet and inlines it into <style> blocks.
  2. Downloads external <script src="..."> and inlines them, EXCEPT
     known bot-detection/anti-fraud vendor scripts (PerimeterX, DataDome,
     Akamai Bot Manager, reCAPTCHA/hCaptcha, FingerprintJS, Imperva/
     Incapsula, Cloudflare Turnstile/challenge, Forter, Kasada, Arkose/
     FunCaptcha) -- those get removed instead, since they only try to
     phone home to live detection APIs offline and can blank/redirect the
     page. All other JS (carousels, tab-switchers, quantity pickers,
     etc.) is kept so the clone stays as close to the original as
     possible.
  3. Converts <img src>, <img srcset>, CSS background-image url(...), and
     <link rel="icon"> targets into base64 data: URIs.
  4. Rewrites any remaining relative URLs (href/src on <a>, <form>, etc.)
     to absolute URLs so the offline file's links still work if clicked.
  5. If a mobile viewport width was captured, pins the page to that width
     via CSS so the mobile layout renders correctly no matter how wide
     the browser window is that later opens the file (see
     `force_mobile_width` below for why this is necessary).

Design choices:
  - All network fetches use the *same* browser context's request API
    (`context.request`) rather than a separate `requests`/`httpx` client.
    This reuses cookies/headers already negotiated with the target site
    (helps with hot-linking-protected assets) and avoids a second
    networking stack in an already memory-constrained process.
  - Fetches run with bounded concurrency (not fully parallel) to avoid
    spiking memory with dozens of simultaneous buffers on a 512MB box.
"""

import asyncio
import base64
import logging
import mimetypes
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger("asset-inliner")

ASSET_FETCH_CONCURRENCY = 8
_fetch_semaphore = asyncio.Semaphore(ASSET_FETCH_CONCURRENCY)

# Known bot-detection / anti-fraud / CAPTCHA vendor signatures. Matched
# case-insensitively against a script's `src` URL (for external scripts)
# or its raw text content (for inline scripts). This list is inherently
# incomplete -- new vendors and renamed endpoints appear over time -- but
# it covers the large majority of what you'll actually run into.
SECURITY_SCRIPT_SIGNATURES = [
    "perimeterx", "px-cdn", "pxi-captcha", "_pxappid", "_pxmvid",
    "datadome", "dd-b",
    "distil", "distilnetworks",
    "akamai", "bmak", "sensor_data", "abck",
    "imperva", "incapsula", "incap_ses",
    "recaptcha", "grecaptcha", "google.com/recaptcha",
    "hcaptcha",
    "fingerprintjs", "fpjs.io", "fp-collect",
    "cf-turnstile", "challenges.cloudflare.com", "cf_chl",
    "forter",
    "kasada", "kpsdk",
    "arkoselabs", "funcaptcha",
    "shieldsquare", "radware",
    "geetest",
    "humansecurity", "perimeterx-humans",
]
_SECURITY_SCRIPT_RE = re.compile("|".join(re.escape(sig) for sig in SECURITY_SCRIPT_SIGNATURES), re.I)


def _is_security_script(text: str | None) -> bool:
    if not text:
        return False
    return bool(_SECURITY_SCRIPT_RE.search(text))


def _guess_mime(url: str, content_type_header: str | None) -> str:
    if content_type_header:
        return content_type_header.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(url)
    return guessed or "application/octet-stream"


async def _fetch_bytes(context, url: str) -> tuple[bytes | None, str | None]:
    """Fetch a resource via the browser context. Returns (bytes, mime) or (None, None) on failure."""
    async with _fetch_semaphore:
        try:
            resp = await context.request.get(url, timeout=8000)
            if not resp.ok:
                logger.warning(f"Asset fetch non-200 ({resp.status}): {url}")
                return None, None
            body = await resp.body()
            if len(body) > settings.MAX_ASSET_MB * 1024 * 1024:
                logger.info(f"Skipping oversized asset ({len(body)/1e6:.1f}MB): {url}")
                return None, None
            mime = _guess_mime(url, resp.headers.get("content-type"))
            return body, mime
        except Exception as e:
            logger.warning(f"Asset fetch failed for {url}: {e}")
            return None, None


def _to_data_uri(data: bytes, mime: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")


async def _inline_css_urls(context, css_text: str, base_url: str, asset_budget: list[int]) -> str:
    """Replace url(...) references inside a CSS blob (fonts, background images) with data URIs."""
    matches = list(CSS_URL_RE.finditer(css_text))
    replacements = {}
    for m in matches:
        if asset_budget[0] <= 0:
            break
        raw_url = m.group(1)
        if raw_url.startswith("data:"):
            continue
        absolute = urljoin(base_url, raw_url)
        if absolute in replacements:
            continue
        data, mime = await _fetch_bytes(context, absolute)
        asset_budget[0] -= 1
        if data:
            replacements[raw_url] = _to_data_uri(data, mime)

    def _sub(match):
        raw_url = match.group(1)
        return f"url('{replacements.get(raw_url, raw_url)}')"

    return CSS_URL_RE.sub(_sub, css_text)


async def inline_all_assets(html: str, base_url: str, context, force_mobile_width: int | None = None) -> str:
    """
    Main entry point: takes rendered HTML + base URL, returns a fully
    self-contained HTML string with CSS/JS/images inlined.

    force_mobile_width: if set (the viewport width Chromium actually used
    during capture), pins the page to that width via injected CSS. This
    matters because CSS media queries and any surviving JS layout logic
    were evaluated at THAT width during capture -- if the resulting file
    is later opened in a normal desktop browser (a much wider real
    viewport), those same media queries stop applying and the "mobile"
    layout can visually fall apart. Pinning html/body to the captured
    width keeps the mobile layout intact regardless of the viewer's
    actual window size.
    """
    soup = BeautifulSoup(html, "html.parser")
    asset_budget = [settings.MAX_ASSETS]  # mutable counter shared across helpers

    # --- 1. Inline <link rel="stylesheet"> ---
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href")
        if not href:
            continue
        absolute = urljoin(base_url, href)
        data, _ = await _fetch_bytes(context, absolute)
        if data is None:
            continue
        css_text = data.decode("utf-8", errors="ignore")
        css_text = await _inline_css_urls(context, css_text, absolute, asset_budget)
        style_tag = soup.new_tag("style")
        style_tag.string = css_text
        link.replace_with(style_tag)

    # --- 2. Inline url(...) references inside existing <style> blocks ---
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            style_tag.string = await _inline_css_urls(context, style_tag.string, base_url, asset_budget)

    # --- 3. Inline inline `style="background-image:url(...)"` attributes ---
    for tag in soup.find_all(style=True):
        tag["style"] = await _inline_css_urls(context, tag["style"], base_url, asset_budget)

    # --- 4. Scripts: remove ONLY known security/anti-bot vendors, inline
    #         everything else so real page behavior is preserved ---
    for script in soup.find_all("script"):
        src = script.get("src")
        if src:
            absolute = urljoin(base_url, src)
            if _is_security_script(absolute):
                script.decompose()
                continue
            if asset_budget[0] <= 0:
                # Out of fetch budget -- leave the reference alone rather
                # than silently dropping potentially-important app JS.
                continue
            data, _ = await _fetch_bytes(context, absolute)
            asset_budget[0] -= 1
            if data is None:
                script.replace_with(soup.new_comment(f" external script skipped (fetch failed): {absolute} "))
                continue
            text = data.decode("utf-8", errors="ignore")
            if _is_security_script(text):
                script.decompose()
                continue
            new_script = soup.new_tag("script")
            new_script.string = text
            del new_script["src"]
            script.replace_with(new_script)
        else:
            # Inline <script>...</script> -- remove only if its own
            # content matches a known security-vendor signature.
            if script.string and _is_security_script(script.string):
                script.decompose()

    # --- 5. Inline <img src> and srcset ---
    for img in soup.find_all("img"):
        if asset_budget[0] <= 0:
            break
        src = img.get("src")
        if src and not src.startswith("data:"):
            absolute = urljoin(base_url, src)
            data, mime = await _fetch_bytes(context, absolute)
            asset_budget[0] -= 1
            if data:
                img["src"] = _to_data_uri(data, mime)
        if img.get("srcset"):
            # Simplify: drop srcset entirely once src is inlined (offline file
            # doesn't need responsive variants; keeps output size sane).
            del img["srcset"]

    # --- 6. Inline favicon / touch icons ---
    for link in soup.find_all("link", rel=lambda v: v and any(r in v for r in ("icon", "apple-touch-icon"))):
        href = link.get("href")
        if not href or asset_budget[0] <= 0:
            continue
        absolute = urljoin(base_url, href)
        data, mime = await _fetch_bytes(context, absolute)
        asset_budget[0] -= 1
        if data:
            link["href"] = _to_data_uri(data, mime)

    # --- 7. Rewrite remaining relative links/forms to absolute so clicking
    #         them offline at least goes to the live site instead of 404s ---
    for tag, attr in ((a, "href") for a in soup.find_all("a", href=True)):
        val = tag.get(attr)
        if val and not val.startswith(("http://", "https://", "#", "mailto:", "javascript:", "data:")):
            tag[attr] = urljoin(base_url, val)
    for tag in soup.find_all("form", action=True):
        val = tag.get("action")
        if val and not val.startswith(("http://", "https://")):
            tag["action"] = urljoin(base_url, val)

    # --- 8. Drop CSP meta tags that could interfere with offline rendering ---
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("Content-Security-Policy", re.I)}):
        meta.decompose()

    # --- 9. Pin mobile width, if this was a mobile capture ---
    if force_mobile_width:
        # Force the real viewport meta (mobile browsers honor this).
        viewport_meta = soup.find("meta", attrs={"name": "viewport"})
        content_value = f"width={force_mobile_width}, initial-scale=1"
        if viewport_meta:
            viewport_meta["content"] = content_value
        else:
            new_meta = soup.new_tag("meta", attrs={"name": "viewport", "content": content_value})
            if soup.head:
                soup.head.insert(0, new_meta)

        # Force the visual width via CSS too -- desktop browsers ignore
        # the mobile viewport meta tag entirely, so without this a mobile
        # capture opened on a desktop browser would stretch back out to
        # full window width and any @media (max-width: ...) rules that
        # made the layout correct at capture time would stop applying.
        pin_css = f"""
        html {{ width: {force_mobile_width}px !important; margin: 0 auto !important; }}
        body {{ width: {force_mobile_width}px !important; max-width: {force_mobile_width}px !important; margin: 0 auto !important; overflow-x: hidden !important; }}
        """
        pin_style_tag = soup.new_tag("style")
        pin_style_tag.string = pin_css
        if soup.head:
            soup.head.append(pin_style_tag)

    provenance = f"<!-- Cloned offline snapshot of {base_url} -->\n"
    # Pretty-print for readability -- the user wants to view this like
    # normal single-page source code, not a single minified line. Note:
    # prettify() can occasionally add whitespace around inline elements
    # (e.g. <span>/<a> inside text) that very rarely nudges spacing by a
    # pixel or two -- an acceptable trade for genuinely readable output.
    return provenance + soup.prettify()
