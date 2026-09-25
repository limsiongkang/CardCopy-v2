"""
Pulling product facts out of a page.

Strategy, in order of trust:

1. JSON-LD   - most shops embed a machine-readable "Product" record in the page
               so Google Shopping can read it. When it is there, it is exact.
2. Microdata - the older version of the same idea, marked up with itemprop=.
3. OpenGraph - social-sharing meta tags. Usually has name and price.
4. Visible text - last resort. Looks for the class names and currency patterns
               that shop templates conventionally use.

Each strategy fills in whatever it can; later strategies only fill the gaps
left by earlier ones. A site with clean JSON-LD is read exactly, and a site
with none still produces a usable row.
"""

from __future__ import annotations

import json
import re
from typing import Any

def _first(page, selector: str):
    """First match for a CSS selector, or None. Scrapling has no css_first()."""
    try:
        found = page.css(selector)
    except Exception:
        return None
    return found[0] if found else None


# ----------------------------------------------------------------------
# Price parsing
# ----------------------------------------------------------------------

_PRICE_RE = re.compile(r"\d[\d\s.,]*\d|\d")


def parse_price(raw: Any) -> float | None:
    """Turn '£1,234.56' or '1.234,56 EUR' or 1234.56 into a float."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None

    text = str(raw).strip()
    if not text:
        return None

    match = _PRICE_RE.search(text.replace(" ", " "))
    if not match:
        return None

    num = match.group(0).replace(" ", "")
    has_dot, has_comma = "." in num, "," in num

    if has_dot and has_comma:
        # Whichever separator comes last is the decimal point.
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif has_comma:
        # "1,50" is a decimal comma; "1,500" is a thousands separator.
        tail = num.rsplit(",", 1)[1]
        num = num.replace(",", "." if len(tail) != 3 else "")
    elif has_dot:
        # "1.500" is ambiguous. Three trailing digits plus more than one dot
        # means thousands separators ("1.234.500").
        if num.count(".") > 1:
            num = num.replace(".", "")

    try:
        value = float(num)
    except ValueError:
        return None
    return value if value > 0 else None


# ----------------------------------------------------------------------
# Stock status
# ----------------------------------------------------------------------

_IN_STOCK_SCHEMA = {
    "instock", "preorder", "backorder", "limitedavailability",
    "onlineonly", "instoreonly", "presale",
}
_OUT_OF_STOCK_SCHEMA = {"outofstock", "soldout", "discontinued"}

_OUT_PHRASES = (
    "out of stock", "out-of-stock", "sold out", "soldout", "currently unavailable",
    "no longer available", "not available", "temporarily unavailable",
    "notify me when", "email me when", "back in stock soon",
)
_IN_PHRASES = (
    "in stock", "in-stock", "add to cart", "add to basket", "add to bag",
    "buy now", "available now", "ships today", "pick up today",
)


def stock_from_schema(value: Any) -> bool | None:
    """Read a schema.org availability value like 'https://schema.org/InStock'."""
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name") or ""
    if isinstance(value, list):
        for item in value:
            result = stock_from_schema(item)
            if result is not None:
                return result
        return None

    token = str(value).rstrip("/").rsplit("/", 1)[-1]
    token = token.replace("_", "").replace("-", "").replace(" ", "").lower()
    if token in _IN_STOCK_SCHEMA:
        return True
    if token in _OUT_OF_STOCK_SCHEMA:
        return False
    return None


def stock_from_text(text: str | None) -> bool | None:
    """Guess stock status from the words on the page. Out-of-stock wins ties."""
    if not text:
        return None
    lowered = " ".join(str(text).lower().split())
    for phrase in _OUT_PHRASES:
        if phrase in lowered:
            return False
    for phrase in _IN_PHRASES:
        if phrase in lowered:
            return True
    return None


# ----------------------------------------------------------------------
# Strategy 1: JSON-LD
# ----------------------------------------------------------------------

def _walk_jsonld(node: Any):
    """Yield every dict inside a JSON-LD blob, however deeply nested."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _is_product(node: dict) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        return False
    return any(
        str(t).lower() in ("product", "productmodel", "individualproduct")
        for t in types
    )


def _offer_price(offer: dict) -> float | None:
    """Pull the payable price out of one schema.org Offer."""
    spec = offer.get("priceSpecification")
    if isinstance(spec, list):
        spec = next((s for s in spec if isinstance(s, dict)), None)
    if not isinstance(spec, dict):
        spec = {}

    for candidate in (
        offer.get("price"),
        spec.get("price"),
        offer.get("lowPrice"),
    ):
        price = parse_price(candidate)
        if price:
            return price
    return None


