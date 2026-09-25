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

import atexit
import json
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from scrapling.fetchers import Fetcher, StealthyFetcher, StealthySession

import config
import extractors

# Adaptive mode, on for every fetch this process makes - on both the fast
# path and the browser path. Each fetch also passes it explicitly through
# selector_config (see _selector_config), which is what the shared browser
# session actually reads.
StealthyFetcher.adaptive = True
Fetcher.adaptive = True


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


def read_targets(path=None) -> list[tuple[str, str, bool]]:
    """
    Read competitors.txt into (competitor_name, url, force_listing) triples.

    A line may be a single product page or a whole category page; which one
    it is gets worked out at scrape time. Prefixing a line with "list:"
    forces listing mode, for a category page that also publishes
    single-product data and would otherwise be read as one product.

    Both of these are still accepted:
        https://shop.example/product/thing
        My Shop | https://shop.example/product/thing
    """
    path = path or config.COMPETITORS_FILE
    targets: list[tuple[str, str, bool]] = []
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

        # The prefix may sit before the URL on either side of the "|".
        force_listing = False
        if url.lower().startswith("list:"):
            force_listing = True
            url = url[5:].strip()
        elif name.lower().startswith("list:"):
            force_listing = True
            name = name[5:].strip()

        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        targets.append((name or competitor_name_from_url(url), url, force_listing))

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

# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------
#
# Two ways to get a page, cheapest first:
#
#   1. A plain HTTP request that impersonates Chrome's network fingerprint.
#      Well under a second. Most shops put their product data in the HTML
#      they send, so this is usually all that is needed.
#   2. Scrapling's stealth browser - the original engine - for pages that
#      block plain requests or build their content with JavaScript.
#
# A fast result is only trusted when it holds real product data. A bot
# check or an empty JavaScript shell has none, so it goes to the browser
# instead of producing a misleading row.
#
# Adaptive mode is configured identically on both paths, so learned
# selectors keep working whichever one fetched the page.

_browser: StealthySession | None = None     # one browser, shared by the whole run
_last_hit: dict[str, float] = {}            # host -> when we last requested it


def _selector_config(url: str) -> dict:
    # Adaptive mode needs somewhere durable to keep what it learns. Scrapling's
    # default is a file inside its own site-packages folder, which
    # `pip install --upgrade scrapling` wipes - taking every learned selector
    # with it. Keep it in the project. `url` scopes saved fingerprints to the site.
    return {
        "adaptive": True,
        "storage_args": {"storage_file": str(config.ADAPTIVE_STORAGE), "url": url},
    }


def _browser_session() -> StealthySession:
    """Start the browser on first use and reuse it for every later page."""
    global _browser
    if _browser is None:
        options = dict(
            headless=config.HEADLESS,
            disable_resources=config.DISABLE_RESOURCES,
            google_search=True,
            block_webrtc=True,        # stop the real IP leaking past a proxy
        )
        proxy_settings = config.proxy()
        if proxy_settings:
            options["proxy"] = proxy_settings
            options["dns_over_https"] = True
        _browser = StealthySession(**options)
        _browser.start()
    return _browser


def close_browser() -> None:
    """Shut the shared browser down. Safe to call when none was started."""
    global _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None


atexit.register(close_browser)


