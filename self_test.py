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

import config
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


def test_variant_readers() -> None:
    """Every variant's price and stock, read from the data shops embed."""
    import json as _json
    from scrapling import Selector

    import variants

    # --- Shopify: the store's /products/<handle>.js file ---------------
    shopify_page = Selector('<html><head><script src="//cdn.shopify.com/s/x.js"></script>'
                            '</head><body></body></html>')
    product_js = {
        "title": "House Blend",
        "variants": [
            {"id": 11, "title": "12 OZ / Whole Bean", "price": 1699, "compare_at_price": None, "available": True},
            {"id": 12, "title": "12 OZ / Ground", "price": 1499, "compare_at_price": 1699, "available": True},
            {"id": 13, "title": "5 LB / Whole Bean", "price": 6999, "compare_at_price": None, "available": False},
        ],
    }
    asked = []

    def fake_fetch(url):
        asked.append(url)
        return product_js

    found = variants.read_variants(
        shopify_page, "https://shop.example/collections/coffee/products/house-blend?x=1", fake_fetch)
    check("shopify reads every variant", len(found), 3)
    check("shopify asks for the product data file",
          asked[:1], ["https://shop.example/products/house-blend.js"])
    check("shopify cents become money", found[0]["price"], 16.99)
    check("shopify compare-at makes a sale", (found[1]["price"], found[1]["sale_price"]), (16.99, 14.99))
    check("shopify unavailable variant is out of stock", found[2]["in_stock"], False)
    check("shopify variant link opens that variant",
          found[2]["url"], "https://shop.example/products/house-blend?variant=13")
    check("shopify parent name kept", found[0]["product"], "House Blend")

    single = {"title": "Mug", "variants": [{"id": 1, "title": "Default Title", "price": 900, "available": True}]}
    check("one-variant shopify product is not a variant list",
          variants.read_variants(shopify_page, "https://shop.example/products/mug", lambda u: single), [])

    not_shopify = Selector("<html><body>/products/ in a path proves nothing</body></html>")
    check("non-shopify page never asks for the data file",
          variants.from_shopify(not_shopify, "https://shop.example/products/x", fake_fetch), [])

    # --- WooCommerce: data-product_variations -----------------------------
    variations = [
        {"variation_id": 1, "attributes": {"attribute_size": "l", "attribute_pa_colour": "blue"},
         "display_price": 69, "display_regular_price": 69, "is_in_stock": True},
        {"variation_id": 2, "attributes": {"attribute_size": "m", "attribute_pa_colour": "blue"},
         "display_price": 49, "display_regular_price": 69, "is_in_stock": True},
        {"variation_id": 3, "attributes": {"attribute_size": "s", "attribute_pa_colour": ""},
         "display_price": 69, "display_regular_price": 69, "is_in_stock": False},
    ]
    woo_html = (
        "<html><body><h1 class='product_title'>Hoodie</h1>"
        "<form class='variations_form' data-product_variations='"
        + _json.dumps(variations).replace("'", "&#39;") + "'>"
        "<select name='attribute_size'><option value=''>Choose</option>"
        "<option value='l'>Large</option><option value='m'>Medium</option>"
        "<option value='s'>Small</option></select>"
        "<select name='attribute_pa_colour'><option value='blue'>Ocean Blue</option></select>"
        "</form></body></html>"
    )
    found = variants.read_variants(Selector(woo_html), "https://shop.example/product/hoodie/")
    check("woocommerce reads every variation", len(found), 3)
    check("woocommerce shows the shopper's labels, not slugs", found[0]["label"], "Large / Ocean Blue")
    check("woocommerce reduced variation is a sale", (found[1]["price"], found[1]["sale_price"]), (69.0, 49.0))
    check("woocommerce 'any' option is spelled out", found[2]["label"], "Small / Any colour")
    check("woocommerce out-of-stock variation", found[2]["in_stock"], False)
    check("woocommerce link preselects the variation",
          found[0]["url"], "https://shop.example/product/hoodie/?attribute_size=l&attribute_pa_colour=blue")
    check("woocommerce variant links are unique", len({v["url"] for v in found}), 3)

    too_many = Selector("<html><body><form class='variations_form' "
                        "data-product_variations='false'></form></body></html>")
    check("woocommerce too-many-to-embed gives no variants",
          variants.read_variants(too_many, "https://shop.example/p/"), [])

    # --- schema.org ProductGroup ----------------------------------------
    group = {"@type": "ProductGroup", "name": "Tee", "variesBy": ["https://schema.org/size"],
             "hasVariant": [
                 {"@type": "Product", "name": "Tee - Small", "size": "S",
                  "offers": {"price": "20.00", "priceCurrency": "EUR",
                             "availability": "https://schema.org/InStock",
                             "url": "https://shop.example/tee?v=s"}},
                 {"@type": "Product", "name": "Tee - Large", "size": "L",
                  "offers": {"price": "22.00", "priceCurrency": "EUR",
                             "availability": "https://schema.org/OutOfStock"}},
             ]}
    page = Selector('<html><head><script type="application/ld+json">'
                    + _json.dumps(group) + "</script></head></html>")
    found = variants.read_variants(page, "https://shop.example/tee")
    check("schema.org reads every variant", len(found), 2)
    check("schema.org label drops the parent name", found[0]["label"], "Small")
    check("schema.org per-variant stock", (found[0]["in_stock"], found[1]["in_stock"]), (True, False))
    check("schema.org uses the offer's own link", found[0]["url"], "https://shop.example/tee?v=s")
    check("schema.org gives the other variant a distinct link",
          found[1]["url"] != found[0]["url"], True)


