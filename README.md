# Competitor Price & Stock Monitor
// test
Checks competitor product pages every morning at 7am, records what it finds in a
Google Sheet, and emails you when a price drops or something goes out of stock.

Everything lives in this one folder. You can move the folder, but if you do,
run `install_schedule.ps1` again afterwards so Windows knows the new location.

---

## What still needs doing

Work through these five steps in order. Steps 2–4 involve passwords and account
setup, so **you** need to do them — I have deliberately not touched your
credentials, and nothing secret is stored in the code.

| # | Step | Roughly how long |
|---|------|------------------|
| 1 | Put your competitor URLs in `competitors.txt` | 5 min |
| 2 | Paste your proxy details into `.env` | 2 min |
| 3 | Create a Google "robot account" so the script can write to Sheets | 10 min |
| 4 | Create a Gmail app password so the script can email you | 5 min |
| 5 | Test it, then switch on the 7am schedule | 5 min |

---

## Step 1 — Add your competitor URLs

Open `competitors.txt` in Notepad. Put one product page address per line:

```
https://www.competitor-a.com/products/blue-widget
https://www.competitor-b.com/shop/blue-widget-pro
```

If you want to control how a competitor is named in the sheet, use a pipe:

```
Competitor A | https://www.competitor-a.com/products/blue-widget
```

Without the pipe, the name is taken from the web address, so
`www.competitor-a.com` becomes `Competitor-A`.

Lines starting with `#` are ignored, so you can park a URL without deleting it.

**These must be individual product pages**, not category or search pages. One
line per product you want to track.

---

## Step 2 — Your proxy details

Open `.env` in Notepad. Find this section and paste in what your proxy provider
gave you:

```
PROXY_SERVER=http://gate.yourprovider.com:7000
PROXY_USERNAME=your-username
PROXY_PASSWORD=your-password
```

`.env` is the only place any password lives. It is listed in `.gitignore`, so it
will never be copied into a code repository, and the script never prints its
contents to the screen or into the log files.

**To test without a proxy first**, leave `PROXY_SERVER` empty. The script will
then use your own internet connection. That is fine for a handful of pages, but
switch the proxy back on before you monitor a long list.

---

## Step 3 — Let the script write to Google Sheets

The script signs in as its own "robot account" rather than as you. That way
nothing expires and nothing needs approving at 7 in the morning.

1. Go to **https://console.cloud.google.com/** and sign in.
2. At the top left, click the project dropdown, then **New Project**. Name it
   anything, e.g. `Competitor Monitor`. Click **Create**, then make sure the new
   project is selected.
3. In the search bar at the top, search for **Google Sheets API**. Open it and
   click **Enable**.
4. Search for **Google Drive API**. Open it and click **Enable** as well.
   (The script needs this one to create and share the spreadsheet.)
5. In the left menu go to **APIs & Services → Credentials**.
6. Click **+ Create Credentials → Service account**.
   - Name it `competitor-monitor`. Click **Create and Continue**, then **Done**.
7. You are back on the Credentials page. Under **Service Accounts**, click the
   account you just made.
8. Open the **Keys** tab → **Add Key → Create new key** → choose **JSON** →
   **Create**. A `.json` file downloads.
9. Rename the downloaded file to **`google-service-account.json`** and move it
   into the **`secrets`** folder inside this project.
10. Open it in Notepad and find the line that says `"client_email"`. Copy that
    address — it looks like
    `competitor-monitor@yourproject.iam.gserviceaccount.com`.

That is the robot's email address. The first time you run the script, it will
create the spreadsheet itself and share it with you automatically.

> **If you would rather use a sheet you already have:** create it in Google
> Sheets, click **Share**, paste in the robot's address from step 10, set it to
> **Editor**, and send. Then copy the long code out of the sheet's web address
> (the part between `/d/` and `/edit`) and paste it into `.env` as
> `GOOGLE_SHEET_ID=`.

> **Google will not let the robot CREATE the sheet.** Service accounts get no
> Drive storage of their own, so the first time you run either script you must
> make the sheet yourself and share it with the robot:
>
> 1. Open <https://sheets.new> and name it `Cardcopy - Competitor Intel`
> 2. Click **Share**, paste in the robot's address from step 10, set **Editor**, Send
> 3. Copy the long code from the sheet's web address (between `/d/` and `/edit`)
>    into `.env` as `GOOGLE_SHEET_ID=`
>
> You do this once. After that the robot edits it freely.

---

## Step 4 — Let the script email you

Gmail will not accept your normal password from a script. You need a 16-character
"app password" instead.

