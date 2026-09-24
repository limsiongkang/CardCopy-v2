"""
Fetching competitor pages with Scrapling's StealthyFetcher.

Two things worth knowing about "adaptive mode":

  * It is switched on here with `StealthyFetcher.adaptive = True`.
  * What it actually does is *remember* an element that a selector matched
    successfully, so that when the shop redesigns its page and the old
    selector stops matching, Scrapling can find the same element again by
    its saved characteristics.

That means it protects against redesigns from the second run onwards. It
cannot find a price on a site it has never seen - that is what the
structured-data strategies in extractors.py are for.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from scrapling.fetchers import StealthyFetcher

import config
import extractors

# Adaptive mode, on for every fetch this process makes.
StealthyFetcher.adaptive = True


@dataclass
class Product:
    """One row of the report."""

    url: str
    competitor: str
    name: str = ""
    price: float | None = None
    sale_price: float | None = None
    currency: str = ""
    in_stock: bool | None = None
    error: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and (self.price is not None or bool(self.name))

    @property
    def effective_price(self) -> float | None:
        """What a shopper actually pays today."""
        return self.sale_price if self.sale_price is not None else self.price

    def stock_text(self) -> str:
        if self.in_stock is None:
            return "Unknown"
        return "Yes" if self.in_stock else "No"


# ----------------------------------------------------------------------
# competitors.txt
# ----------------------------------------------------------------------

def load_selector_rules() -> dict:
    """Read selectors.json. A missing or broken file is not fatal."""
    path = config.SELECTORS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if not k.startswith("_") and isinstance(v, dict)}


def rules_for(url: str, all_rules: dict) -> dict | None:
    """The selectors.json entry for this URL's domain, if there is one."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return all_rules.get(host)


def competitor_name_from_url(url: str) -> str:
    """
    www.example.com/x -> example.com

    The hostname, not a prettified brand name. The existing sheet history
    identifies competitors this way, and it stays unambiguous when two brands
    share a word. Override it per line in competitors.txt with "Name | URL".
    """
    host = (urlparse(url).hostname or url).lower()
    if host.startswith("www."):
        host = host[4:]
    return host or url


def read_targets(path=None) -> list[tuple[str, str]]:
    """Read competitors.txt into (competitor_name, url) pairs."""
    path = path or config.COMPETITORS_FILE
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    if not path.exists():
        return targets

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "|" in line:
            name, _, url = line.partition("|")
            name, url = name.strip(), url.strip()
        else:
            url = line
            name = ""

        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        targets.append((name or competitor_name_from_url(url), url))

    return targets


# ----------------------------------------------------------------------
# Adaptive selection helper
# ----------------------------------------------------------------------

def make_smart_css(log=print):
    """
    Returns a css helper that uses adaptive mode.

    Plain selector first. If it finds nothing - which is what happens after
    a site redesign - ask Scrapling to re-find the element it remembered from
    a previous run. When the plain selector does work, save it so adaptive
    mode has something to fall back on next time.
    """

    def smart_css(page, selector: str, key: str = ""):
        try:
            found = page.css(selector, auto_save=True)
            if found:
                return found[0]
        except Exception:
            pass

        try:
            recovered = page.css(selector, adaptive=True)
            if recovered:
                log(f"      adaptive mode recovered '{selector}' after a layout change")
                return recovered[0]
        except Exception:
            pass

        return None

    return smart_css


# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------

def fetch_page(url: str, log=print):
    """Fetch one page through the stealth browser, retrying on failure."""
    last_error: Exception | None = None

    for attempt in range(1, config.MAX_RETRIES + 2):
        try:
            response = StealthyFetcher.fetch(
                url,
                headless=config.HEADLESS,
                network_idle=True,
                google_search=True,
                solve_cloudflare=config.SOLVE_CLOUDFLARE,
                disable_resources=config.DISABLE_RESOURCES,
                timeout=config.PAGE_TIMEOUT_MS,
                proxy=config.proxy(),
                # Adaptive mode needs somewhere durable to keep what it learns.
                # Scrapling's default is a file inside its own site-packages
                # folder, which `pip install --upgrade scrapling` wipes - taking
                # every learned selector with it. Keep it in the project.
                # `url` is what scopes saved fingerprints to this site.
                selector_config={
                    "adaptive": True,
                    "storage_args": {
                        "storage_file": str(config.ADAPTIVE_STORAGE),
                        "url": url,
                    },
                },
            )
            if response is None:
                raise RuntimeError("the fetcher returned nothing")

            status = getattr(response, "status", None)
            if status and status >= 400:
                raise RuntimeError(f"the site replied with error code {status}")

            return response

        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
            last_error = exc
            if attempt <= config.MAX_RETRIES:
                pause = min(30, 5 * attempt) + random.uniform(0, 3)
                log(f"      attempt {attempt} failed ({_short(exc)}), retrying in {pause:.0f}s")
                time.sleep(pause)

    raise RuntimeError(_short(last_error))