def test_variant_rows_and_changes() -> None:
    """Variants become rows, alerts and email lines correctly."""
    import monitor
    import notify
    import sheets
    from scraper import Product

    col = {name: index for index, name in enumerate(config.DATA_HEADERS)}
    p = Product(url="https://shop.example/p?variant=1", competitor="shop.example",
                name="Coffee", price=16.99, in_stock=True, variant="12 OZ / Whole Bean",
                page_url="https://shop.example/p")
    row = sheets.product_row(p, "2026-09-26 7:00")
    # The leading apostrophe marks a plain-text cell; Sheets does not show it.
    check("variant has its own column", row[col["variant"]], "'12 OZ / Whole Bean")
    check("product column is the product, not the variant", row[col["product"]], "'Coffee")

    # Scraped text must never be interpreted by Sheets.
    hostile = Product(url="https://evil.example/p", competitor="evil.example",
                      name='=IMPORTXML("https://evil.example","//a")', variant="3/4", price=1.0)
    hostile_row = sheets.product_row(hostile, "2026-09-26 7:00")
    check("a product name starting with = cannot become a formula",
          hostile_row[col["product"]].startswith("'="), True)
    check("a variant like 3/4 cannot become a date", hostile_row[col["variant"]], "'3/4")
    check("an empty variant stays empty", sheets._text(""), "")

    # Merging: one block per product page, only blocks of 2+ rows are merged.
    group = [Product(url=f"u?v={n}", competitor="c", page_url="u") for n in range(3)] \
        + [Product(url="solo", competitor="c", page_url="solo")] \
        + [Product(url=f"w?v={n}", competitor="c", page_url="w") for n in range(2)]
    blocks = sheets._product_blocks(group, first_row=10)
    check("product blocks", blocks, [(10, 13), (13, 14), (14, 16)])
    merges = sheets._merge_requests(99, 2, blocks)
    check("single-row products are not merged", len(merges), 2)
    check("merge covers the product column only",
          [(m["mergeCells"]["range"]["startColumnIndex"], m["mergeCells"]["range"]["endColumnIndex"])
           for m in merges], [(2, 3), (2, 3)])

    # Alerts: per variant, variant carried separately.
    snapshot = {
        "u?v=1": {"product": "Coffee", "variant": "Small", "price": 20.0, "sale_price": None, "in_stock": "yes"},
        "u?v=2": {"product": "Coffee", "variant": "Large", "price": 30.0, "sale_price": None, "in_stock": "yes"},
    }
    now = [
        Product(url="u?v=1", competitor="c", name="Coffee", variant="Small", price=18.0, in_stock=True),
        Product(url="u?v=2", competitor="c", name="Coffee", variant="Large", price=30.0, in_stock=False),
    ]
    alerts = monitor.find_alerts(now, snapshot)
    check("variant price drop and stock-out both caught",
          sorted((a["variant"], a["alert"]) for a in alerts),
          [("Large", "out of stock"), ("Small", "price drop")])
    check("alert product cell is the product", {a["product"] for a in alerts}, {"Coffee"})

    # Email: identical changes across variants collapse into one line.
    same = [{"competitor": "c", "product": "Coffee", "variant": v, "alert": "out of stock",
             "was": "in stock", "now": "out of stock", "url": "u"} for v in ("S", "M", "L", "XL")]
    merged = notify.collapse(same)
    check("four variants selling out is one email line", len(merged), 1)
    check("collapsed line names the count", merged[0]["label"].startswith("Coffee - 4 variants"), True)

    # Latest tab "change" wording.
    prev = {"price": 20.0, "sale_price": None, "in_stock": "yes"}
    snap = {"x": prev}
    check("price down is described",
          sheets.change_text(Product(url="x", competitor="c", price=18.0, in_stock=True), prev, snap),
          "price down 2.00 (was 20.00)")
    check("stock-out is described",
          sheets.change_text(Product(url="x", competitor="c", price=20.0, in_stock=False), prev, snap),
          "went out of stock")
    check("unchanged row says nothing",
          sheets.change_text(Product(url="x", competitor="c", price=20.0, in_stock=True), prev, snap), "")
    check("first ever run says nothing",
          sheets.change_text(Product(url="x", competitor="c", price=20.0), None, {}), "")
    check("unseen product is new",
          sheets.change_text(Product(url="y", competitor="c", price=5.0), None, snap), "new")
    check("variant of a product known before variants is not 'new'",
          sheets.change_text(Product(url="z?variant=1", competitor="c", price=5.0, page_url="z"),
                             None, {"z": prev}), "")