def from_jsonld(page) -> dict:
    """Read the first Product record embedded in the page, if any."""
    found: dict = {}

    for script in page.css('script[type="application/ld+json"]'):
        raw = (script.text or "").strip()
        if not raw:
            continue
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

        for node in _walk_jsonld(blob):
            if not _is_product(node):
                continue

            if not found.get("name") and node.get("name"):
                found["name"] = str(node["name"]).strip()

            offers = node.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if not isinstance(offers, list):
                continue

            for offer in offers:
                if not isinstance(offer, dict):
                    continue

                price = _offer_price(offer)
                if price and "current_price" not in found:
                    found["current_price"] = price

                currency = offer.get("priceCurrency")
                if currency and "currency" not in found:
                    found["currency"] = str(currency).strip()

                in_stock = stock_from_schema(offer.get("availability"))
                if in_stock is not None and "in_stock" not in found:
                    found["in_stock"] = in_stock

            if found.get("current_price"):
                break

        if found.get("current_price"):
            break

    return {k: v for k, v in found.items() if v is not None}


# ----------------------------------------------------------------------
# Strategy 2: Microdata
# ----------------------------------------------------------------------

def _attr(node, *names) -> str | None:
    for name in names:
        try:
            value = node.attrib.get(name)
        except Exception:
            value = None
        if value:
            return str(value).strip()
    return None


def from_microdata(page) -> dict:
    found: dict = {}

    name = _first(page, '[itemprop="name"]')
    if name is not None:
        text = " ".join(str(name.get_all_text()).split())
        if text:
            found["name"] = text[:300]

    for selector in ('[itemprop="price"]', '[itemprop="lowPrice"]'):
        node = _first(page, selector)
        if node is None:
            continue
        price = parse_price(_attr(node, "content")) or parse_price(node.get_all_text())
        if price:
            found["current_price"] = price
            break

    currency = _first(page, '[itemprop="priceCurrency"]')
    if currency is not None:
        found["currency"] = _attr(currency, "content") or " ".join(
            str(currency.get_all_text()).split()
        )

    availability = _first(page, '[itemprop="availability"]')
    if availability is not None:
        status = stock_from_schema(_attr(availability, "href", "content"))
        if status is not None:
            found["in_stock"] = status

    return {k: v for k, v in found.items() if v not in (None, "")}


# ----------------------------------------------------------------------
# Strategy 3: OpenGraph / meta tags
# ----------------------------------------------------------------------

_META_NAME = ('meta[property="og:title"]', 'meta[name="title"]')
_META_PRICE = (
    'meta[property="product:price:amount"]',
    'meta[property="og:price:amount"]',
)


def from_meta(page) -> dict:
    found: dict = {}

    for selector in _META_NAME:
        node = _first(page, selector)
        content = _attr(node, "content") if node is not None else None
        if content:
            found["name"] = content[:300]
            break

    for selector in _META_PRICE:
        node = _first(page, selector)
        if node is None:
            continue
        price = parse_price(_attr(node, "content"))
        if price:
            found["current_price"] = price
            break

    currency = _first(page, 
        'meta[property="product:price:currency"], meta[property="og:price:currency"]'
    )
    if currency is not None:
        value = _attr(currency, "content")
        if value:
            found["currency"] = value

    availability = _first(page, 
        'meta[property="product:availability"], meta[property="og:availability"]'
    )
    if availability is not None:
        value = _attr(availability, "content")
        status = stock_from_schema(value)
        if status is None:
            status = stock_from_text(value)
        if status is not None:
            found["in_stock"] = status

    return {k: v for k, v in found.items() if v not in (None, "")}


# ----------------------------------------------------------------------
# Strategy 4: visible text
# ----------------------------------------------------------------------

_NAME_SELECTORS = (
    "h1[itemprop='name']",
    "h1[class*='product']",
    "h1[class*='title']",
    "[class*='product-title']",
    "[class*='product-name']",
    "[data-testid*='product-title']",
    "h1",
)

_PRICE_SELECTORS = (
    "[class*='sale-price']",
    "[class*='special-price']",
    "[class*='now-price']",
    "[class*='current-price']",
    "[data-testid*='price']",
    "[class*='product-price']",
    "[itemprop='price']",
    "[class*='price']",
    "[id*='price']",
)

# Struck-through "was" prices - these are what tell us an item is on sale.
_WAS_SELECTORS = (
    "[class*='was-price']",
    "[class*='old-price']",
    "[class*='original-price']",
    "[class*='list-price']",
    "[class*='compare-at']",
    "[class*='compare_at']",
    "[class*='regular-price']",
    "[class*='strikethrough']",
    "del",
    "s",
    "strike",
)

_STOCK_SELECTORS = (
    "[class*='stock']",
    "[class*='availability']",
    "[class*='inventory']",
    "[data-testid*='stock']",
    "button[class*='cart']",
    "button[class*='basket']",
)