def polite_wait(url: str) -> None:
    """
    Pause only if this same site was hit less than the delay ago.

    Time spent fetching and parsing counts toward the gap, and moving on to a
    different site needs no pause at all - the delay exists to be gentle with
    each shop, not to slow the run down for its own sake.
    """
    host = urlparse(url).netloc.lower()
    last = _last_hit.get(host)
    if last is not None:
        gap = config.REQUEST_DELAY_SECONDS + random.uniform(0, 1.5)
        remaining = gap - (time.monotonic() - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_hit[host] = time.monotonic()


def _fast_fetch(url: str):
    options = dict(
        impersonate="chrome",
        stealthy_headers=True,
        timeout=config.HTTP_TIMEOUT_SECONDS,
        retries=1,
        selector_config=_selector_config(url),
    )
    proxy_settings = config.proxy()
    if proxy_settings:
        # Server and credentials go separately so the password never sits
        # inside a URL.
        options["proxy"] = proxy_settings["server"]
        if proxy_settings.get("username"):
            options["proxy_auth"] = (proxy_settings["username"],
                                     proxy_settings.get("password", ""))
    return Fetcher.get(url, **options)


def _quiet(*_args, **_kwargs) -> None:
    pass


def _has_product_data(page, url: str, selector_rules: dict | None) -> bool:
    """True if the page holds something we can actually read a product from."""
    if _looks_like_single_product(page):
        return True

    site_rules = rules_for(url, selector_rules or {})
    if site_rules:
        try:
            facts = extractors.from_css_rules(page, site_rules, make_smart_css(_quiet)) or {}
        except Exception:  # noqa: BLE001
            facts = {}
        price = facts.get("current_price", facts.get("price"))
        if facts.get("name") and price is not None:
            return True

    try:
        return bool(extractors.extract_listing(page, url, site_rules or {}))
    except Exception:  # noqa: BLE001
        return False


def fetch_page(url: str, log=print, selector_rules: dict | None = None):
    """
    Fetch one page, as cheaply as will still give a trustworthy result.

    Returns the page. Raises if neither method could get one.
    """
    fast_page = None

    if config.FAST_MODE:
        try:
            page = _fast_fetch(url)
            status = getattr(page, "status", 0) or 0
            if 0 < status < 400:
                if _has_product_data(page, url, selector_rules):
                    log("      fetched with a fast request")
                    return page
                # Keep it in case the browser fails outright: a page with no
                # structured data can still be read by the weaker strategies.
                fast_page = page
                log("      fast request had no product data - using the browser")
            else:
                log(f"      fast request refused (code {status}) - using the browser")
        except Exception as exc:  # noqa: BLE001
            log(f"      fast request failed ({_short(exc)}) - using the browser")

    last_error: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 2):
        try:
            response = _browser_session().fetch(
                url,
                network_idle=config.NETWORK_IDLE,
                timeout=config.PAGE_TIMEOUT_MS,
                disable_resources=config.DISABLE_RESOURCES,
                solve_cloudflare=config.SOLVE_CLOUDFLARE,
                selector_config=_selector_config(url),
            )
            if response is None:
                raise RuntimeError("the fetcher returned nothing")

            status = getattr(response, "status", None)
            if status and status >= 400:
                raise RuntimeError(f"the site replied with error code {status}")

            log("      fetched with the browser")
            return response

        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
            last_error = exc
            # A browser that crashed or hung would poison every later page;
            # a fresh one costs a few seconds.
            close_browser()
            if attempt <= config.MAX_RETRIES:
                pause = min(30, 5 * attempt) + random.uniform(0, 3)
                log(f"      attempt {attempt} failed ({_short(exc)}), retrying in {pause:.0f}s")
                time.sleep(pause)

    if fast_page is not None:
        log("      browser failed - falling back to the fast request's page")
        return fast_page

    raise RuntimeError(_short(last_error))