def test_variants_opt_in() -> None:
    """Variant rows only when the line asks for them with "variants:"."""
    import json as _json
    from scrapling import Selector

    import scraper
    import sheets
    from scraper import Product

    variations = [
        {"variation_id": n, "attributes": {"attribute_size": size},
         "display_price": 20, "display_regular_price": 20, "is_in_stock": True}
        for n, size in ((1, "s"), (2, "m"), (3, "l"))
    ]
    product_ld = {"@type": "Product", "name": "Tee",
                  "offers": {"price": "20.00", "availability": "https://schema.org/InStock"}}
    html = ("<html><head><script type='application/ld+json'>" + _json.dumps(product_ld)
            + "</script></head><body><h1 class='product_title'>Tee</h1>"
            "<form class='variations_form' data-product_variations='"
            + _json.dumps(variations) + "'></form></body></html>")
    url = "https://shop.example/product/tee/"

    normal = scraper.scrape_product(url, "shop", log=lambda *a, **k: None,
                                    page=Selector(html), want_variants=False)
    expanded = scraper.scrape_product(url, "shop", log=lambda *a, **k: None,
                                      page=Selector(html), want_variants=True)
    check("without variants: one row", (len(normal), normal[0].variant), (1, ""))
    check("with variants: one row per variant", len(expanded), 3)
    check("default is normal scraping",
          len(scraper.scrape_product(url, "shop", log=lambda *a, **k: None, page=Selector(html))), 1)

    # Switching a URL between modes must not make its rows look "new".
    prev = {"price": 20.0, "sale_price": None, "in_stock": "yes"}
    as_variants = {url + "?attribute_size=s": prev, url + "?attribute_size=m": prev}
    check("variants -> normal is not 'new'",
          sheets.change_text(Product(url=url, competitor="c", price=20.0, page_url=url),
                             None, as_variants), "")
    check("normal -> variants is not 'new'",
          sheets.change_text(Product(url=url + "?attribute_size=l", competitor="c",
                                     price=20.0, page_url=url), None, {url: prev}), "")


