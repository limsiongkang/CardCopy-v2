"""
Competitor price and stock monitor.

Run it by hand with:      run.ps1
Or test without writing:  run.ps1 -DryRun

What it does, in order:
  1. reads competitors.txt
  2. fetches each page through Scrapling's stealth browser and your proxy
  3. extracts name, price, sale price and stock status
  4. loads the previous run from the Google Sheet
  5. appends today's rows to the Data tab
  6. writes any price drops or stock-outs to the Alerts tab
  7. emails you a summary
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback

import config
import scraper


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------

class Logger:
    """Prints to the screen and to a dated file in logs/."""

    def __init__(self, to_file: bool = True):
        self.handle = None
        if to_file:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = config.LOG_DIR / f"run-{dt.date.today().isoformat()}.log"
            self.handle = open(path, "a", encoding="utf-8")
            self.path = path

    def __call__(self, message: str = "") -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        if not message:
            line = ""
        else:
            # Keep multi-line blocks aligned under the timestamp.
            parts = str(message).splitlines() or [""]
            indented = [f"{stamp}  {parts[0]}"]
            indented += [" " * 10 + part for part in parts[1:]]
            line = "\n".join(indented)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode(), flush=True)
        if self.handle:
            self.handle.write(line + "\n")
            self.handle.flush()

    def close(self):
        if self.handle:
            self.handle.close()


# ----------------------------------------------------------------------
# Local snapshot - a safety net, not the source of truth
# ----------------------------------------------------------------------
#
# The Google Sheet is what runs are compared against, because it survives
# losing this machine and it is what you actually look at. But if the sheet
# is briefly unreadable - an outage, a revoked key, someone reorganising the
# tabs - a run with no baseline raises no alerts at all and reports nothing
# wrong. That silent failure is worse than a stale comparison, so every run
# also writes a local copy to fall back on.

def save_snapshot(products, run_date: str, log=print) -> None:
    try:
        payload = {
            "run_date": run_date,
            "products": {
                p.url: {
                    "product": p.name,
                    "variant": p.variant,
                    "price": p.price,
                    "sale_price": p.sale_price,
                    "in_stock": p.stock_text().lower(),
                }
                for p in products if not p.error
            },
        }
        config.SNAPSHOT_FILE.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - a backup failing must not fail the run
        log(f"  (could not write the local snapshot: {exc})")


def load_snapshot(log=print) -> tuple[str, dict]:
    if not config.SNAPSHOT_FILE.exists():
        return "", {}
    try:
        payload = json.loads(config.SNAPSHOT_FILE.read_text(encoding="utf-8"))
        return payload.get("run_date", ""), payload.get("products", {}) or {}
    except Exception as exc:  # noqa: BLE001
        log(f"  (local snapshot unreadable, treating as first run: {exc})")
        return "", {}


# ----------------------------------------------------------------------
# Comparing this run to the previous one
# ----------------------------------------------------------------------

def _money(value, currency: str = "") -> str:
    if value in (None, ""):
        return "-"
    prefix = f"{currency} " if currency else ""
    return f"{prefix}{float(value):,.2f}"


def find_alerts(products, snapshot: dict[str, dict], log=print) -> list[dict]:
    """
    Compare this run to the previous one.

    Two things raise an alert, exactly as specified:
      * the price a shopper pays today went DOWN
      * the product went from in stock to out of stock

    Products that failed to scrape are skipped entirely. A page that timed
    out is not evidence that a product went out of stock, and raising an
    alert for it would train you to ignore the alerts.
    """
    alerts: list[dict] = []

    for product in products:
        if product.error:
            continue

        previous = snapshot.get(product.url)
        if not previous:
            continue  # new product, nothing to compare against

        label = product.name or previous.get("product") or product.url
        variant = product.variant or previous.get("variant", "")

        # --- price drop ------------------------------------------------
        was_price = previous.get("sale_price")
        if was_price is None:
            was_price = previous.get("price")
        now_price = product.effective_price

        # Ignore sub-cent wobble from rounding or currency conversion.
        if (was_price is not None and now_price is not None
                and abs(now_price - was_price) >= 0.01):
            rose = now_price > was_price
            if not rose or config.ALERT_ON_PRICE_RISE:
                alerts.append({
                    "competitor": product.competitor,
                    "product": label,
                    "variant": variant,
                    "alert": "price rise" if rose else "price drop",
                    "was": f"{was_price:.2f}",
                    "now": f"{now_price:.2f}",
                    # Signed difference: +9.99 up, -7.89 down.
                    "change": round(now_price - was_price, 2),
                    "url": product.url,
                })

        # --- stock changes ---------------------------------------------
        was_in_stock = previous.get("in_stock", "").strip().lower()
        now_in_stock = product.stock_text().lower()

        if was_in_stock == "yes" and now_in_stock == "no":
            alerts.append({
                "competitor": product.competitor,
                "product": label,
                "variant": variant,
                "alert": "out of stock",
                "was": "in stock",
                "now": "out of stock",
                "url": product.url,
            })
        elif (config.ALERT_ON_BACK_IN_STOCK
              and was_in_stock == "no" and now_in_stock == "yes"):
            alerts.append({
                "competitor": product.competitor,
                "product": label,
                "variant": variant,
                "alert": "back in stock",
                "was": "out of stock",
                "now": "in stock",
                "url": product.url,
            })

    return alerts


# ----------------------------------------------------------------------
# Reporting to the screen
# ----------------------------------------------------------------------

def print_table(products, log):
    """
    One line per product. A coffee with 21 grind-and-size options is one
    line here; every variant is in the sheet.
    """
    import scraper

    groups: dict[str, list] = {}
    for p in products:
        groups.setdefault(p.page_url or p.url, []).append(p)

    log()
    log(f"  {'COMPETITOR':<18}PRODUCT")
    log(f"  {'-' * 18}{'-' * 70}")
    for rows in groups.values():
        first = rows[0]
        if first.error:
            log(f"  {first.competitor[:17]:<18}FAILED: {first.error[:70]}")
            continue
        log(f"  {first.competitor[:17]:<18}{scraper._summary(rows)}")
    variant_rows = sum(1 for p in products if p.variant)
    log()
    log(f"  {len(groups)} product(s), {len(products)} row(s)"
        + (f" including {variant_rows} variant row(s)" if variant_rows else ""))
    log()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def run(args) -> int:
    log = Logger(to_file=not args.no_log)
    exit_code = 0

    try:
        log("=" * 72)
        log(f"Competitor check starting - {dt.datetime.now():%Y-%m-%d %H:%M}")
        log("=" * 72)

        if args.dry_run:
            log("DRY RUN - nothing will be written to Google Sheets and no email sent.")
        if config.TEST_MODE:
            log("TEST MODE - reading the local test shop; writing only to the TEST tabs.")

        # --- settings ---------------------------------------------------
        problems = config.check(
            need_sheets=not args.dry_run,
            need_email=not (args.dry_run or args.no_email),
        )
        if problems:
            log()
            log("Before this can run, these need fixing:")
            for problem in problems:
                log(f"  * {problem}")
            log()
            return 2

        log()
        log(config.redacted_summary())
        log()

        # --- what to check ----------------------------------------------
        if args.url:
            # Same syntax as a competitors.txt line, so
            # --url "variants:https://..." works too.
            target = scraper.parse_target(args.url)
            if target is None:
                log(f"That is not a web address: {args.url}")
                return 2
            targets = [target]
        else:
            targets = scraper.read_targets()

        if not targets:
            log(f"No URLs found in {config.COMPETITORS_FILE.name}.")
            log("Add one product URL per line, then run this again.")
            return 2

        if args.limit:
            targets = targets[: args.limit]

        log(f"Checking {len(targets)} product page(s).")
        log()

        # --- previous run -----------------------------------------------
        workbook = data_tab = alerts_tab = None
        snapshot: dict[str, dict] = {}
        previous_date = ""

        if not args.dry_run:
            import sheets

            log("Connecting to Google Sheets...")
            client = sheets.connect()
            workbook = sheets.open_workbook(client, log=log)
            data_tab, alerts_tab = sheets.ensure_tabs(workbook, log=log)
            try:
                previous_date, snapshot = sheets.previous_run(data_tab)
            except sheets.SheetsError as exc:
                log(f"  Could not read the previous run from the sheet: {exc}")
                snapshot = {}

            if snapshot:
                log(f"  Previous run was {previous_date} with {len(snapshot)} product(s).")
            else:
                # Fall back to the local copy rather than silently comparing
                # against nothing and reporting "no changes".
                previous_date, snapshot = load_snapshot(log=log)
                if snapshot:
                    log(f"  Sheet had no history; using the local snapshot "
                        f"from {previous_date} ({len(snapshot)} product(s)).")
                else:
                    log("  No previous run found - this is the first one, so no alerts yet.")
            log()

        # --- scrape -------------------------------------------------------
        log("Reading competitor pages...")
        products = scraper.scrape_all(targets, log=log)

        ok = [p for p in products if not p.error]
        failed = [p for p in products if p.error]
        # Count products, not rows: one coffee in 21 sizes is one product.
        # Variants of a product share its page_url.
        product_groups: dict[str, bool] = {}
        for p in products:
            key = p.page_url or p.url
            product_groups[key] = product_groups.get(key, False) or bool(p.error)
        failed_products = sum(1 for has_error in product_groups.values() if has_error)
        stats = {"checked": len(product_groups),
                 "ok": len(product_groups) - failed_products,
                 "failed": failed_products}

        print_table(products, log)
        log(f"Read {stats['ok']} of {stats['checked']} products successfully "
            f"({len(ok)} row(s) including variants).")

        # --- compare ------------------------------------------------------
        alerts = find_alerts(products, snapshot, log=log)

        if snapshot:
            if alerts:
                log()
                log(f"{len(alerts)} change(s) since {previous_date}:")
                for alert in alerts:
                    variant = f" ({alert['variant']})" if alert.get("variant") else ""
                    log(f"  {alert['alert']}: {alert['competitor']} - {alert['product'][:50]}{variant}")
                    change = alert.get("change")
                    log(f"      {alert['was']}  ->  {alert['now']}"
                        + (f"   ({change:+.2f})" if isinstance(change, (int, float)) else ""))
            else:
                log(f"No price drops or stock-outs since {previous_date}.")

        if args.dry_run:
            log()
            log("Dry run finished. Nothing was written and no email was sent.")
            return 0

        # --- write --------------------------------------------------------
        run_date = sheets.today()

        # Save the local copy BEFORE touching Google, not after. If the sheet
        # write fails, this is the one run where the fallback actually matters
        # - and writing it afterwards means it never gets saved precisely then,
        # leaving tomorrow to compare against older and older data.
        save_snapshot(products, run_date, log=log)

        log()
        log("Writing to Google Sheets...")
        written = sheets.append_data(data_tab, products, run_date)
        log(f"  Added {written} row(s) to the '{config.DATA_TAB}' tab.")

        if alerts:
            sheets.append_alerts(alerts_tab, alerts, run_date)
            log(f"  Added {len(alerts)} row(s) to the '{config.ALERTS_TAB}' tab.")

        sheets.polish_all(workbook, data_tab, alerts_tab, log=log)
        log("  Reapplied the table formatting.")

        try:
            sheets.write_latest(workbook, products, snapshot, log=log)
            log(f"  Refreshed the '{config.LATEST_TAB}' tab.")
        except Exception as exc:  # noqa: BLE001 - history is already saved
            log(f"  (could not refresh the '{config.LATEST_TAB}' tab: {exc})")

        # --- email --------------------------------------------------------
        if args.no_email:
            log("  Email skipped (--no-email).")
        elif alerts or args.always_email:
            import notify

            log()
            log("Sending the summary email...")
            try:
                message = notify.build_message(
                    alerts, run_date, stats, sheet_url=workbook.url if workbook else ""
                )
                notify.send(message, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"  Email failed: {exc}")
                exit_code = 1
        else:
            log("  Nothing changed, so no email was sent.")
            log("  (Use --always-email if you want a daily email either way.)")

        if failed:
            log()
            log(f"Note: {len(failed)} page(s) could not be read. They are in the sheet")
            log("marked ERROR so you can see which ones need attention.")
            exit_code = exit_code or 1

        log()
        log("Done.")
        return exit_code

    except KeyboardInterrupt:
        log("\nStopped by you.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log()
        log(f"The run stopped with an unexpected problem: {exc}")
        log()
        for line in traceback.format_exc().splitlines():
            log(f"    {line}")
        return 1
    finally:
        log.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check competitor prices and stock, log them to Google Sheets."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and show the results, but write nothing and send nothing.")
    parser.add_argument("--no-email", action="store_true",
                        help="Write to the sheet but do not send email.")
    parser.add_argument("--always-email", action="store_true",
                        help="Send the summary even when nothing changed.")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Only check the first N URLs. Useful for testing.")
    parser.add_argument("--url", metavar="URL",
                        help="Check one URL instead of competitors.txt.")
    parser.add_argument("--no-log", action="store_true",
                        help="Do not write a log file.")
    parser.add_argument("--test", action="store_true",
                        help="Check the local test shop and write to the TEST tabs only.")
    args = parser.parse_args()
    if args.test:
        problem = enter_test_mode(args)
        if problem:
            print(problem)
            return 2
    return run(args)


def enter_test_mode(args) -> str:
    """
    Point this run at the local test shop, well away from the real data.

    Returns a message if the run cannot go ahead, else an empty string.
    """
    import socket

    config.TEST_MODE = True
    config.DATA_TAB = "TEST Results"
    config.ALERTS_TAB = "TEST Alerts"
    config.LATEST_TAB = "TEST Latest"
    # Its own comparison file too, or the next real run would compare
    # against test-shop prices.
    config.SNAPSHOT_FILE = config.DATA_DIR / "test_last_run.json"
    config.CLIENT_NAME = f"[TEST] {config.CLIENT_NAME}".strip()
    # A proxy out on the internet cannot reach a shop running on this computer.
    config.PROXY_ENABLED = False
    # It is our own shop; no need to be gentle with it.
    config.REQUEST_DELAY_SECONDS = 0.5
    # The test shop's own page list and site rules, kept beside it. The
    # rules are written for the Classic layout, which is what lets adaptive
    # mode be tested when the layout is switched.
    config.COMPETITORS_FILE = config.ROOT / "testshop" / "competitors.txt"
    config.SELECTORS_FILE = config.ROOT / "testshop" / "selectors.json"

    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=3):
            pass
    except OSError:
        return ("The test shop is not running.\n"
                "Start it first, in a separate PowerShell window:\n"
                "    .\\start_testshop.ps1\n"
                "then run this again.")
    return ""


if __name__ == "__main__":
    sys.exit(main())