def _short(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    text = " ".join(str(exc).split())
    return text[:200] if text else exc.__class__.__name__


# ----------------------------------------------------------------------
# Scraping one product
# ----------------------------------------------------------------------

def scrape(url: str, competitor: str, log=print, selector_rules: dict | None = None,
           page=None) -> Product:
    """
    Read one product page.

    `page` lets a caller pass a page it has already fetched. Listing
    detection needs to try a single-product read and then a listing read on
    the same page, and fetching it twice would double the load on the shop
    for no reason.
    """
    product = Product(url=url, competitor=competitor)
    smart_css = make_smart_css(log)

    if page is None:
        try:
            page = fetch_page(url, log=log, selector_rules=selector_rules)
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


def _looks_like_single_product(page) -> bool:
    """
    True when the page carries machine-readable data for exactly one product.

    Only the two trustworthy strategies are consulted. Shops publish these so
    Google Shopping can read a product, and a category page does not carry
    them for itself - which makes their presence a reliable way to tell one
    kind of page from the other, without guessing from layout.
    """
    for strategy in (extractors.from_jsonld, extractors.from_microdata):
        try:
            facts = strategy(page) or {}
        except Exception:  # noqa: BLE001
            continue
        # These strategies report the figure as "current_price"; "price" is
        # accepted too so this keeps working if the key is ever renamed.
        price = facts.get("current_price")
        if price is None:
            price = facts.get("price")
        if facts.get("name") and price is not None:
            return True
    return False


def scrape_listing(page, url: str, competitor: str, selector_rules: dict | None = None,
                   log=print) -> list[Product]:
    """
    Read a category page as many products.

    Each product is given its own product-page URL. The snapshot, the alerts
    and the sheet history are all keyed on url, so without this every
    product on the page would share one key and a price drop could not be
    traced back to the product it belongs to.
    """
    site_rules = rules_for(url, selector_rules or {}) or {}

    try:
        found = extractors.extract_listing(page, url, site_rules)
    except Exception as exc:  # noqa: BLE001
        log(f"      could not read this as a listing - {_short(exc)}")
        return []

    products: list[Product] = []
    for facts in found:
        item_url = facts.get("product_url") or url
        product = Product(url=item_url, competitor=competitor)
        product.name = facts.get("name") or ""
        product.price = facts.get("price")
        product.sale_price = facts.get("sale_price")
        product.in_stock = facts.get("in_stock")
        product.sources = ["listing page"]
        if product.price is None and not product.name:
            continue
        products.append(product)

    return products


def scrape_all(targets: list[tuple[str, str, bool]], log=print) -> list[Product]:
    """
    Scrape every target, pausing politely between them.

    A target may turn out to be a single product page or a category page
    holding many. The page is fetched once either way.
    """
    results: list[Product] = []
    total = len(targets)

    selector_rules = load_selector_rules()
    if selector_rules:
        log(f"  Loaded site rules for: {', '.join(sorted(selector_rules))}")

    for index, target in enumerate(targets, start=1):
        # Tolerate the older two-item form so nothing that calls this breaks.
        competitor, url = target[0], target[1]
        force_listing = target[2] if len(target) > 2 else False

        log(f"  [{index}/{total}] {competitor}: {url}"
            + ("  (listing)" if force_listing else ""))

        polite_wait(url)
        try:
            page = fetch_page(url, log=log, selector_rules=selector_rules)
        except Exception as exc:  # noqa: BLE001
            failed = Product(url=url, competitor=competitor, error=_short(exc))
            log(f"      could not read this page - {failed.error}")
            results.append(failed)
            continue

        found: list[Product] = []

        # Deciding single-product vs listing, in order of how much the
        # evidence is worth:
        #
        #   1. Strong structured data naming ONE product with a price. Shops
        #      publish this for Google, and a category page does not have it.
        #   2. Two or more product tiles - solid structural evidence of a grid.
        #   3. Everything else, including the guess-from-visible-text strategy.
        #
        # The order matters. The text strategy will happily read a category
        # page's own title and the first price it sees, producing a plausible
        # but meaningless row ("Shop - 69.00"). Letting it run before listing
        # detection would hide a perfectly readable grid behind one wrong row.
        if not force_listing and _looks_like_single_product(page):
            single = scrape(url, competitor, log=log,
                            selector_rules=selector_rules, page=page)
            if not single.error:
                found = [single]

        if not found:
            found = scrape_listing(page, url, competitor, selector_rules, log=log)
            if found:
                log(f"      listing page - found {len(found)} product(s)")

        # Neither definite: fall back to the full single-product read, weak
        # strategies included. Better a rough row than nothing.
        if not found and not force_listing:
            single = scrape(url, competitor, log=log,
                            selector_rules=selector_rules, page=page)
            if not single.error:
                found = [single]

        if not found:
            failed = Product(url=url, competitor=competitor,
                             error="could not find a product name or price on this page")
            log(f"      could not read this page - {failed.error}")
            results.append(failed)
        elif len(found) == 1:
            product = found[0]
            price_text = _money(product.price, product.currency)
            if product.sale_price is not None:
                price_text += f" (on sale at {_money(product.sale_price, product.currency)})"
            log(f"      {product.name[:60] or '(no name found)'} - {price_text} - in stock: {product.stock_text()}")
            if product.sources:
                log(f"      read from: {', '.join(product.sources)}")
            results.append(product)
        else:
            priced = sum(1 for p in found if p.price is not None)
            in_stock = sum(1 for p in found if p.in_stock is True)
            log(f"      {len(found)} product(s): {priced} priced, {in_stock} in stock")
            results.extend(found)

    close_browser()
    return results


def _money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "no price found"
    return f"{currency + ' ' if currency else ''}{value:,.2f}"