def test_one_look() -> None:
    """Every tab formatted from the same rules, and every change coloured."""
    import sheets

    every_column = {h.lower() for h in
                    config.DATA_HEADERS + config.ALERT_HEADERS + config.LATEST_HEADERS}
    check("every column on every tab has a set width",
          sorted(every_column - set(sheets._WIDTH)), [])

    change_rules = sheets._STATUS_RULES["change since last run"]
    colour_of = {text: colour for _, text, colour in change_rules}
    check("a price rise is green", colour_of.get("price up"), "green")
    check("a price drop is red", colour_of.get("price down"), "red")
    signed = {condition: colour for condition, _, colour in sheets._STATUS_RULES["change"]}
    check("alert change column: + green, - red", signed, {"GT": "green", "LT": "red"})

    order = [text for _, text, _ in change_rules]
    check("sold out outranks a price change in the same cell",
          order.index("went out of stock") < min(order.index("price up"), order.index("price down")), True)

    # The same meaning is the same colour on every tab.
    stock = {text: colour for _, text, colour in sheets._STATUS_RULES["in stock"]}
    alert = {text: colour for _, text, colour in sheets._STATUS_RULES["alert"]}
    check("out of stock is red everywhere",
          {stock["no"], alert["out of stock"], colour_of["went out of stock"]}, {"red"})
    check("available is green everywhere",
          {stock["yes"], alert["back in stock"], colour_of["back in stock"]}, {"green"})
    check("price drop is red everywhere", {alert["price drop"], colour_of["price down"]}, {"red"})
    check("price rise is green everywhere", {alert["price rise"], colour_of["price up"]}, {"green"})
    check("every colour in the rules has a definition and a key entry",
          {c for rules in sheets._STATUS_RULES.values() for _, _, c in rules}
          <= set(sheets._COLOURS) == {c for c, _ in sheets._LEGEND}, True)


def test_adaptive_canary() -> None:
    """Adaptive recovery only after a real redesign, and never for a sale price."""
    from scrapling import Selector

    import extractors

    rules = {"name": "h1.title", "price": ".price", "sale_price": ".sale", "in_stock": ".stock"}
    calls: dict[str, bool] = {}

    def recorder(page, selector, key="", adaptive=True):
        calls[key] = adaptive
        found = page.css(selector)
        return found[0] if found else None

    # Same layout, no sale on: nothing may be "recovered".
    unchanged = Selector("<html><body><h1 class='title'>Mug</h1>"
                         "<span class='price'>$9.50</span></body></html>")
    facts = extractors.from_css_rules(unchanged, rules, recorder)
    check("unchanged page: price read normally", facts.get("current_price"), 9.5)
    check("unchanged page: missing stock is not guessed", calls["in_stock"], False)
    check("unchanged page: missing sale is not guessed", calls["sale_price"], False)

    # Redesigned: the name rule fails too, so recovery is allowed - except
    # for the sale price, which is normally absent.
    calls.clear()
    redesigned = Selector("<html><body><h1 class='p-title'>Mug</h1>"
                          "<span class='p-now'>$9.50</span></body></html>")
    extractors.from_css_rules(redesigned, rules, recorder)
    check("redesigned page: price may be recovered", calls["price"], True)
    check("redesigned page: stock may be recovered", calls["in_stock"], True)
    check("redesigned page: sale may be recovered too", calls["sale_price"], True)

    # A guessed sale only counts beside the price it discounts.
    side_by_side = Selector("<html><body><main><h1>Bottle</h1><div class='cost'>"
                            "<span class='old'>$15.00</span><span class='new'>$12.00</span>"
                            "</div></main></body></html>")
    table = Selector("<html><body><main><h1>Coffee</h1><table>"
                     "<tr><td>250g</td><td class='a'>$12.00</td></tr>"
                     "<tr><td>1kg</td><td class='b'>$40.00</td></tr></table></main></body></html>")
    guess_rules = {"name": "h1.gone", "price": ".old, .b", "sale_price": ".new, .a"}
    check("guessed sale beside its price is kept",
          extractors.from_css_rules(side_by_side, guess_rules, recorder).get("explicit_sale_price"), 12.0)
    check("guessed 'sale' from another size's row is rejected",
          extractors.from_css_rules(table, guess_rules, recorder).get("explicit_sale_price"), None)

    # A guessed rule must not overrule the price the shop publishes.
    import json as _json
    import scraper

    real = extractors.from_css_rules
    extractors.from_css_rules = lambda page, rules, smart_css: {
        "name": "Bottle", "current_price": 15.0, "explicit_sale_price": 12.0, "_recovered": True}
    try:
        ld = {"@type": "Product", "name": "Bottle",
              "offers": {"price": "12.00", "availability": "https://schema.org/InStock"}}
        page = Selector("<html><head><script type='application/ld+json'>" + _json.dumps(ld)
                        + "</script></head><body></body></html>")
        guessed = scraper.scrape("https://shop.example/p", "shop", log=lambda *a, **k: None,
                                 selector_rules={"shop.example": {"name": "h1"}}, page=page)
    finally:
        extractors.from_css_rules = real
    check("published price beats an adaptive guess",
          (guessed.price, guessed.sale_price), (12.0, None))