def _short(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    text = " ".join(str(exc).split())
    return text[:200] if text else exc.__class__.__name__


# ----------------------------------------------------------------------
# Scraping one product
# ----------------------------------------------------------------------

def scrape(url: str, competitor: str, log=print, selector_rules: dict | None = None) -> Product:
    product = Product(url=url, competitor=competitor)
    smart_css = make_smart_css(log)

    try:
        page = fetch_page(url, log=log)
    except Exception as exc:  # noqa: BLE001
        product.error = _short(exc)
        return product

    facts: dict = {}
    site_rules = rules_for(url, selector_rules or {})

    # Hand-written rules run FIRST. One only exists because somebody wrote it
    # for this shop, which usually means the automatic strategies read it
    # wrongly - so the rule has to be able to win, not just fill in gaps.
    for label, strategy in (
        ("site rules", lambda: extractors.from_css_rules(page, site_rules, smart_css)),
        ("structured data", lambda: extractors.from_jsonld(page)),
        ("microdata", lambda: extractors.from_microdata(page)),
        ("meta tags", lambda: extractors.from_meta(page)),
        ("page text", lambda: extractors.from_visible_text(page, smart_css)),
    ):
        try:
            result = strategy()
        except Exception as exc:  # noqa: BLE001
            log(f"      {label} strategy failed: {_short(exc)}")
            continue

        added = False
        for field_name, value in result.items():
            if facts.get(field_name) in (None, "") and value not in (None, ""):
                facts[field_name] = value
                added = True
        if added:
            product.sources.append(label)

        if facts.get("name") and facts.get("current_price") is not None and facts.get("in_stock") is not None:
            break

    current_price = facts.get("current_price")
    explicit_sale = facts.get("explicit_sale_price")

    if explicit_sale is not None:
        # A site rule named both prices outright, so no guessing is needed.
        product.price = current_price
        product.sale_price = explicit_sale
    else:
        # Structured data reports the price you pay today. If a crossed-out
        # higher price is on the page, today's price is a sale price.
        was_price = None
        try:
            was_price = extractors.find_was_price(page, current_price)
        except Exception:  # noqa: BLE001
            pass

        if was_price:
            product.price = was_price
            product.sale_price = current_price
        else:
            product.price = current_price
            product.sale_price = None

    product.name = facts.get("name", "") or ""
    product.currency = facts.get("currency", "") or ""
    product.in_stock = facts.get("in_stock")

    if product.price is None and not product.name:
        product.error = "could not find a product name or price on this page"

    return product


def scrape_all(targets: list[tuple[str, str]], log=print) -> list[Product]:
    """Scrape every target, pausing politely between them."""
    results: list[Product] = []
    total = len(targets)

    selector_rules = load_selector_rules()
    if selector_rules:
        log(f"  Loaded site rules for: {', '.join(sorted(selector_rules))}")

    for index, (competitor, url) in enumerate(targets, start=1):
        log(f"  [{index}/{total}] {competitor}: {url}")
        product = scrape(url, competitor, log=log, selector_rules=selector_rules)

        if product.error:
            log(f"      could not read this page - {product.error}")
        else:
            price_text = _money(product.price, product.currency)
            if product.sale_price is not None:
                price_text += f" (on sale at {_money(product.sale_price, product.currency)})"
            log(f"      {product.name[:60] or '(no name found)'} - {price_text} - in stock: {product.stock_text()}")
            if product.sources:
                log(f"      read from: {', '.join(product.sources)}")

        results.append(product)

        if index < total:
            pause = config.REQUEST_DELAY_SECONDS + random.uniform(0, 2)
            time.sleep(pause)

    return results


def _money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "no price found"
    return f"{currency + ' ' if currency else ''}{value:,.2f}"
