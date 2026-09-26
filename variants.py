"""
Reading every variant of a product - each size, colour, grind and so on.

Why this exists
---------------
A product page's headline data describes the product as a whole, and for a
product with options that headline is often wrong about stock. WooCommerce,
for instance, marks a variable product "out of stock" at the top level while
every one of its sizes can be bought. Prices differ per variant too, and the
headline only carries one of them.

What it does
------------
Shops that let you pick a variant already ship every variant's price and
stock inside the page (or right next to it), because the page's own
JavaScript needs that data to update the price when you pick an option. So
there is no need to click through the options: the data is read directly,
which is faster and far more reliable than simulating clicks.

Sources, tried in order:

  1. Shopify     - every Shopify store serves /products/<handle>.js, a small
                   JSON file listing each variant's price, compare-at (sale)
                   price and availability.
  2. WooCommerce - variable products carry every variation in the
                   data-product_variations attribute of the add-to-cart form.
  3. schema.org  - the published structured-data standard for variants
                   (ProductGroup + hasVariant), used by many other platforms.

Each variant comes back as a dict:

    {"product": parent name, "label": "12 OZ BAG / Whole Bean",
     "price": regular price, "sale_price": price paid if reduced, else None,
     "in_stock": True / False / None, "currency": "USD" or "",
     "url": a link that opens this exact variant, "source": where it came from}

An empty list means the page has no variants (or none we can read), and the
caller keeps its ordinary one-row result.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, urlunparse


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def _split_price(regular: float | None, paid: float | None):
    """
    Return (price, sale_price) in the sheet's convention: 'price' is the
    regular price, and 'sale price' is only filled when the shopper pays
    less than that.
    """
    if regular is not None and paid is not None and paid < regular:
        return regular, paid
    if paid is not None:
        return paid, None
    return regular, None


def _base_url(url: str) -> str:
    """The URL without query string or fragment."""
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))


def _with_query(url: str, params: dict) -> str:
    base = _base_url(url)
    return f"{base}?{urlencode(params)}" if params else base


def _page_html(page) -> str:
    for attribute in ("html_content", "body", "text"):
        value = getattr(page, attribute, None)
        if value is None:
            continue
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
        return str(value)
    return ""


def _stock_from_schema(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).rsplit("/", 1)[-1].lower().replace("_", "").replace("-", "")
    if text in ("outofstock", "soldout", "discontinued"):
        return False
    if text in ("instock", "limitedavailability", "onlineonly", "instoreonly",
                "preorder", "presale", "backorder"):
        return True
    return None


def _unique_urls(variants: list[dict]) -> list[dict]:
    """Every row must have its own URL: history and alerts are keyed on it."""
    seen: dict[str, int] = {}
    for variant in variants:
        url = variant["url"]
        if url in seen:
            seen[url] += 1
            variant["url"] = f"{url}#v{seen[url]}"
        else:
            seen[url] = 0
    return variants


# ----------------------------------------------------------------------
# 1. Shopify
# ----------------------------------------------------------------------

_SHOPIFY_MARKERS = ("cdn.shopify.com", "Shopify.shop", "shopify-digital-wallet",
                    "window.ShopifyAnalytics", "myshopify.com")


def _shopify_product_url(url: str) -> str | None:
    """
    .../products/<handle>               -> https://host/products/<handle>
    .../collections/x/products/<handle> -> https://host/products/<handle>
    """
    parts = urlparse(url)
    match = re.search(r"/products/([^/?#]+)", parts.path)
    if not match:
        return None
    handle = match.group(1)
    if handle.endswith(".js") or handle.endswith(".json"):
        handle = handle.rsplit(".", 1)[0]
    return f"{parts.scheme}://{parts.netloc}/products/{handle}"


def from_shopify(page, url: str, fetch_json: Callable[[str], Any] | None) -> list[dict]:
    product_url = _shopify_product_url(url)
    if not product_url or fetch_json is None:
        return []
    html = _page_html(page)
    if not any(marker in html for marker in _SHOPIFY_MARKERS):
        return []

    data = fetch_json(product_url + ".js")
    if not isinstance(data, dict):
        return []

    raw_variants = data.get("variants") or []
    # A product with no real options still has one variant, called
    # "Default Title". That is a single product, not a list of one.
    if len(raw_variants) < 2:
        return []

    name = str(data.get("title") or "").strip()
    variants: list[dict] = []
    for raw in raw_variants:
        # /products/x.js gives prices in cents as whole numbers;
        # /products/x.json gives them as "16.99". Accept either.
        def money(value):
            if isinstance(value, int):
                return value / 100
            return _to_float(value)

        paid = money(raw.get("price"))
        compare_at = money(raw.get("compare_at_price"))
        price, sale = _split_price(compare_at, paid)

        available = raw.get("available")
        variants.append({
            "product": name,
            "label": str(raw.get("title") or raw.get("public_title") or "").strip(),
            "price": price,
            "sale_price": sale,
            "in_stock": bool(available) if available is not None else None,
            "currency": "",
            "url": f"{product_url}?variant={raw.get('id')}" if raw.get("id") else product_url,
            "source": "Shopify variant data",
        })
    return _unique_urls(variants)


# ----------------------------------------------------------------------
# 2. WooCommerce
# ----------------------------------------------------------------------

def _attribute_title(key: str) -> str:
    """attribute_pa_colour -> Colour"""
    name = key.removeprefix("attribute_").removeprefix("pa_")
    return name.replace("-", " ").replace("_", " ").strip().title() or "Option"


def from_woocommerce(page, url: str) -> list[dict]:
    try:
        forms = page.css("form.variations_form")
    except Exception:  # noqa: BLE001
        return []
    if not forms:
        return []
    form = forms[0]

    raw = (form.attrib.get("data-product_variations") or "").strip()
    # "false" means the shop has too many combinations to embed and loads
    # them one at a time as the shopper picks options. Nothing to read here.
    if not raw or raw == "false":
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []

    # The variation data holds option slugs ("blue", "l"); the drop-downs on
    # the page hold what the shopper actually reads ("Blue", "L").
    labels: dict[str, dict[str, str]] = {}
    try:
        for select in form.css("select"):
            key = select.attrib.get("data-attribute_name") or select.attrib.get("name") or ""
            if not key.startswith("attribute_"):
                continue
            options = {}
            for option in select.css("option"):
                value = option.attrib.get("value") or ""
                text = option.get_all_text().strip() if hasattr(option, "get_all_text") else ""
                if value:
                    options[value] = text or value
            labels[key] = options
    except Exception:  # noqa: BLE001
        pass

    name = ""
    try:
        heading = page.css("h1.product_title, h1.entry-title, h1")
        if heading:
            name = heading[0].get_all_text().strip()
    except Exception:  # noqa: BLE001
        pass

    variants: list[dict] = []
    for raw_variant in data:
        if not isinstance(raw_variant, dict):
            continue
        if raw_variant.get("variation_is_active") is False:
            continue

        parts, query = [], {}
        for key, value in (raw_variant.get("attributes") or {}).items():
            if value:
                parts.append(labels.get(key, {}).get(value, value))
                query[key] = value
            else:
                # An empty value means "any" - one variation covering every
                # choice of that option.
                parts.append(f"Any {_attribute_title(key).lower()}")

        paid = _to_float(raw_variant.get("display_price"))
        regular = _to_float(raw_variant.get("display_regular_price"))
        price, sale = _split_price(regular, paid)

        in_stock = raw_variant.get("is_in_stock")
        availability = str(raw_variant.get("availability_html") or "").lower()
        if "out of stock" in availability:
            in_stock = False

        variants.append({
            "product": name,
            "label": " / ".join(parts) or f"Variation {raw_variant.get('variation_id', '')}".strip(),
            "price": price,
            "sale_price": sale,
            "in_stock": bool(in_stock) if in_stock is not None else None,
            "currency": "",
            "url": _with_query(url, query) if query else
                   f"{_base_url(url)}#variation-{raw_variant.get('variation_id', '')}",
            "source": "WooCommerce variation data",
        })
    return _unique_urls(variants)


# ----------------------------------------------------------------------
# 3. schema.org ProductGroup / hasVariant
# ----------------------------------------------------------------------

def _jsonld_nodes(page):
    try:
        blocks = page.css('script[type="application/ld+json"]::text').getall()
    except Exception:  # noqa: BLE001
        return
    for block in blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def _types(node: dict) -> set[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(v) for v in values if v}


def _first_offer(node: dict) -> dict:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def _variant_label(parent: str, child: dict, varies_by: list[str]) -> str:
    name = str(child.get("name") or "").strip()
    if parent and name.lower().startswith(parent.lower()):
        rest = name[len(parent):].strip(" -–—:|/,")
        if rest:
            return rest
    # Fall back to the properties the group says its variants differ by.
    parts = []
    for prop in varies_by:
        key = prop.rsplit("/", 1)[-1]
        value = child.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if value:
            parts.append(str(value))
    if parts:
        return " / ".join(parts)
    return name or str(child.get("sku") or "")


def from_schema_org(page, url: str) -> list[dict]:
    for node in _jsonld_nodes(page):
        if "ProductGroup" not in _types(node):
            continue
        children = node.get("hasVariant") or []
        if not isinstance(children, list) or len(children) < 2:
            continue

        parent = str(node.get("name") or "").strip()
        varies_by = node.get("variesBy") or []
        varies_by = varies_by if isinstance(varies_by, list) else [varies_by]

        variants: list[dict] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            offer = _first_offer(child)
            paid = _to_float(offer.get("price"))
            link = offer.get("url") or child.get("url")
            sku = child.get("sku") or offer.get("sku")
            variants.append({
                "product": parent,
                "label": _variant_label(parent, child, varies_by),
                "price": paid,
                "sale_price": None,
                "in_stock": _stock_from_schema(offer.get("availability")),
                "currency": str(offer.get("priceCurrency") or ""),
                "url": str(link) if link else
                       f"{_base_url(url)}#{sku or len(variants) + 1}",
                "source": "structured variant data",
            })
        if len(variants) >= 2:
            return _unique_urls(variants)
    return []


# ----------------------------------------------------------------------
# 4. Beanbox-style script tables
# ----------------------------------------------------------------------
#
# Some shops build their size / grind picker from tables inside a script
# rather than any standard format. Beanbox's pages carry two:
#
#   key_pid   = {"12oz-single-whole": 609289, "2lb-monthly-ground": ..., ...}
#   pid_prods = {"609289": {"price": "24.45", "original_price": "24.45", ...}}
#
# Only one-time purchases ("single") are read: subscription prices are a
# different kind of purchase and would swamp the sheet with near-duplicates.
# The tables carry no per-size stock, so every size takes the stock status
# the page publishes for the product as a whole.

def _script_table(script: str, name: str):
    """Parse `var <name> = {...}` out of a script, however it is laid out."""
    match = re.search(rf"var\s+{name}\s*=\s*", script)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(script, match.end())
    except (json.JSONDecodeError, ValueError):
        return None
    return value


def from_beanbox(page, url: str) -> list[dict]:
    try:
        scripts = page.css("script::text").getall()
    except Exception:  # noqa: BLE001
        return []
    script = next((s for s in scripts if s and "key_pid" in s and "pid_prods" in s), None)
    if not script:
        return []

    key_pid = _script_table(script, "key_pid")
    products = _script_table(script, "pid_prods")
    if not isinstance(key_pid, dict) or not isinstance(products, dict):
        return []

    def option_labels(select_id: str) -> dict[str, str]:
        labels = {}
        try:
            for option in page.css(f"select#{select_id} option"):
                value = option.attrib.get("value")
                if value:
                    labels[value] = " ".join(option.get_all_text().split())
        except Exception:  # noqa: BLE001
            pass
        return labels

    sizes, formats = option_labels("size"), option_labels("format")

    # The product's own name, and the one stock status the page publishes.
    name, in_stock, currency = "", None, ""
    try:
        heading = page.css("h1")
        if heading:
            name = " ".join(heading[0].get_all_text().split())
    except Exception:  # noqa: BLE001
        pass
    for node in _jsonld_nodes(page):
        if "Product" in _types(node):
            offer = _first_offer(node)
            in_stock = _stock_from_schema(offer.get("availability"))
            currency = str(offer.get("priceCurrency") or "")
            published = str(node.get("name") or "")
            # The heading may be styled in capitals; the published name has
            # the real capitalisation but extra words after it. Take the
            # heading's words in the published name's capitalisation.
            if name and published.lower().startswith(name.lower()):
                name = published[:len(name)]
            name = name or published
            break

    found: list[tuple[str, str, dict]] = []
    for key, pid in key_pid.items():
        parts = str(key).split("-")
        if len(parts) == 3 and parts[1] == "single":
            found.append((parts[0], parts[2], products.get(str(pid)) or {}))

    # The shop's own order: sizes as its drop-down lists them, then grinds.
    def position(order: list[str], value: str) -> int:
        return order.index(value) if value in order else len(order)

    found.sort(key=lambda f: (position(list(sizes), f[0]), position(list(formats), f[1])))

    variants: list[dict] = []
    for size, grind, details in found:
        paid = _to_float(details.get("price"))
        regular = _to_float(details.get("original_price"))
        price, sale = _split_price(regular, paid)
        variants.append({
            "product": name,
            "label": f"{sizes.get(size, size)} / {formats.get(grind, grind)}",
            "price": price,
            "sale_price": sale,
            "in_stock": in_stock,
            "currency": currency,
            "url": f"{_base_url(url)}#{size}-{grind}",
            "source": "Beanbox size and grind data",
        })
    return _unique_urls(variants)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def read_variants(page, url: str, fetch_json: Callable[[str], Any] | None = None) -> list[dict]:
    """
    Every variant of the product on this page, or [] if it has none.

    fetch_json(url) is how the Shopify reader gets the store's product data
    file; it should return parsed JSON or None.
    """
    for reader in (
        lambda: from_shopify(page, url, fetch_json),
        lambda: from_woocommerce(page, url),
        lambda: from_schema_org(page, url),
        lambda: from_beanbox(page, url),
    ):
        try:
            found = reader()
        except Exception:  # noqa: BLE001 - one reader failing must not stop the others
            found = []
        if len(found) >= 2:
            return found
    return []