def test_itemlist_listing() -> None:
    """A category page described as a schema.org ItemList is read as a listing."""
    import json as _json
    from scrapling import Selector

    import extractors
    import scraper

    collection = {"@type": "CollectionPage", "name": "Light roasts", "mainEntity": {
        "@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": n, "item": {
                "@type": "Product", "name": name, "url": f"/coffee/roast/{n}",
                "offers": {"@type": "Offer", "price": price,
                           "availability": f"https://schema.org/{stock}"}}}
            for n, (name, price, stock) in enumerate(
                [("Tanzania", "24.45", "InStock"), ("Colombia", "65.70", "InStock"),
                 ("Kenya", "19.00", "OutOfStock")], start=1)]}}
    page = Selector("<html><head><script type='application/ld+json'>" + _json.dumps(collection)
                    + "</script></head><body></body></html>")
    check("a product list is not mistaken for one product",
          scraper._looks_like_single_product(page), False)
    items = extractors.extract_listing(page, "https://shop.example/coffee/light")
    check("every product in the list is read", [i["name"] for i in items],
          ["Tanzania", "Colombia", "Kenya"])
    check("list prices and stock read", [(i["price"], i["in_stock"]) for i in items],
          [(24.45, True), (65.7, True), (19.0, False)])
    check("list links made absolute", items[0]["product_url"], "https://shop.example/coffee/roast/1")

    related = {"@type": "Product", "name": "Mug", "offers": {"price": "9.50"},
               "isRelatedTo": {"@type": "ItemList", "itemListElement": [
                   {"@type": "Product", "name": "Cup", "url": "/c"},
                   {"@type": "Product", "name": "Saucer", "url": "/s"}]}}
    product_page = Selector("<html><head><script type='application/ld+json'>" + _json.dumps(related)
                            + "</script></head><body></body></html>")
    check("a product page with related products is still one product",
          extractors.jsonld_is_listing(product_page), False)