1. Your Google account needs 2-Step Verification switched on. If it is not, turn
   it on at **https://myaccount.google.com/security**.
2. Go to **https://myaccount.google.com/apppasswords**.
3. Type a name like `Competitor Monitor` and click **Create**.
4. Google shows you 16 letters in four blocks. Copy them.
5. Open `.env` and fill in:

```
ALERT_RECIPIENT=you@yourcompany.com
GMAIL_ADDRESS=your.address@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop
```

Type the app password without the spaces. `ALERT_RECIPIENT` is where alerts go and
can be any address — it does not have to be the Gmail one.

Also fill in the client name near the top of `.env`, because it names the sheet:

```
CLIENT_NAME=Acme Ltd
```

That produces a sheet called **Acme Ltd - Competitor Intel**.

---

## Step 5 — Test it, then schedule it

Open PowerShell in this folder (right-click the folder while holding Shift →
*Open PowerShell window here*), and work through these in order.

**a) Check one page, write nothing:**

```powershell
.\run.ps1 -DryRun -Limit 1
```

You should see the product name, price and stock status printed in a table.
Nothing is sent and nothing is saved.

**b) Check everything, still writing nothing:**

```powershell
.\run.ps1 -DryRun
```

Look down the table for rows saying `FAILED` or showing no price. Those sites
need a closer look — see *When a site will not read* below.

**c) A real run.** This creates the sheet, writes the first rows and, because
there is nothing to compare against yet, sends no alert email:

```powershell
.\run.ps1
```

The script prints the address of the new sheet. It also prints a line beginning
`GOOGLE_SHEET_ID=` — **copy that line into your `.env`**, so tomorrow's run uses
the same sheet instead of hunting for it by name.

**d) Check the email works** without waiting for a price to change:

```powershell
.\run.ps1 -AlwaysEmail
```

**e) Switch on the 7am schedule:**

```powershell
.\install_schedule.ps1
```

By default the run happens at 7am **while you are logged in**. If your PC is
usually on but logged out overnight, use this instead:

```powershell
.\install_schedule.ps1 -RunWhenLoggedOff
```

If the PC is switched off at 7am, the run happens the next time you turn it on.

To test the schedule immediately: `Start-ScheduledTask -TaskName 'Competitor Monitor - Daily'`

To turn it off again: `.\install_schedule.ps1 -Remove`

---

## What you get

**The `Data` tab** — one row per product, per day:

| date | competitor | product | price | sale price | in stock | URL |
|------|-----------|---------|-------|------------|----------|-----|

`price` is the normal price. `sale price` is only filled in when the product is
currently discounted — in that case `price` is the crossed-out "was" price and
`sale price` is what a shopper actually pays.

**The `Alerts` tab** — a row each time something changed:

| date | competitor | product | alert | was | now | URL |
|------|-----------|---------|-------|-----|-----|-----|

Two things raise an alert, and only these two:

- **Price drop** — what a shopper pays today is lower than at the previous run.
- **Went out of stock** — it was in stock last time and is not now.

A price *rise* does not raise an alert. Neither does a product coming back into
stock. If you want either of those, say so and I will add them.

**Important:** if a page fails to load, it raises no alert at all. A timeout is
not evidence that a product sold out, and alerting on it would quickly teach you
to ignore the emails. Failed pages appear in the sheet marked `ERROR` and are
counted in the email summary instead.

---

## Everyday commands

```powershell
.\run.ps1                    # normal run
.\run.ps1 -DryRun            # test: change nothing, send nothing
.\run.ps1 -Limit 5           # only the first 5 URLs
.\run.ps1 -NoEmail           # update the sheet, skip the email
.\run.ps1 -AlwaysEmail       # email even when nothing changed
.\run.ps1 -Url "https://..." # check one page
```

Every run also writes a log to the `logs` folder, named for the date. If a 7am
run misbehaves, that file is the place to look.

---

## How it reads the prices

Most online shops quietly publish their product details in a machine-readable
block inside the page, so that Google Shopping can list them. The script reads
that first, which is why it works on sites it has never seen before and gets the
exact figures rather than guessing from the text.

If a site does not publish that block, it falls back in order to: the older
`itemprop` markup, then social-sharing tags, then the visible text on the page.
The `read from:` line in the output tells you which one was used.

### About "adaptive mode"

Adaptive mode is switched on. It is worth knowing what it actually does, because
the name oversells it.

Adaptive mode **remembers an element that a selector matched successfully**, so
that when the shop redesigns its pages and the old selector stops working,
Scrapling can find the same element again by its saved characteristics. You will
see `adaptive mode recovered ...` in the output when that happens.