def from_css_rules(page, rules: dict | None, smart_css) -> dict:
    """
    Read the page using hand-written selectors from selectors.json.

    This runs BEFORE the structured-data strategies. A rule only exists
    because somebody deliberately wrote one for this shop, which usually
    means the automatic strategies got it wrong - so the rule has to be able
    to override them, not merely fill in what they missed.

    `smart_css` carries Scrapling's adaptive mode, so once one of these
    selectors has matched it keeps working after the shop redesigns the page.
    """
    if not rules:
        return {}

    found: dict = {}

    name_selector = rules.get("name")
    if name_selector:
        node = smart_css(page, name_selector, key="name")
        if node is not None:
            text = " ".join(str(node.get_all_text()).split())
            if text:
                found["name"] = text[:300]

    price = None
    price_selector = rules.get("price")
    if price_selector:
        node = smart_css(page, price_selector, key="price")
        if node is not None:
            price = parse_price(node.get_all_text())

    sale = None
    sale_selector = rules.get("sale_price")
    if sale_selector:
        node = smart_css(page, sale_selector, key="sale_price")
        if node is not None:
            sale = parse_price(node.get_all_text())

    # A "sale" figure that is not actually lower is not a sale.
    if price is not None and sale is not None and sale >= price:
        sale = None
    if price is None and sale is not None:
        price, sale = sale, None

    if price is not None:
        found["current_price"] = price
    if sale is not None:
        found["explicit_sale_price"] = sale

    stock_selector = rules.get("in_stock")
    if stock_selector:
        node = smart_css(page, stock_selector, key="in_stock")
        if node is not None:
            status = stock_from_text(str(node.get_all_text()))
            if status is not None:
                found["in_stock"] = status

    return found


def from_visible_text(page, smart_css) -> dict:
    """Last-resort scrape of the rendered page. `smart_css` adds adaptive mode."""
    found: dict = {}

    for selector in _NAME_SELECTORS:
        node = smart_css(page, selector, key="name")
        if node is None:
            continue
        text = " ".join(str(node.get_all_text()).split())
        if text:
            found["name"] = text[:300]
            break

    for selector in _PRICE_SELECTORS:
        node = smart_css(page, selector, key="price")
        if node is None:
            continue
        price = parse_price(node.get_all_text())
        if price:
            found["current_price"] = price
            break

    for selector in _STOCK_SELECTORS:
        node = smart_css(page, selector, key="stock")
        if node is None:
            continue
        status = stock_from_text(str(node.get_all_text()))
        if status is not None:
            found["in_stock"] = status
            break

    return {k: v for k, v in found.items() if v not in (None, "")}


def find_was_price(page, current_price: float | None) -> float | None:
    """
    Look for a crossed-out "was" price.

    Shops publish the price you pay *today* in their structured data. The old
    price normally only appears as struck-through text. If we find one higher
    than today's price, then today's price is a sale price.
    """
    if not current_price:
        return None

    best: float | None = None
    for selector in _WAS_SELECTORS:
        try:
            nodes = page.css(selector)[:6]
        except Exception:
            continue
        for node in nodes:
            price = parse_price(node.get_all_text())
            # Guard against picking up a wildly unrelated number.
            if price and current_price * 1.001 < price <= current_price * 20:
                if best is None or price < best:
                    best = price
        if best:
            return best
    return None


# ----------------------------------------------------------------------
# Listing pages (a category or shop page holding many products)
# ----------------------------------------------------------------------
#
# Everything above reads ONE product from ONE page. A category page holds
# many, so this returns a list of fact dicts instead of a single one.
#
# Each product keeps the link to its own product page. That link, not the
# category address, becomes the row's url - which matters because the
# snapshot, the alerts and the sheet history are all keyed on url. Without
# it, sixteen products from one category would collide on a single key and
# a price drop could not be attributed to the right product.

# Tried in order; the first that finds at least _MIN_LISTING_ITEMS wins.
# The microdata selector leads because it is a published standard rather
# than one shop platform's habit, so it travels furthest between sites.
_LISTING_CONTAINERS = (
    "[itemtype*='schema.org/Product']",
    "li.product",
    ".products .product",
    "[class*='product-card']",
    "[class*='product-item']",
    "[class*='product-tile']",
    "[data-product-id]",
    "article[class*='product']",
)

_LISTING_NAME = (
    "[itemprop='name']",
    ".woocommerce-loop-product__title",
    "[class*='product-title']",
    "[class*='product-name']",
    "[class*='card__heading']",
    "h2 a", "h3 a", "h2", "h3",
)

_LISTING_PRICE = (
    "[itemprop='price']",
    "[class*='price']",
    ".money",
)