def test_beanbox_reader() -> None:
    """Size/grind prices from script tables, one-time purchases only."""
    import json as _json
    from scrapling import Selector

    import variants

    key_pid = {"12oz-single-whole": 1, "12oz-single-ground": 2, "2lb-single-whole": 3,
               "2lb-single-ground": 4, "12oz-monthly-whole": 5}
    pid_prods = {"1": {"price": "24.45", "original_price": "24.45"},
                 "2": {"price": "24.45", "original_price": "24.45"},
                 "3": {"price": "49.00", "original_price": "55.85"},
                 "4": {"price": "55.85", "original_price": "55.85"},
                 "5": {"price": "22.01", "original_price": "22.01"}}
    ld = {"@type": "Product", "name": "Tanzania - 12oz | Roaster",
          "offers": {"price": "24.45", "priceCurrency": "USD",
                     "availability": "https://schema.org/InStock"}}
    html = ("<html><head><script type='application/ld+json'>" + _json.dumps(ld) + "</script></head><body>"
            "<h1>Tanzania Songwe Peaberry</h1>"
            "<select id='size'><option value='12oz'>12-Ounce Bag</option><option value='2lb'>2-Pound Bag</option></select>"
            "<select id='format'><option value='whole'>Whole Bean</option><option value='ground'>Freshly Ground</option></select>"
            "<script>var duration = 'single'\n  var key_pid = " + _json.dumps(key_pid)
            + "\n  var pid_prods = " + _json.dumps(pid_prods, indent=2) + "\n</script></body></html>")
    found = variants.read_variants(Selector(html), "https://beanbox.example/coffee/roast/t/1")
    check("beanbox: one-time sizes only, subscription skipped", len(found), 4)
    check("beanbox: shopper's labels in the shop's order", [v["label"] for v in found],
          ["12-Ounce Bag / Whole Bean", "12-Ounce Bag / Freshly Ground",
           "2-Pound Bag / Whole Bean", "2-Pound Bag / Freshly Ground"])
    check("beanbox: original price above price is a sale", (found[2]["price"], found[2]["sale_price"]), (55.85, 49.0))
    check("beanbox: product name from the heading", found[0]["product"], "Tanzania Songwe Peaberry")
    check("beanbox: product-level stock applied", {v["in_stock"] for v in found}, {True})
    check("beanbox: every size has its own identity", len({v["url"] for v in found}), 4)
    check("beanbox reader ignores ordinary pages",
          variants.from_beanbox(Selector("<html><body><script>var x = {}</script></body></html>"), "u"), [])


