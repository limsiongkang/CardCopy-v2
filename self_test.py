"""
Offline checks for the parts that decide what ends up in the sheet.

Run it any time, especially after editing extractors.py or the alert rules:

    python self_test.py

Nothing here touches the network, Google or your treg balance, so it is free
and takes about a second. It exists because the risky parts of this project
are not the scraping - they are the quiet judgement calls: is "1.500" fifteen
hundred or one and a half, is an executive assistant a founder, does a page
that timed out mean the product sold out.
"""

from __future__ import annotations

import sys

import extractors
import scraper

PASSED = 0
FAILED: list[str] = []


def check(label: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{label}\n      wanted {want!r}\n      got    {got!r}")


# ----------------------------------------------------------------------

def test_price_parsing() -> None:
    cases = [
        ("$51.77", 51.77),
        ("£1,234.56", 1234.56),
        ("1.234,56 EUR", 1234.56),
        ("1,50", 1.5),
        (" 99,99 €", 99.99),
        ("1.234.500", 1234500.0),
        ("Free", None),
        ("0.00", None),
        ("", None),
        (None, None),
        (69, 69.0),
        (True, None),          # a bool is not a price
    ]
    for raw, want in cases:
        check(f"parse_price({raw!r})", extractors.parse_price(raw), want)


def test_stock_from_schema() -> None:
    cases = [
        ("https://schema.org/InStock", True),
        ("http://schema.org/OutOfStock", False),
        ("https://schema.org/SoldOut", False),
        ("InStock", True),
        ("LimitedAvailability", True),
        ({"@id": "https://schema.org/OutOfStock"}, False),
        ("https://schema.org/Nonsense", None),
        (None, None),
    ]
    for raw, want in cases:
        check(f"stock_from_schema({raw!r})", extractors.stock_from_schema(raw), want)


def test_stock_from_text() -> None:
    cases = [
        ("In stock (22 available)", True),
        ("Add to cart", True),
        ("Out of stock", False),
        ("Sold out", False),
        ("Currently unavailable", False),
        # Out-of-stock must win: a page can show both an "add to cart" button
        # and an out-of-stock notice, and shipping the wrong answer here
        # produces a false alert.
        ("Add to cart - currently unavailable", False),
        ("Some unrelated words", None),
        ("", None),
    ]
    for raw, want in cases:
        check(f"stock_from_text({raw!r})", extractors.stock_from_text(raw), want)


def test_competitor_naming() -> None:
    cases = [
        ("https://www.example.com/p/1", "example.com"),
        ("https://shop.example.co.uk/x", "shop.example.co.uk"),
        ("https://books.toscrape.com/catalogue/x.html", "books.toscrape.com"),
    ]
    for url, want in cases:
        check(f"competitor_name_from_url({url!r})",
              scraper.competitor_name_from_url(url), want)


def test_target_parsing(tmp_lines: list[str], want: list[tuple[str, str]]) -> None:
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "_monitor_selftest_targets.txt"
    path.write_text("\n".join(tmp_lines), encoding="utf-8")
    try:
        check("read_targets", scraper.read_targets(path), want)
    finally:
        path.unlink(missing_ok=True)


def test_listing_extraction() -> None:
    """Category pages: many products from one page, each with its own link."""
    from scrapling import Selector

    import extractors

    html = """
    <html><body><ul class="products">
      <li class="product instock purchasable">
        <a href="/shop/alpha/"><img src="a.jpg"></a>
        <h2 class="woocommerce-loop-product__title">Alpha Shirt</h2>
        <a href="/shop/alpha/">Alpha Shirt</a>
        <span class="price"><span class="amount">$<br>25.00</span></span>
      </li>
      <li class="product outofstock">
        <a href="/shop/beta/"><img src="b.jpg"></a>
        <h2 class="woocommerce-loop-product__title">Beta Jacket</h2>
        <span class="price">
          <del><span class="amount">$80.00</span></del>
          <ins><span class="amount">$59.99</span></ins>
        </span>
      </li>
      <li class="product instock">
        <a href="https://other.example/shop/gamma/"></a>
        <h3 class="product-title">Gamma Cap</h3>
        <span class="price">$12,50</span>
      </li>
    </ul></body></html>
    """

    items = extractors.extract_listing(Selector(html), "https://shop.example/category/")
    check("listing finds every tile", len(items), 3)

    by_name = {i["name"]: i for i in items}

    check("price read through nested markup", by_name["Alpha Shirt"]["price"], 25.0)
    check("stock read from container class", by_name["Alpha Shirt"]["in_stock"], True)
    check("relative link made absolute",
          by_name["Alpha Shirt"]["product_url"], "https://shop.example/shop/alpha/")

    check("regular price from <del>", by_name["Beta Jacket"]["price"], 80.0)
    check("sale price from <ins>", by_name["Beta Jacket"]["sale_price"], 59.99)
    check("out of stock from class", by_name["Beta Jacket"]["in_stock"], False)

    check("european decimal comma", by_name["Gamma Cap"]["price"], 12.50)
    check("absolute link left alone",
          by_name["Gamma Cap"]["product_url"], "https://other.example/shop/gamma/")

    # Every row needs its own identity: the snapshot, the alerts and the
    # sheet history are all keyed on url, so duplicates would make a price
    # drop impossible to attribute to the right product.
    links = [i["product_url"] for i in items]
    check("every product has a distinct link", len(set(links)), len(links))

    # A single product page must not be mistaken for a one-item listing.
    single = """<html><body><div itemtype="http://schema.org/Product">
        <h1 itemprop="name">Lonely Widget</h1>
        <span class="price">$9.99</span></div></body></html>"""
    check("single product page is not a listing",
          extractors.extract_listing(Selector(single), "https://shop.example/p/"), [])

    # Tiles with no name are skipped rather than written as blank rows.
    nameless = """<html><body><ul class="products">
        <li class="product"><span class="price">$1.00</span></li>
        <li class="product"><span class="price">$2.00</span></li>
        </ul></body></html>"""
    check("nameless tiles skipped",
          len(extractors.extract_listing(Selector(nameless), "https://shop.example/c/")), 0)


def test_page_kind_detection() -> None:
    """A page with real product data must not be read as a listing."""
    from scrapling import Selector

    import scraper

    with_jsonld = Selector("""<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Real Product",
         "offers":{"price":"19.99","availability":"https://schema.org/InStock"}}
        </script></head><body>
        <li class="product"><h2>Related A</h2><span class="price">$5</span></li>
        <li class="product"><h2>Related B</h2><span class="price">$6</span></li>
        </body></html>""")
    check("product page with related items reads as single",
          scraper._looks_like_single_product(with_jsonld), True)

    grid = Selector("""<html><body><ul class="products">
        <li class="product"><h2>One</h2><span class="price">$5</span></li>
        <li class="product"><h2>Two</h2><span class="price">$6</span></li>
        </ul></body></html>""")
    check("bare category grid does not read as single",
          scraper._looks_like_single_product(grid), False)


def test_alert_rules() -> None:
    import monitor
    from scraper import Product

    def person(url, price=None, sale=None, stock=None, err=""):
        return Product(url=url, competitor="c", name=url, price=price,
                       sale_price=sale, in_stock=stock, error=err)

    previous = {
        "u/drop":    {"product": "x", "price": 100.0, "sale_price": None, "in_stock": "yes"},
        "u/rise":    {"product": "x", "price": 100.0, "sale_price": None, "in_stock": "yes"},
        "u/oos":     {"product": "x", "price": 50.0,  "sale_price": None, "in_stock": "yes"},
        "u/failed":  {"product": "x", "price": 50.0,  "sale_price": None, "in_stock": "yes"},
        "u/sale":    {"product": "x", "price": 100.0, "sale_price": 90.0, "in_stock": "yes"},
        "u/penny":   {"product": "x", "price": 100.0, "sale_price": None, "in_stock": "yes"},
        "u/unknown": {"product": "x", "price": 50.0,  "sale_price": None, "in_stock": "yes"},
    }
    now = [
        person("u/drop",    price=80.0,  stock=True),
        person("u/rise",    price=120.0, stock=True),
        person("u/oos",     price=50.0,  stock=False),
        person("u/failed",  err="timed out"),
        person("u/sale",    price=100.0, sale=70.0, stock=True),
        person("u/penny",   price=99.995, stock=True),
        person("u/unknown", price=50.0,  stock=None),
        person("u/new",     price=10.0,  stock=True),
    ]

    alerts = monitor.find_alerts(now, previous)
    got = sorted((a["product"], a["alert"]) for a in alerts)
    want = sorted([
        ("u/drop", "price drop"),
        ("u/sale", "price drop"),
        ("u/oos", "out of stock"),
    ])
    check("alert rules", got, want)

    # The one that matters most: a page that failed to load must NEVER be
    # reported as out of stock. A timeout is not evidence about inventory,
    # and a false alert here trains you to ignore the real ones.
    check("a failed scrape raises no alert",
          [a for a in alerts if a["product"] == "u/failed"], [])


def test_sale_price_semantics() -> None:
    import sheets
    from scraper import Product

    p = Product(url="https://shop.example.com/p/1", competitor="shop.example.com",
                name="Thing", price=100.0, sale_price=70.0, in_stock=True)
    row = sheets.product_row(p, "2026-09-24 7:00")
    check("price column holds the normal price", row[3], 100.0)
    check("sale column holds the discounted price", row[4], 70.0)
    check("stock is written lower case", row[5], "yes")
    check("competitor cell is a link", row[1].startswith("=HYPERLINK("), True)
    # The URL column is the plain address, NOT a formula: Sheets auto-links a
    # bare URL, and a real URL in the cell copies and exports cleanly.
    check("URL cell is the plain address",
          row[6], "https://shop.example.com/p/1")
    check("competitor links to the home page",
          '"https://shop.example.com"' in row[1], True)

    # A URL with no host has nothing to link to - the cell must degrade to
    # plain text rather than emitting a broken formula.
    bare = Product(url="not-a-url", competitor="c", name="Thing", price=1.0)
    check("a hostless URL degrades to plain text",
          sheets.product_row(bare, "2026-09-24 7:00")[1], "c")


def test_selector_rules_loading() -> None:
    rules = scraper.load_selector_rules()
    check("selectors.json parses", isinstance(rules, dict), True)
    check("comment keys are ignored", any(k.startswith("_") for k in rules), False)
    check("books.toscrape.com rule is found",
          scraper.rules_for("https://books.toscrape.com/catalogue/x.html", rules)
          is not None, True)
    check("www. is stripped when matching",
          scraper.rules_for("https://www.books.toscrape.com/x", rules) is not None, True)
    check("an unknown site has no rule",
          scraper.rules_for("https://nobody-knows-this.com/x", rules), None)


# ----------------------------------------------------------------------

def main() -> int:
    test_price_parsing()
    test_stock_from_schema()
    test_stock_from_text()
    test_competitor_naming()
    test_target_parsing(
        [
            "# a comment",
            "https://a.com/p",
            "Named Shop | https://b.com/p",
            "",
            "https://a.com/p",        # duplicate, should be dropped
            "not-a-url",
            "ftp://c.com/p",          # wrong scheme
            "list:https://d.com/shop",            # forced listing
            "Grid Shop | list:https://e.com/all",  # forced listing, named
        ],
        [
            ("a.com", "https://a.com/p", False),
            ("Named Shop", "https://b.com/p", False),
            ("d.com", "https://d.com/shop", True),
            ("Grid Shop", "https://e.com/all", True),
        ],
    )
    test_listing_extraction()
    test_page_kind_detection()
    test_alert_rules()
    test_sale_price_semantics()
    test_selector_rules_loading()

    print()
    if FAILED:
        print(f"  {len(FAILED)} FAILED, {PASSED} passed")
        print()
        for failure in FAILED:
            print(f"   x  {failure}")
        print()
        return 1

    print(f"  All {PASSED} checks passed.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