What it does **not** do is work out where the price is on a site it has never
seen. It protects you from redesigns from the second run onwards; the
machine-readable data described above is what handles the first run.

---

## When a site will not read

Try these in order.

**1. Watch what the browser is doing.** In `.env` set `HEADLESS=false`, then run
`.\run.ps1 -DryRun -Url "the-failing-url"`. A browser window opens and you can
see whether the page is loading, showing a cookie wall, or blocking you.

**2. The site uses Cloudflare.** In `.env` set `SOLVE_CLOUDFLARE=true`. It is
slower, so leave it off unless you need it.

**3. The page is slow.** Raise `PAGE_TIMEOUT_MS` from `45000` to `90000`.

**4. You are being rate-limited.** Raise `DELAY_BETWEEN_URLS` from `3` to
`15` or more.

**5. The price loads only after you pick a size or colour.** These pages need a
rule written specifically for them. Send me the URL and I will add one.

---

## A note on scraping competitors

`StealthyFetcher` is built to look like an ordinary human browser, which is how
it gets past bot detection. That is standard practice for price monitoring, but
it does cut against most sites' terms of service, so it is worth being a
deliberate choice rather than an accident.

The settings here are deliberately gentle: a real browser, a 6-second gap between
pages, and a few seconds of random jitter so the timing does not look mechanical.
Please leave `DELAY_BETWEEN_URLS` at 3 or higher. Turning it down is what
turns polite monitoring into something a site will notice and block.

---

## The files

| File | What it is |
|------|-----------|
| `competitors.txt` | Your list of URLs. The one you edit most. |
| `.env` | All settings and passwords. Never leaves this machine. |
| `.env.example` | A blank copy of the above, for reference. |
| `secrets/google-service-account.json` | Your Google robot key, from step 3. |
| `monitor.py` | The main script — runs the whole job. |
| `scraper.py` | Fetches the pages through Scrapling. |
| `extractors.py` | Works out name, price and stock from a page. |
| `sheets.py` | Reads and writes the Google Sheet. |
| `notify.py` | Builds and sends the email. |
| `config.py` | Loads `.env` and checks it for mistakes. |
| `run.ps1` | What you type to run it. |
| `install_schedule.ps1` | Sets up (or removes) the 7am schedule. |
| `logs/` | One log file per day. |


## Variants (sizes, colours, grinds...)

Put `variants:` in front of a URL in `competitors.txt` and that product gets
**one row per variant** (each size, colour, grind...), each with its own
price, sale price and stock status. Without it, the product is one row.

```
https://shop.example/products/coffee               one row
variants:https://shop.example/products/coffee      one row per variant
list:variants:https://shop.example/collections/all every variant of every product
```

The words combine in either order, and work after a name too
(`My Shop | variants:https://...`).

This matters for stock in particular. A shop's headline data describes the
product as a whole, and for a product with options it is often wrong -
WooCommerce, for example, can mark a product "out of stock" while every size
can be bought. The per-variant data is the truth, so it is what gets used.

No clicking through options is needed. Shops ship every variant's price and
stock inside the page, because the page's own script needs it to update the
price when you pick an option. It is read from:

- **Shopify** - the store's `/products/<name>.js` data file
- **WooCommerce** - the variation data built into the product form
- **Other platforms** - the schema.org variant format (`ProductGroup`)
- **Beanbox-style pages** - size and grind prices kept in script tables
  (one-time purchases only; each size takes the product's stock status,
  because that is all such pages publish)

Each variant's URL opens that exact variant, so clicking a row in the sheet
takes you straight to what you are checking.

A page that embeds no variant data keeps its single product-level row. If a
shop you track shows a wrong stock status that way, that site needs a reader
of its own.

### Category pages

Category pages are read from the shop's published product list (the
schema.org `ItemList` many shops include for search engines) when there is
one - exact, and unaffected by how the grid looks - and otherwise from the
product tiles on the page.

A plain `list:` line reads the category grid itself: one row per product,
one page visit, fast. A `list:variants:` line also opens every product on the
grid to read its variants, because a grid cannot show which sizes are in
stock or what each costs. That is one page visit per product, with the
politeness delay between them; `LISTING_MAX_PRODUCTS` (default 100) stops
one huge category from running for hours.

Without `variants:`, stock for a product with options comes from the shop's
headline data, which can say "out of stock" while some sizes are available.
Use `variants:` wherever stock matters.

## The sheet