def test_history_migration() -> None:
    """Adding the variant column must move old rows across, never relabel them."""
    import sheets

    class FakeTab:
        def __init__(self, title, header):
            self.title, self.header = title, list(header)
            self.inserted, self.updated = [], []

        def row_values(self, _row):
            return list(self.header)

        def insert_cols(self, values, col=1, **_):
            self.inserted.append((col, values))
            self.header.insert(col - 1, values[0][0])

        def update(self, values, where, **_):
            self.updated.append((where, values))

    class FakeBook:
        def __init__(self, tabs):
            self.tabs = tabs

        def worksheets(self):
            return list(self.tabs)

    legacy_data = FakeTab(config.DATA_TAB, config.LEGACY_DATA_HEADERS)
    legacy_alerts = FakeTab(config.ALERTS_TAB, config.LEGACY_ALERT_HEADERS)
    sheets.ensure_tabs(FakeBook([legacy_data, legacy_alerts]), log=lambda *a, **k: None)

    check("history gets a real column inserted after 'product'",
          legacy_data.inserted, [(4, [["variant"]])])
    check("history header is never overwritten", legacy_data.updated, [])
    check("history now has the new layout",
          [h.lower() for h in legacy_data.header], [h.lower() for h in config.DATA_HEADERS])
    check("alerts gain variant and change, each in place",
          legacy_alerts.inserted, [(4, [["variant"]]), (8, [["change"]])])

    # The layout just before the change column: only 'change' is added.
    before_change = FakeTab(config.ALERTS_TAB,
                            [h for h in config.ALERT_HEADERS if h != "change"])
    sheets.ensure_tabs(FakeBook([FakeTab(config.DATA_TAB, config.DATA_HEADERS), before_change]),
                       log=lambda *a, **k: None)
    check("alerts from last week gain just the change column",
          before_change.inserted, [(8, [["change"]])])

    current = FakeTab(config.DATA_TAB, config.DATA_HEADERS)
    sheets.ensure_tabs(FakeBook([current, FakeTab(config.ALERTS_TAB, config.ALERT_HEADERS)]),
                       log=lambda *a, **k: None)
    check("an up-to-date tab is left alone", (current.inserted, current.updated), ([], []))

    odd = FakeTab(config.DATA_TAB, ["when", "shop", "thing", "cost"])
    try:
        sheets.ensure_tabs(FakeBook([odd, FakeTab(config.ALERTS_TAB, config.ALERT_HEADERS)]),
                           log=lambda *a, **k: None)
        refused = False
    except sheets.SheetsError:
        refused = True
    check("an unrecognised layout is refused, not relabelled", (refused, odd.updated), (True, []))


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
        ("u/rise", "price rise"),
        ("u/sale", "price drop"),
        ("u/oos", "out of stock"),
    ])
    check("alert rules", got, want)

    changes = {a["product"]: a.get("change") for a in alerts}
    check("drop carries a negative change", changes["u/drop"], -20.0)
    check("rise carries a positive change", changes["u/rise"], 20.0)
    check("stock alert has no price change", changes["u/oos"], None)

    original = config.ALERT_ON_PRICE_RISE
    try:
        config.ALERT_ON_PRICE_RISE = False
        quiet = monitor.find_alerts(now, previous)
    finally:
        config.ALERT_ON_PRICE_RISE = original
    check("price rises can be switched off",
          any(a["alert"] == "price rise" for a in quiet), False)

    import notify
    message = notify.build_message(alerts, "2026-09-26 7:00", {"checked": 4, "ok": 3, "failed": 1})
    text = message.get_body(("plain",)).get_content()
    html_body = message.get_body(("html",)).get_content()
    check("email text shows the signed change", "(+20.00)" in text and "(-20.00)" in text, True)
    check("email html colours the change", "#17692e" in html_body and "#b31b1b" in html_body, True)

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
    # Look columns up by heading, not position, so a new column cannot
    # silently shift what these checks are reading.
    col = {name: index for index, name in enumerate(config.DATA_HEADERS)}
    check("row is as wide as the header", len(row), len(config.DATA_HEADERS))
    check("price column holds the normal price", row[col["price"]], 100.0)
    check("sale column holds the discounted price", row[col["sale price"]], 70.0)
    check("stock is written lower case", row[col["in stock"]], "yes")
    check("variant cell empty for a product without options", row[col["variant"]], "")
    check("competitor cell is a link", row[col["competitor"]].startswith("=HYPERLINK("), True)
    # The URL column is the plain address, NOT a formula: Sheets auto-links a
    # bare URL, and a real URL in the cell copies and exports cleanly.
    check("URL cell is the plain address",
          row[col["URL"]], "https://shop.example.com/p/1")
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
            "variants:https://f.com/p",           # every variant
            "Var Shop | variants:https://g.com/p",
            "variants:list:https://h.com/all",    # both, either order
            "LIST:VARIANTS:https://i.com/all",
            "variants:",                          # prefix with no address
        ],
        [
            ("a.com", "https://a.com/p", False, False),
            ("Named Shop", "https://b.com/p", False, False),
            ("d.com", "https://d.com/shop", True, False),
            ("Grid Shop", "https://e.com/all", True, False),
            ("f.com", "https://f.com/p", False, True),
            ("Var Shop", "https://g.com/p", False, True),
            ("h.com", "https://h.com/all", True, True),
            ("i.com", "https://i.com/all", True, True),
        ],
    )
    test_variants_opt_in()
    test_listing_extraction()
    test_page_kind_detection()
    test_variant_readers()
    test_variant_rows_and_changes()
    test_one_look()
    test_adaptive_canary()
    test_itemlist_listing()
    test_beanbox_reader()
    test_history_migration()
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
