"""
Asset Inlining Engine.

Takes a rendered DOM snapshot (already-evaluated HTML string) plus the
page's base URL, then:
  1. Downloads every linked stylesheet and inlines it into <style> blocks.
  2. Downloads every external <script src> and inlines it into <script>
     blocks (skipping ones that fail, e.g. blocked by CORS-adjacent CDN
     rules -- a failed script inline degrades gracefully rather than
     crashing the whole job).
  3. Converts <img src>, <img srcset>, CSS background-image url(...), and
     <link rel="icon"> targets into base64 data: URIs.
  4. Rewrites any remaining relative URLs (href/src on <a>, <form>, etc.)
     to absolute URLs so the offline file's links still work if clicked.

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

ASSET_FETCH_CONCURRENCY = 4
_fetch_semaphore = asyncio.Semaphore(ASSET_FETCH_CONCURRENCY)


def _guess_mime(url: str, content_type_header: str | None) -> str:
    if content_type_header:
        return content_type_header.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(url)
    return guessed or "application/octet-stream"


async def _fetch_bytes(context, url: str) -> tuple[bytes | None, str | None]:
    """Fetch a resource via the browser context. Returns (bytes, mime) or (None, None) on failure."""
    async with _fetch_semaphore:
        try:
            resp = await context.request.get(url, timeout=15000)
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


async def inline_all_assets(html: str, base_url: str, context) -> str:
    """
    Main entry point: takes rendered HTML + base URL, returns a fully
    self-contained HTML string with CSS/JS/images inlined.
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

    # --- 4. Inline external <script src="..."> ---
    for script in soup.find_all("script", src=True):
        if asset_budget[0] <= 0:
            break
        src = script.get("src")
        absolute = urljoin(base_url, src)
        data, _ = await _fetch_bytes(context, absolute)
        asset_budget[0] -= 1
        if data is None:
            # Leave a comment rather than a dangling src that will 404 offline.
            script.replace_with(soup.new_comment(f" external script skipped: {absolute} "))
            continue
        new_script = soup.new_tag("script")
        new_script.string = data.decode("utf-8", errors="ignore")
        del new_script["src"]
        script.replace_with(new_script)

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

    # --- 8. Drop noscript/meta refresh/CSP meta tags that could interfere
    #         with offline rendering ---
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("Content-Security-Policy", re.I)}):
        meta.decompose()

    provenance = f"<!-- Cloned offline snapshot of {base_url} -->\n"
    return provenance + str(soup)
