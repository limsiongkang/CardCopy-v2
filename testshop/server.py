"""
A pretend shop, running on this computer only, for testing the alerts.

    Shop:   http://localhost:8765/          (a category page)
    Admin:  http://localhost:8765/admin     (change prices and stock here)

Its pages are built the way real shops build theirs - structured product
data, a category grid, and WooCommerce-style variant data - so the monitor
reads it exactly as it reads a competitor. The difference is that you
decide when a price changes.

Start it with start_testshop.ps1 (or: python testshop/server.py). It only
listens on this computer; nothing on your network or the internet can
reach it. Stop it with Ctrl+C or by closing its window.

Prices and stock live in testshop/state.json, created on first start.
"""

from __future__ import annotations

import copy
import html
import json
import random
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"          # this computer only
PORT = 8765
STATE_FILE = Path(__file__).with_name("state.json")

# ----------------------------------------------------------------------
# The products. A product with one variant called "main" has no options.
# ----------------------------------------------------------------------

STARTING_STOCK = {
    "coffee-beans": {
        "name": "Test Coffee Beans",
        "option": "Size",
        "variants": [
            {"id": "250g", "price": 12.00, "sale": None, "in_stock": True},
            {"id": "500g", "price": 22.00, "sale": None, "in_stock": True},
            {"id": "1kg",  "price": 40.00, "sale": None, "in_stock": True},
        ],
    },
    "t-shirt": {
        "name": "Test T-Shirt",
        "option": "Size",
        "variants": [
            {"id": "S",  "price": 20.00, "sale": None, "in_stock": True},
            {"id": "M",  "price": 20.00, "sale": None, "in_stock": True},
            {"id": "L",  "price": 20.00, "sale": None, "in_stock": True},
            {"id": "XL", "price": 22.00, "sale": None, "in_stock": False},
        ],
    },
    "mug": {
        "name": "Test Mug",
        "variants": [{"id": "main", "price": 9.50, "sale": None, "in_stock": True}],
    },
    "water-bottle": {
        "name": "Test Water Bottle",
        "variants": [{"id": "main", "price": 15.00, "sale": 12.00, "in_stock": True}],
    },
    "backpack": {
        "name": "Test Backpack",
        "variants": [{"id": "main", "price": 45.00, "sale": None, "in_stock": False}],
    },
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    state = copy.deepcopy(STARTING_STOCK)
    save_state(state)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def has_options(product: dict) -> bool:
    return not (len(product["variants"]) == 1 and product["variants"][0]["id"] == "main")


def paid(variant: dict) -> float:
    """What a shopper pays: the sale price when there is one."""
    return variant["sale"] if variant.get("sale") is not None else variant["price"]


def money(value: float) -> str:
    return f"${value:,.2f}"


def e(text) -> str:
    return html.escape(str(text), quote=True)


# ----------------------------------------------------------------------
# Shop pages
# ----------------------------------------------------------------------

PAGE_STYLE = """
<style>
 body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#fafafa;color:#222}
 header{background:#2d3e50;color:#fff;padding:14px 24px}
 header a{color:#fff;text-decoration:none;font-weight:600}
 main{max-width:960px;margin:24px auto;padding:0 16px}
 ul.products{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}
 li.product{background:#fff;border:1px solid #ddd;border-radius:6px;padding:16px}
 li.product a{color:#222;text-decoration:none}
 .price del{color:#999;margin-right:6px} .price ins{text-decoration:none;color:#b00020;font-weight:600}
 .stock{font-size:13px} .outofstock .stock{color:#b00020} .instock .stock{color:#1b7a3a}
 .note{background:#fff8e1;border:1px solid #f0d58c;padding:10px 14px;border-radius:6px;font-size:14px}
</style>
"""


def price_html(regular: float, sale: float | None) -> str:
    if sale is not None and sale < regular:
        return (f'<del><span class="amount">{money(regular)}</span></del>'
                f'<ins><span class="amount">{money(sale)}</span></ins>')
    return f'<span class="amount">{money(regular)}</span>'


def category_page(state: dict) -> str:
    tiles = []
    for slug, product in state.items():
        variants = product["variants"]
        any_stock = any(v["in_stock"] for v in variants)
        if has_options(product):
            low, high = min(paid(v) for v in variants), max(paid(v) for v in variants)
            price = (f'<span class="amount">{money(low)}</span>' if low == high else
                     f'<span class="amount">{money(low)}</span> &ndash; '
                     f'<span class="amount">{money(high)}</span>')
        else:
            price = price_html(variants[0]["price"], variants[0]["sale"])
        tiles.append(f"""
      <li class="product {'instock' if any_stock else 'outofstock'}" itemscope itemtype="http://schema.org/Product">
        <a href="/product/{slug}/">
          <h2 class="woocommerce-loop-product__title" itemprop="name">{e(product['name'])}</h2>
          <span class="price">{price}</span>
          <p class="stock">{'In stock' if any_stock else 'Out of stock'}</p>
        </a>
      </li>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Test Shop</title>{PAGE_STYLE}</head>
<body><header><a href="/">Test Shop</a></header><main>
<p class="note">This is a pretend shop for testing the competitor monitor.
Change its prices at <a href="/admin">/admin</a>.</p>
<h1>All products</h1>
<ul class="products">{''.join(tiles)}
</ul></main></body></html>"""


def product_page(slug: str, product: dict) -> str:
    variants = product["variants"]
    any_stock = any(v["in_stock"] for v in variants)
    lowest = min(paid(v) for v in variants)

    structured = {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": product["name"],
        "sku": slug,
        "offers": {
            "@type": "Offer",
            "price": f"{lowest:.2f}",
            "priceCurrency": "USD",
            "availability": "https://schema.org/" + ("InStock" if any_stock else "OutOfStock"),
        },
    }

    if has_options(product):
        option_key = "attribute_" + product["option"].lower()
        variation_data = [{
            "variation_id": index + 1,
            "attributes": {option_key: v["id"]},
            "display_price": paid(v),
            "display_regular_price": v["price"],
            "is_in_stock": v["in_stock"],
            "variation_is_active": True,
        } for index, v in enumerate(variants)]
        options = "".join(f'<option value="{e(v["id"])}">{e(v["id"])}</option>' for v in variants)
        rows = "".join(
            f"<tr><td>{e(v['id'])}</td><td class='price'>{price_html(v['price'], v['sale'])}</td>"
            f"<td>{'In stock' if v['in_stock'] else 'Out of stock'}</td></tr>"
            for v in variants)
        body = f"""
<p class="price">From {money(lowest)}</p>
<form class="variations_form cart" data-product_variations="{e(json.dumps(variation_data))}">
  <label for="{option_key}">{e(product['option'])}</label>
  <select id="{option_key}" name="{option_key}" data-attribute_name="{option_key}">
    <option value="">Choose an option</option>{options}
  </select>
</form>
<table style="margin-top:16px;border-collapse:collapse">{rows}</table>"""
    else:
        v = variants[0]
        body = f"""
<p class="price">{price_html(v['price'], v['sale'])}</p>
<p class="stock {'in-stock' if v['in_stock'] else 'out-of-stock'}">{'In stock' if v['in_stock'] else 'Out of stock'}</p>"""

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(product['name'])} - Test Shop</title>
<script type="application/ld+json">{json.dumps(structured)}</script>{PAGE_STYLE}</head>
<body><header><a href="/">Test Shop</a></header><main>
<h1 class="product_title entry-title">{e(product['name'])}</h1>{body}
</main></body></html>"""


# ----------------------------------------------------------------------
# Layouts - the same products, built four different ways
# ----------------------------------------------------------------------
#
# A real shop redesign changes how a page is built, not what it sells. These
# layouts do exactly that, so you can see what the monitor survives:
#
#   classic    - the original layout
#   redesign   - every tag and CSS class renamed, product data still published
#   bare       - renamed AND no machine-readable product data at all
#   javascript - the page arrives empty; a script builds it in the browser

LAYOUT_FILE = Path(__file__).with_name("layout.txt")
LAYOUTS = {
    "classic": "Classic (the original layout)",
    "redesign": "Redesigned - new HTML and CSS, still publishes product data",
    "bare": "Redesigned, no product data - the hardest case",
    "javascript": "Built by JavaScript - the page is empty until a script runs",
}


def load_layout() -> str:
    try:
        name = LAYOUT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "classic"
    return name if name in LAYOUTS else "classic"


def save_layout(name: str) -> None:
    LAYOUT_FILE.write_text(name if name in LAYOUTS else "classic", encoding="utf-8")


def _structured_group(slug: str, product: dict) -> dict:
    """schema.org ProductGroup: the published standard for variants."""
    return {
        "@context": "https://schema.org/",
        "@type": "ProductGroup",
        "name": product["name"],
        "productGroupID": slug,
        "variesBy": ["https://schema.org/size"],
        "hasVariant": [{
            "@type": "Product",
            "name": f"{product['name']} - {v['id']}",
            "size": v["id"],
            "sku": f"{slug}-{v['id']}",
            "offers": {
                "@type": "Offer",
                "price": f"{paid(v):.2f}",
                "priceCurrency": "USD",
                "availability": "https://schema.org/" + ("InStock" if v["in_stock"] else "OutOfStock"),
                "url": f"/product/{slug}/?size={v['id']}",
            },
        } for v in product["variants"]],
    }


def _page(title: str, head: str, body: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{e(title)}</title>'
            f"{head}</head><body>{body}</body></html>")


# --- redesign: new markup, product data kept --------------------------

REDESIGN_STYLE = """<style>
 body{font-family:Georgia,serif;margin:0;background:#f4efe6;color:#2b2b2b}
 .topbar{background:#6b3e26;color:#fff;padding:18px 28px;font-size:20px}
 .catalogue__grid{display:flex;flex-wrap:wrap;gap:20px;padding:28px}
 .tile{background:#fff;width:220px;padding:18px;box-shadow:0 2px 6px #0002}
 .tile a{color:inherit;text-decoration:none} .tile__cost{font-size:18px;margin-top:8px}
 .pdp{max-width:760px;margin:30px auto;background:#fff;padding:28px}
 .pdp__cost .was{text-decoration:line-through;color:#999;margin-right:8px}
 .badge{display:inline-block;margin-top:12px;padding:3px 10px;border-radius:12px;background:#e6f2e6}
 .badge.off{background:#f6dcdc} .options td{padding:4px 14px 4px 0}
</style>"""


def _redesign_cost(v: dict) -> str:
    if v.get("sale") is not None and v["sale"] < v["price"]:
        return f'<span class="was">{money(v["price"])}</span><span class="now">{money(v["sale"])}</span>'
    return f'<span class="now">{money(v["price"])}</span>'


def redesign_category(state: dict) -> str:
    tiles = []
    for slug, product in state.items():
        variants = product["variants"]
        stock = any(v["in_stock"] for v in variants)
        cost = (f"from {money(min(paid(v) for v in variants))}" if has_options(product)
                else _redesign_cost(variants[0]))
        tiles.append(f"""
  <article class="tile" data-product-id="{slug}">
    <a class="tile__link" href="/product/{slug}/">
      <h3 class="tile__name">{e(product['name'])}</h3>
      <div class="tile__cost">{cost}</div>
      <div class="tile__flag">{'Available' if stock else 'Sold out'}</div>
    </a>
  </article>""")
    return _page("Test Shop", REDESIGN_STYLE,
                 f'<div class="topbar">Test Shop &middot; new look</div>'
                 f'<section class="catalogue"><div class="catalogue__grid">{"".join(tiles)}</div></section>')


def redesign_product(slug: str, product: dict) -> str:
    variants = product["variants"]
    if has_options(product):
        structured = _structured_group(slug, product)
        rows = "".join(f"<tr><td>{e(v['id'])}</td><td>{_redesign_cost(v)}</td>"
                       f"<td>{'Available' if v['in_stock'] else 'Sold out'}</td></tr>" for v in variants)
        detail = f'<table class="options">{rows}</table>'
    else:
        v = variants[0]
        structured = {
            "@context": "https://schema.org/", "@type": "Product", "name": product["name"], "sku": slug,
            "offers": {"@type": "Offer", "price": f"{paid(v):.2f}", "priceCurrency": "USD",
                       "availability": "https://schema.org/" + ("InStock" if v["in_stock"] else "OutOfStock")},
        }
        detail = (f'<div class="pdp__cost">{_redesign_cost(v)}</div>'
                  f'<span class="badge{"" if v["in_stock"] else " off"}">'
                  f'{"Available" if v["in_stock"] else "Sold out"}</span>')
    head = f'<script type="application/ld+json">{json.dumps(structured)}</script>{REDESIGN_STYLE}'
    return _page(product["name"], head,
                 f'<div class="topbar">Test Shop &middot; new look</div>'
                 f'<div class="pdp"><div class="pdp__info"><h1 class="pdp__heading">{e(product["name"])}</h1>'
                 f'{detail}</div></div>')


# --- bare: new markup, NO product data ---------------------------------

def _bare_cost(v: dict) -> str:
    if v.get("sale") is not None and v["sale"] < v["price"]:
        return f'<span class="p-old">{money(v["price"])}</span> <span class="p-now">{money(v["sale"])}</span>'
    return f'<span class="p-now">{money(v["price"])}</span>'


def bare_category(state: dict) -> str:
    items = []
    for slug, product in state.items():
        stock = any(v["in_stock"] for v in product["variants"])
        items.append(f'<div class="shelf-item"><a href="/product/{slug}/"><b>{e(product["name"])}</b></a>'
                     f'<div>{_bare_cost(product["variants"][0])}</div>'
                     f'<div>{"In stock" if stock else "Out of stock"}</div></div>')
    return _page("Test Shop", "", f'<h2>Test Shop</h2><div class="shelf">{"".join(items)}</div>')


def bare_product(slug: str, product: dict) -> str:
    variants = product["variants"]
    if has_options(product):
        rows = "".join(f"<tr><td>{e(v['id'])}</td><td>{_bare_cost(v)}</td>"
                       f"<td>{'In stock' if v['in_stock'] else 'Out of stock'}</td></tr>" for v in variants)
        detail = f"<table>{rows}</table>"
    else:
        v = variants[0]
        detail = (f'<div class="p-cost">{_bare_cost(v)}</div>'
                  f'<p class="p-avail">{"In stock" if v["in_stock"] else "Out of stock"}</p>')
    return _page(product["name"], "",
                 f'<main class="p-wrap"><h1 class="p-title">{e(product["name"])}</h1>{detail}</main>')


# --- javascript: empty page, a script builds it ------------------------

def javascript_version(full_html: str) -> str:
    """
    The same page, but delivered empty: a script puts the content in place
    when a browser runs it. A plain request sees only "Loading...".
    """
    head_start, body_start = full_html.find("<head>"), full_html.find("<body>")
    head = full_html[head_start + 6:full_html.find("</head>")]
    body = full_html[body_start + 6:full_html.find("</body>")]
    # The product data travels inside the script as text, so it only exists
    # on the page once the script has run. "</" is escaped so a </script>
    # inside the text cannot end the outer script early.
    def js_string(text: str) -> str:
        return json.dumps(text).replace("</", "<\\/")

    return _page("Test Shop", "",
                 '<div id="app">Loading...</div>'
                 f"<script>document.head.insertAdjacentHTML('beforeend', {js_string(head)});"
                 f"document.body.innerHTML = {js_string(body)};</script>")


def render_category(state: dict) -> str:
    layout = load_layout()
    if layout == "redesign":
        return redesign_category(state)
    if layout == "bare":
        return bare_category(state)
    page = category_page(state)
    return javascript_version(page) if layout == "javascript" else page


def render_product(slug: str, product: dict) -> str:
    layout = load_layout()
    if layout == "redesign":
        return redesign_product(slug, product)
    if layout == "bare":
        return bare_product(slug, product)
    page = product_page(slug, product)
    return javascript_version(page) if layout == "javascript" else page


# ----------------------------------------------------------------------
# Admin page
# ----------------------------------------------------------------------

def admin_page(state: dict, message: str = "") -> str:
    rows = []
    for slug, product in state.items():
        for v in product["variants"]:
            key = f"{slug}__{v['id']}"
            label = e(product["name"]) + (f" &mdash; {e(v['id'])}" if has_options(product) else "")
            sale = "" if v["sale"] is None else f"{v['sale']:.2f}"
            rows.append(f"""
      <tr><td>{label}</td>
        <td><input name="{key}__price" value="{v['price']:.2f}" size="8" inputmode="decimal"></td>
        <td><input name="{key}__sale" value="{sale}" size="8" inputmode="decimal" placeholder="none"></td>
        <td style="text-align:center"><input type="checkbox" name="{key}__stock" {'checked' if v['in_stock'] else ''}></td></tr>""")

    banner = f'<p class="note" style="background:#e7f5ea;border-color:#9fd3ad">{e(message)}</p>' if message else ""
    current = load_layout()
    layout_choices = "".join(
        f'<label style="display:block;margin:4px 0"><input type="radio" name="layout" value="{key}"'
        f'{" checked" if key == current else ""}> {e(label)}</label>'
        for key, label in LAYOUTS.items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Test Shop admin</title>{PAGE_STYLE}
<style>table.admin{{border-collapse:collapse;background:#fff}} table.admin td,table.admin th{{border:1px solid #ddd;padding:6px 10px}}
table.admin th{{background:#f0f2f5;text-align:left}} button{{padding:8px 14px;margin:4px 6px 4px 0;cursor:pointer}}
ol li{{margin:6px 0}} code{{background:#eef;padding:1px 5px;border-radius:3px}}</style></head>
<body><header><a href="/">Test Shop</a> &nbsp;&middot;&nbsp; admin</header><main>
{banner}
<h2>How to test the alerts</h2>
<ol>
 <li>In PowerShell, in the project folder, run <code>.\\run.ps1 -Test</code> once. That records today's test prices.</li>
 <li>Change something below and click <b>Save changes</b> (or click <b>Simulate some changes</b>).</li>
 <li>Run <code>.\\run.ps1 -Test</code> again. Check the <b>TEST Alerts</b> tab in the sheet, and your email.</li>
</ol>
<p class="note">What alerts: <b>any price change</b> - down (including a new sale price) or up - and
something going <b>from in stock to out of stock</b>. The TEST Alerts tab's <b>change</b> column shows the
difference, e.g. <b style="color:#17692e">+3.00</b> or <b style="color:#b31b1b">-2.00</b>.
Back-in-stock alerts only if <code>ALERT_ON_BACK_IN_STOCK=true</code> in .env.</p>

<h2>Shop layout</h2>
<p>Switch how the shop's pages are built - the products and prices stay the same - to test whether
the monitor still finds everything after a competitor redesigns their site. Run
<code>.\\run.ps1 -Test</code> once on <b>Classic</b> first, so adaptive mode can learn the page.</p>
<form method="post" action="/admin/layout">
{layout_choices}
<p><button type="submit">Switch layout</button></p>
</form>

<h2>Prices and stock</h2>
<form method="post" action="/admin">
<table class="admin">
  <tr><th>Product</th><th>Price</th><th>Sale price</th><th>In stock</th></tr>{''.join(rows)}
</table>
<p><button type="submit">Save changes</button></p>
</form>
<form method="post" action="/admin/simulate" style="display:inline"><button>Simulate some changes</button></form>
<form method="post" action="/admin/reset" style="display:inline"><button>Reset to starting prices</button></form>
<p style="margin-top:24px"><a href="/">View the shop as the monitor sees it</a></p>
</main></body></html>"""


def apply_form(state: dict, form: dict) -> list[str]:
    """Update prices and stock from the admin form. Returns problems, if any."""
    problems = []
    for slug, product in state.items():
        for v in product["variants"]:
            key = f"{slug}__{v['id']}"
            label = product["name"] + (f" {v['id']}" if has_options(product) else "")
            raw_price = form.get(f"{key}__price", [""])[0].strip().lstrip("$")
            raw_sale = form.get(f"{key}__sale", [""])[0].strip().lstrip("$")
            try:
                price = float(raw_price)
                if price <= 0:
                    raise ValueError
                v["price"] = round(price, 2)
            except ValueError:
                problems.append(f"{label}: '{raw_price}' is not a price, kept {money(v['price'])}")
            if raw_sale:
                try:
                    sale = float(raw_sale)
                    if not 0 < sale < v["price"]:
                        raise ValueError
                    v["sale"] = round(sale, 2)
                except ValueError:
                    problems.append(f"{label}: sale price must be above 0 and below the price; sale removed")
                    v["sale"] = None
            else:
                v["sale"] = None
            v["in_stock"] = f"{key}__stock" in form
    return problems


def simulate(state: dict) -> list[str]:
    """Make a few realistic changes: one price drop, one sell-out, one price rise."""
    notes = []
    everything = [(slug, p, v) for slug, p in state.items() for v in p["variants"]]
    random.shuffle(everything)

    def name(p, v):
        return p["name"] + (f" {v['id']}" if has_options(p) else "")

    for slug, p, v in everything:
        if v["in_stock"]:
            new = round(paid(v) * 0.85, 2)
            v["sale"] = new if new < v["price"] else None
            notes.append(f"{name(p, v)}: now on sale at {money(new)} (price drop - should alert)")
            break
    for slug, p, v in everything:
        if v["in_stock"] and not any(name(p, v) in n for n in notes):
            v["in_stock"] = False
            notes.append(f"{name(p, v)}: now out of stock (should alert)")
            break
    for slug, p, v in everything:
        if not any(name(p, v) in n for n in notes):
            v["price"] = round(v["price"] + 3, 2)
            notes.append(f"{name(p, v)}: price up by $3.00 (should alert)")
            break
    return notes


# ----------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        state = load_state()
        if path == "/":
            return self._send(render_category(state))
        if path == "/admin":
            query = parse_qs(urlparse(self.path).query)
            return self._send(admin_page(state, query.get("msg", [""])[0]))
        if path.startswith("/product/"):
            slug = path.strip("/").split("/")[-1]
            if slug in state:
                return self._send(render_product(slug, state[slug]))
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._send("<h1>Not found</h1>", 404)

    def do_POST(self):
        from urllib.parse import quote

        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        state = load_state()

        if path == "/admin":
            problems = apply_form(state, form)
            save_state(state)
            message = "Saved. " + (" | ".join(problems) if problems else "Now run .\\run.ps1 -Test again.")
        elif path == "/admin/simulate":
            notes = simulate(state)
            save_state(state)
            message = "Simulated: " + " | ".join(notes)
        elif path == "/admin/layout":
            choice = form.get("layout", ["classic"])[0]
            save_layout(choice)
            message = f"Layout is now: {LAYOUTS.get(choice, LAYOUTS['classic'])}. Run .\\run.ps1 -Test to see what the monitor makes of it."
        elif path == "/admin/reset":
            state = copy.deepcopy(STARTING_STOCK)
            save_state(state)
            save_layout("classic")
            message = "Reset to the starting prices, stock and the classic layout."
        else:
            return self._send("<h1>Not found</h1>", 404)
        self._redirect("/admin?msg=" + quote(message))

    def log_message(self, fmt, *args):
        sys.stdout.write(f"  {self.address_string()} {fmt % args}\n")


class ExclusiveServer(ThreadingHTTPServer):
    # Python's default lets a second copy share the port on Windows, and the
    # older copy silently keeps answering - with whatever code it started
    # with. Refuse instead, so there is only ever one shop.
    allow_reuse_address = False


def main() -> None:
    load_state()
    try:
        server = ExclusiveServer((HOST, PORT), Handler)
    except OSError:
        print(f"The test shop is already running (something is using port {PORT}).\n"
              f"Use that window, or close it and start this again.")
        sys.exit(1)
    print(f"Test shop running.\n  Shop : http://localhost:{PORT}/\n  Admin: http://localhost:{PORT}/admin\n"
          f"Leave this window open while testing. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