| Tab | What it is for |
|---|---|
| **Latest** | Open this one. Today's rows only, rewritten each run, in `competitors.txt` order. Each product's variants sit together as a shaded block, and **change since last run** says what moved: `price down 2.00 (was 18.99)`, `went out of stock`, `back in stock`, `new`. |
| **Results** | The full history - every run appended. For tracking a price over time. |

A product's name is written once, in a single tall cell beside all of its
variant rows, rather than repeated on every row. Google Sheets cannot sort a
range containing merged cells, and filtering on the product column only
matches each product's first row - filter on variant or URL instead, or set
`MERGE_PRODUCT_NAMES=false` in `.env` to repeat the name on every row.
| **Alerts** | Every price drop and stock-out, one row per variant. |

The alert email merges identical changes to one product's variants, so a
coffee selling out in all 21 size-and-grind options is one line, not 21. The
Alerts tab still lists each one.

Row volume grows with variants: a product with 21 options writes 21 history
rows a run. That is fine for Google Sheets for a long time, but if the
Results tab ever gets slow, archive old rows to a separate tab.

## Testing the alerts

A pretend shop runs on this computer so you can change prices yourself and
watch the alerts fire. It never touches your real history: test runs write
to separate **TEST Results**, **TEST Alerts** and **TEST Latest** tabs, keep
their own comparison file, and test emails are marked `[TEST]`.

1. Start the shop in its own PowerShell window and leave it open:
   ```
   .\start_testshop.ps1
   ```
   Its admin page opens at http://localhost:8765/admin.
2. In another window, run a test check once to record the current prices:
   ```
   .\run.ps1 -Test
   ```
3. On the admin page, change a price, sale price or stock box and click
   **Save changes** - or click **Simulate some changes**.
4. Run `.\run.ps1 -Test` again. The changes appear in the TEST tabs, and
   anything that alerts is emailed.

What alerts: **any price change** - down or up, including a sale price
starting or ending - and something going **from in stock to out of stock**.
The Alerts tab's **change** column shows the difference: +9.99 in green,
-7.89 in red. Set `ALERT_ON_PRICE_RISE=false` to alert on drops only.
Back-in-stock alerts only with `ALERT_ON_BACK_IN_STOCK=true`.

**What survives a redesign** (measured with the test shop's layouts):
published product data and variant data are unaffected by new HTML and CSS;
pages built by JavaScript are read through the browser; a site rule in
`selectors.json` is recovered by adaptive mode, but only once the page has
really changed (the product-name rule stops matching), and a guess never
overrules a price the shop publishes. What does not survive: a category page
whose product grid is replaced with unrecognisable markup and no published
list - list those product pages directly instead.

**Testing a redesign.** The admin page's **Shop layout** switch rebuilds the
same products four ways - the original, a redesign with new HTML and CSS, a
redesign with no product data at all, and a page built by JavaScript - so you
can see what the monitor still reads when a competitor changes their site.
Run a test once on the original layout first: that is when adaptive mode
learns the page. Only one copy of the shop can run at a time.

Close the shop's window when finished. The TEST tabs can be deleted at any
time; the next test run recreates them.

## Speed

Each page is fetched the cheapest way that still gives a trustworthy result:

1. **A fast request** that impersonates Chrome. Most shops put their product
   data straight into the page they send, so this takes under a second.
2. **The stealth browser**, only if the fast request was refused or came back
   without product data (a bot check, or a page built by JavaScript). One
   browser is started for the whole run and reused, not relaunched per page.

A fast result is only accepted when it contains real product data, so a
blocked or empty page never turns into a misleading row.

Most of a run's time is now the politeness delay (`DELAY_BETWEEN_URLS`),
which only applies between pages on the **same** site. Lowering it speeds
things up but makes the tool look less like a person browsing.

Settings in `.env`: `FAST_MODE` (default `true`), `NETWORK_IDLE` (default
`false`), `HTTP_TIMEOUT_SECONDS` (default `20`).

## Category pages

A line in `competitors.txt` can be a single product page or a whole category
page. You do not have to mark which - it is worked out from the page.

A category page produces **one row per product**, and each row carries that
product's own link rather than the category's. That matters: the snapshot,
the alerts and the sheet history are all keyed on the URL, so without a
distinct link per product a price drop could not be traced to the product it
belongs to.

Force listing mode for a category page that is misread as one product:

```
list:https://example-shop.com/collections/all
```

Two limits worth knowing:

- Only the **first page** of a category is read. Paginated shops need each
  page listed, or a "show all" URL.
- A category grid usually shows less than a product page. Where a shop hides
  stock status on the grid, that column reads `Unknown` for those rows even
  though the product page would have shown it.