# Two is the threshold: a single product page can carry one
# schema.org/Product block of its own, and treating that as a listing would
# turn an ordinary product page into a one-row listing by accident.
_MIN_LISTING_ITEMS = 2


def _all_text(element) -> str | None:
    """
    Full visible text of an element, including its children.

    This must not be element.text, which returns only the element's own
    direct text node. A price marked up as
    <span class="price"><span>$</span>19.99</span> has an empty direct text
    node, so .text would return nothing. Shops nest markup like that
    constantly, so reading children is the normal case.
    """
    for accessor in ("get_all_text", "text_content"):
        method = getattr(element, accessor, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                continue
            if value and str(value).strip():
                return str(value).strip()
    value = getattr(element, "text", None)
    if value and str(value).strip():
        return str(value).strip()
    return None


def _tile_text(element, selectors) -> str | None:
    for selector in selectors:
        found = _first(element, selector)
        if found is not None:
            text = _all_text(found)
            if text:
                return text
    return None


def _tile_link(element, base_url: str) -> str | None:
    """The product's own page address, made absolute."""
    from urllib.parse import urljoin

    try:
        hrefs = element.css("a::attr(href)")
    except Exception:
        return None
    for href in hrefs:
        href = str(href).strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return urljoin(base_url, href)
    return None


def _tile_stock(element) -> bool | None:
    """
    Stock status for one tile.

    Shop platforms usually stamp this on the container's class list
    ("... product outofstock ..."), which is steadier than hunting for a
    badge whose wording changes with every theme.
    """
    try:
        classes = (element.attrib.get("class") or "").lower()
    except Exception:
        classes = ""
    if any(token in classes for token in ("outofstock", "out-of-stock", "sold-out", "soldout")):
        return False
    if any(token in classes for token in ("instock", "in-stock")):
        return True

    for selector in ("[itemprop='availability']", "[class*='stock']", "[class*='availability']"):
        found = _first(element, selector)
        if found is None:
            continue
        for attribute in ("href", "content"):
            try:
                value = found.attrib.get(attribute)
            except Exception:
                value = None
            if value:
                parsed = stock_from_schema(value)
                if parsed is not None:
                    return parsed
        parsed = stock_from_text(_all_text(found))
        if parsed is not None:
            return parsed
    return None


def _tile_prices(element) -> tuple[float | None, float | None]:
    """
    Return (regular_price, sale_price) for one tile.

    Shops mark a reduction with <del> for the old figure and <ins> for the
    new one. When both are present the <del> value is the regular price and
    the <ins> value is what a shopper actually pays.
    """
    old = parse_price(_tile_text(element, ("del .amount", "del .price", "del", "s", "strike")))
    new = parse_price(_tile_text(element, ("ins .amount", "ins .price", "ins")))
    if old is not None and new is not None and new < old:
        return old, new

    plain = parse_price(_tile_text(element, _LISTING_PRICE))
    return plain, None


def extract_listing(page, url: str, rules: dict | None = None) -> list[dict]:
    """
    Pull every product tile from a category or shop page.

    Returns a list of fact dicts in the same shape the single-page
    strategies produce, each with an extra "product_url". An empty list
    means this did not look like a listing page.
    """
    listing_rules = (rules or {}).get("listing") or {}

    candidates: list[str] = []
    if listing_rules.get("item"):
        candidates.append(listing_rules["item"])
    candidates.extend(_LISTING_CONTAINERS)

    tiles = []
    for selector in candidates:
        try:
            found = page.css(selector)
        except Exception:
            continue
        if found and len(found) >= _MIN_LISTING_ITEMS:
            tiles = found
            break

    if not tiles:
        return []

    name_selectors = tuple(
        ([listing_rules["name"]] if listing_rules.get("name") else []) + list(_LISTING_NAME)
    )
    price_selectors = tuple(
        ([listing_rules["price"]] if listing_rules.get("price") else []) + list(_LISTING_PRICE)
    )

    products: list[dict] = []
    seen: set[str] = set()

    for tile in tiles:
        name = _tile_text(tile, name_selectors)
        if not name:
            continue

        if listing_rules.get("price"):
            regular = parse_price(_tile_text(tile, price_selectors))
            sale = None
            if regular is None:
                regular, sale = _tile_prices(tile)
        else:
            regular, sale = _tile_prices(tile)

        link = _tile_link(tile, url)

        # One tile often contains two links to the same product (the image
        # and the title), and some themes repeat a "featured" product above
        # the grid. Keep the first of each.
        key = link or name
        if key in seen:
            continue
        seen.add(key)

        products.append({
            "name": name,
            "price": regular,
            "sale_price": sale,
            "in_stock": _tile_stock(tile),
            "product_url": link,
        })

    return products
