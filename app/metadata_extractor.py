"""
Structured metadata extraction.

Instead of cloning the entire page, this pulls out the handful of fields
people usually actually want from a product/article page: title, price,
currency, main image(s), description, rating, availability.

Extraction strategy, in priority order (first hit wins per field):
  1. JSON-LD structured data (<script type="application/ld+json">) --
     when a site implements schema.org Product/Offer markup, this is the
     most reliable and cleanly-typed source.
  2. Open Graph / Twitter Card meta tags (og:title, og:image, etc.) --
     very widely supported, including by most e-commerce platforms for
     social-share previews.
  3. Heuristic fallback: <title> tag, and a regex scan for common
     currency-symbol + number patterns in the visible text, for sites
     that expose neither of the above.

Being realistic: this only sees whatever HTML actually reached us. If a
site's bot-protection served a CAPTCHA/"unusual traffic" page instead of
the real product page, extraction will faithfully return the CAPTCHA
page's own (mostly empty) metadata -- that's not a bug in this module,
it's an upstream blocking problem this code has no way to see through.
"""

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

PRICE_RE = re.compile(r"(₹|Rs\.?|\$|€|£)\s?[\d,]+(?:\.\d{1,2})?")


def _first_jsonld_product(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") in ("Product", "product"):
                return item
    return None


def _meta_content(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        tag = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def extract_metadata(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result = {
        "source_url": base_url,
        "title": None,
        "price": None,
        "currency": None,
        "description": None,
        "rating": None,
        "review_count": None,
        "availability": None,
        "brand": None,
        "images": [],
    }

    jsonld = _first_jsonld_product(soup)
    if jsonld:
        result["title"] = jsonld.get("name")
        result["description"] = jsonld.get("description")
        brand = jsonld.get("brand")
        if isinstance(brand, dict):
            result["brand"] = brand.get("name")
        elif isinstance(brand, str):
            result["brand"] = brand

        offers = jsonld.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            result["price"] = offers.get("price")
            result["currency"] = offers.get("priceCurrency")
            result["availability"] = offers.get("availability")

        agg = jsonld.get("aggregateRating")
        if isinstance(agg, dict):
            result["rating"] = agg.get("ratingValue")
            result["review_count"] = agg.get("reviewCount")

        images = jsonld.get("image")
        if isinstance(images, str):
            result["images"].append(images)
        elif isinstance(images, list):
            result["images"].extend([i for i in images if isinstance(i, str)])

    # Fill any gaps from Open Graph / Twitter meta tags.
    if not result["title"]:
        result["title"] = _meta_content(soup, "og:title", "twitter:title")
    if not result["title"] and soup.title:
        result["title"] = soup.title.get_text(strip=True)

    if not result["description"]:
        result["description"] = _meta_content(soup, "og:description", "twitter:description", "description")

    og_image = _meta_content(soup, "og:image", "twitter:image")
    if og_image:
        result["images"].append(urljoin(base_url, og_image))

    if not result["price"]:
        price_amount = _meta_content(soup, "product:price:amount", "og:price:amount")
        if price_amount:
            result["price"] = price_amount
    if not result["currency"]:
        currency = _meta_content(soup, "product:price:currency", "og:price:currency")
        if currency:
            result["currency"] = currency

    # Last-resort heuristic: scan visible text for a currency-symbol +
    # number pattern if nothing structured gave us a price.
    if not result["price"]:
        body_text = soup.get_text(" ", strip=True)
        match = PRICE_RE.search(body_text)
        if match:
            result["price"] = match.group(0)

    # De-duplicate images while preserving order, cap to a sane count.
    seen = set()
    deduped = []
    for img in result["images"]:
        absolute = urljoin(base_url, img)
        if absolute not in seen:
            seen.add(absolute)
            deduped.append(absolute)
    result["images"] = deduped[:10]

    return result


# Common URL path fragments used by product detail pages across major
# e-commerce platforms (Flipkart uses /p/, Amazon uses /dp/ and /gp/product/,
# many others use /product/ or /item/). This is a generic heuristic, not
# tied to any single site.
PRODUCT_LINK_PATTERNS = ("/p/", "/dp/", "/gp/product/", "/product/", "/item/", "/pd/")


def find_related_product_links(html: str, base_url: str, exclude_url: str | None = None, limit: int = 8) -> list[str]:
    """
    Scans a page for outgoing links that look like other product pages --
    useful for "related products" / "customers also bought" / "recommended
    for you" rails at the bottom of a listing. Generic pattern-matching
    heuristic, not site-specific.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[str] = []

    exclude_normalized = exclude_url.rstrip("/") if exclude_url else None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(pat in href for pat in PRODUCT_LINK_PATTERNS):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        normalized = absolute.rstrip("/")
        if exclude_normalized and normalized == exclude_normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        results.append(absolute)
        if len(results) >= limit:
            break

    return results
