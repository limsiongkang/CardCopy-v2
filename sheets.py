"""
Reading from and writing to the Google Sheet.

The sheet has two tabs:

  Data   - one row per product per run: date, competitor, product, price,
           sale price, in stock, URL.
  Alerts - one row each time a price drops or a product goes out of stock.

"The previous run" means the most recent date already present on the Data
tab at the moment this run starts. So if you run twice in one day, the
second run compares itself against the first.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlparse

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsError(Exception):
    """Something went wrong talking to Google, explained in plain English."""


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------

def connect() -> gspread.Client:
    if config.PREFER_IPV4:
        import netfix
        netfix.prefer_ipv4()
    key_file = config.service_account_path()
    if not key_file.exists():
        raise SheetsError(
            f"The Google key file is missing.\n"
            f"  Expected it at: {key_file}\n"
            f"  See step 3 of README.md for how to create it."
        )
    try:
        creds = Credentials.from_service_account_file(str(key_file), scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(
            f"Could not use the Google key file at {key_file}.\n"
            f"  Google said: {exc}"
        ) from exc


def robot_email() -> str:
    """The service account's own address - the one the sheet must be shared with."""
    import json

    try:
        data = json.loads(config.service_account_path().read_text(encoding="utf-8"))
        return data.get("client_email", "(unknown)")
    except Exception:  # noqa: BLE001
        return "(unknown)"


# ----------------------------------------------------------------------
# Finding or creating the workbook
# ----------------------------------------------------------------------

def open_workbook(client: gspread.Client, log=print):
    """Open the sheet by ID if we have one, else by name, else create it."""
    if config.GOOGLE_SHEET_ID:
        try:
            return client.open_by_key(config.GOOGLE_SHEET_ID)
        except gspread.SpreadsheetNotFound:
            raise SheetsError(
                f"No sheet found with the ID in your .env file.\n"
                f"  GOOGLE_SHEET_ID = {config.GOOGLE_SHEET_ID}\n"
                f"  Either fix that ID or clear it so a new sheet is created."
            ) from None
        except gspread.exceptions.APIError as exc:
            raise SheetsError(
                f"Google refused access to that sheet.\n"
                f"  Make sure the sheet is shared with {robot_email()} as an Editor.\n"
                f"  Google said: {exc}"
            ) from exc

    try:
        return client.open(config.SHEET_TITLE)
    except gspread.SpreadsheetNotFound:
        pass

    log(f"  No sheet called '{config.SHEET_TITLE}' yet - creating it.")
    try:
        workbook = client.create(config.SHEET_TITLE)
    except gspread.exceptions.APIError as exc:
        if "storage quota" not in str(exc).lower():
            raise
        # Google gives service accounts no Drive storage of their own, so the
        # robot cannot create a file. You make it once; it edits it forever.
        raise SheetsError(
            "A service account cannot create a Google Sheet - Google gives robot\n"
            "  accounts no Drive storage. Make the sheet yourself, once:\n"
            "\n"
            f"    1. Open https://sheets.new and name it:  {config.SHEET_TITLE}\n"
            "    2. Click Share, paste in this address, set it to Editor, Send:\n"
            f"\n         {robot_email()}\n\n"
            "    3. Copy the long code from the sheet's web address (between /d/\n"
            "       and /edit) into your .env as GOOGLE_SHEET_ID=\n"
            "    4. Run the monitor again."
        ) from exc

    # A sheet created by the robot lives in the robot's Drive and is invisible
    # to you until shared. Do that immediately.
    if config.ALERT_EMAIL_TO:
        try:
            workbook.share(config.ALERT_EMAIL_TO, perm_type="user", role="writer")
            log(f"  Shared it with {config.ALERT_EMAIL_TO}.")
        except Exception as exc:  # noqa: BLE001
            log(f"  Created the sheet but could not share it automatically: {exc}")
            log(f"  Open it yourself at {workbook.url} using the robot account.")

    log(f"  Sheet URL: {workbook.url}")
    log(f"  Add this line to your .env so it is reused every day:")
    log(f"      GOOGLE_SHEET_ID={workbook.id}")
    return workbook


# ----------------------------------------------------------------------
# Presentation
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# One look for every tab
# ----------------------------------------------------------------------
#
# Formatting is decided by column NAME, not position, so a column that
# appears on several tabs (price, in stock, URL...) is always the same width,
# alignment and colours wherever it is.
#
# Colour means the same thing everywhere:
#   green  - up / available   (in stock, back in stock, price went up, +9.99)
#   red    - down / sold out  (out of stock, price went down, -7.89)
#   blue   - new product
#   grey   - no information   (stock unknown, page could not be read)

_COLOURS = {
    "green":  ({"red": 0.85, "green": 0.94, "blue": 0.87}, {"red": 0.09, "green": 0.42, "blue": 0.18}),
    "red":    ({"red": 0.99, "green": 0.88, "blue": 0.88}, {"red": 0.70, "green": 0.11, "blue": 0.11}),
    "blue":   ({"red": 0.87, "green": 0.92, "blue": 0.99}, {"red": 0.10, "green": 0.32, "blue": 0.70}),
    "grey":   ({"red": 0.93, "green": 0.93, "blue": 0.94}, {"red": 0.38, "green": 0.40, "blue": 0.43}),
}

# Words that colour a cell, per column. Checked top to bottom and the first
# match wins - so a cell saying "price up 1.00; went out of stock" is red.
_STATUS_RULES = {
    "in stock": [
        ("EQ", "yes", "green"),
        ("EQ", "no", "red"),
        ("EQ", "unknown", "grey"),
    ],
    "alert": [
        ("EQ", "out of stock", "red"),
        ("EQ", "price drop", "red"),
        ("EQ", "back in stock", "green"),
        ("EQ", "price rise", "green"),
    ],
    "change": [
        ("GT", "0", "green"),
        ("LT", "0", "red"),
    ],
    "change since last run": [
        ("CONTAINS", "could not be read", "grey"),
        ("CONTAINS", "went out of stock", "red"),
        ("CONTAINS", "price down", "red"),
        ("CONTAINS", "back in stock", "green"),
        ("CONTAINS", "price up", "green"),
        ("EQ", "new", "blue"),
    ],
}

# The colour key shown beside the data on the Latest tab.
_LEGEND = [
    ("green", "Up: in stock, back in stock, price went up"),
    ("red", "Down: out of stock, price went down"),
    ("blue", "New product"),
    ("grey", "Unknown / page could not be read"),
]

_WIDTH = {
    "date": 125, "competitor": 160, "product": 280, "variant": 190,
    "price": 90, "sale price": 90, "in stock": 80,
    "alert": 115, "was": 90, "now": 90, "change": 85,
    "change since last run": 250, "url": 330,
}
_MONEY = {"price", "sale price"}                 # two decimals, right aligned
_TEXT = {"product", "variant", "was", "now"}     # stored exactly as written
_RIGHT = {"was", "now"}                          # line up with the price columns
_CENTRE = {"in stock", "alert"}                  # short status words
_SIGNED = {"change"}                             # +9.99 / -7.89
_LINKS = {"competitor", "url"}


def ensure_default_grid(worksheet) -> None:
    """
    Give a tab the size of an ordinary new sheet: columns A-Z, 1000 rows.

    Tabs created by the API come out exactly as wide as asked for - eight
    columns, A to H - which looks cramped and leaves nowhere to add notes of
    your own beside the data.
    """
    try:
        if worksheet.col_count < 26:
            worksheet.add_cols(26 - worksheet.col_count)
        if worksheet.row_count < 1000:
            worksheet.add_rows(1000 - worksheet.row_count)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def polish(workbook, worksheet, headers: list[str], log=print) -> None:
    """
    Apply the one house look to a tab. Everything follows from the column
    names in `headers` - see "One look for every tab" above.

    It also strips styling earlier versions applied (header bands, striping,
    filter buttons, borders), so older sheets are cleaned up too. Safe to
    re-run on every run.
    """
    sheet_id = worksheet.id
    ensure_default_grid(worksheet)
    names = [h.strip().lower() for h in headers]

    # --- remove striping, old colour rules and the filter ----------------
    clear: list[dict] = [{"clearBasicFilter": {"sheetId": sheet_id}}]
    try:
        meta = workbook.fetch_sheet_metadata()
        for sheet in meta.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != sheet_id:
                continue
            for band in sheet.get("bandedRanges", []) or []:
                clear.append({"deleteBanding": {"bandedRangeId": band["bandedRangeId"]}})
            rules = sheet.get("conditionalFormats", []) or []
            # Delete from the end so earlier indexes stay valid.
            for index in range(len(rules) - 1, -1, -1):
                clear.append({"deleteConditionalFormatRule":
                              {"sheetId": sheet_id, "index": index}})
    except Exception:  # noqa: BLE001
        pass
    for request in clear:
        # One at a time: clearing a filter that is not there is an error, and
        # it must not take the other clean-up requests down with it.
        try:
            workbook.batch_update({"requests": [request]})
        except Exception:  # noqa: BLE001
            pass

    def column(index: int) -> dict:
        return {"sheetId": sheet_id, "startRowIndex": 1,
                "startColumnIndex": index, "endColumnIndex": index + 1}

    requests: list[dict] = [
        # Header: bold and frozen, otherwise plain - across the whole row.
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                           "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
        # Body: plain cells, every one vertically centred (which is also what
        # puts a merged product name in the middle of its block).
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,borders,padding,textFormat,"
                      "verticalAlignment,horizontalAlignment)"}},
    ]

    for index, name in enumerate(names):
        width = _WIDTH.get(name)
        if width:
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": index, "endIndex": index + 1},
                "properties": {"pixelSize": width}, "fields": "pixelSize"}})

        if name in _MONEY:
            fmt = {"horizontalAlignment": "RIGHT",
                   "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}
        elif name in _SIGNED:
            # A real number, shown with its sign; it can still be sorted and summed.
            fmt = {"horizontalAlignment": "RIGHT",
                   "numberFormat": {"type": "NUMBER", "pattern": "+#,##0.00;-#,##0.00;0.00"}}
        elif name in _TEXT:
            # Explicit, because a column inserted beside a money column
            # inherits its right alignment and number format.
            fmt = {"horizontalAlignment": "RIGHT" if name in _RIGHT else "LEFT",
                   "numberFormat": {"type": "TEXT"}}
        elif name in _CENTRE:
            fmt = {"horizontalAlignment": "CENTER"}
        else:
            fmt = None
        if fmt:
            requests.append({"repeatCell": {
                "range": column(index), "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(" + ",".join(fmt) + ")"}})

        if name in _LINKS:
            # The body reset clears text formatting, link colour included.
            requests.append({"repeatCell": {
                "range": column(index),
                "cell": {"userEnteredFormat": {"textFormat": {
                    "underline": True,
                    "foregroundColor": {"red": 0.07, "green": 0.33, "blue": 0.80}}}},
                "fields": "userEnteredFormat.textFormat"}})

    # --- status colours, in priority order ----------------------------------
    priority = 0
    for index, name in enumerate(names):
        for condition, text, colour in _STATUS_RULES.get(name, []):
            background, foreground = _COLOURS[colour]
            requests.append({"addConditionalFormatRule": {"index": priority, "rule": {
                "ranges": [column(index)],
                "booleanRule": {
                    "condition": {"type": {"EQ": "TEXT_EQ", "CONTAINS": "TEXT_CONTAINS",
                                           "GT": "NUMBER_GREATER", "LT": "NUMBER_LESS"}[condition],
                                  "values": [{"userEnteredValue": text}]},
                    "format": {"backgroundColor": background,
                               "textFormat": {"bold": True, "foregroundColor": foreground}}}}}})
            priority += 1

    try:
        workbook.batch_update({"requests": requests})
    except Exception as exc:  # noqa: BLE001 - never fail a run over cosmetics
        log(f"  (could not fully format '{worksheet.title}': {exc})")


def _legend_requests(worksheet, column: int) -> tuple[list[list[str]], list[dict]]:
    """Values and formatting for the colour key, placed in `column`."""
    values = [["Colour key"]] + [[label] for _, label in _LEGEND]
    requests: list[dict] = [{"updateDimensionProperties": {
        "range": {"sheetId": worksheet.id, "dimension": "COLUMNS",
                  "startIndex": column, "endIndex": column + 1},
        "properties": {"pixelSize": 260}, "fields": "pixelSize"}}]
    for row, (colour, _) in enumerate(_LEGEND, start=1):
        background, foreground = _COLOURS[colour]
        requests.append({"repeatCell": {
            "range": {"sheetId": worksheet.id, "startRowIndex": row, "endRowIndex": row + 1,
                      "startColumnIndex": column, "endColumnIndex": column + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": background,
                "textFormat": {"bold": True, "foregroundColor": foreground}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
    return values, requests


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def linkify_tab(worksheet, competitor_col: int = 1, url_col: int = 6, log=print) -> int:
    """
    Normalise the competitor and URL columns across every row, old ones too.

    The competitor cell becomes a link to the shop's home page, so the column
    reads as a name rather than an address. The URL cell is left as the plain
    address - Sheets auto-links it, so it is still clickable, and the cell
    holds the real URL rather than formula text.

    get_all_values() returns display text, so a cell that is already a link
    reads back as its label and is rewritten identically. Re-running changes
    nothing, which makes this safe to call on every run.
    """
    try:
        rows = worksheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        log(f"  (could not read '{worksheet.title}' to add links: {exc})")
        return 0

    if len(rows) < 2:
        return 0

    competitors: list[list[str]] = []
    urls: list[list[str]] = []
    for row in rows[1:]:
        url = row[url_col] if len(row) > url_col else ""
        name = row[competitor_col] if len(row) > competitor_col else ""
        if url:
            competitors.append([_link(_home_page(url), name)])
        else:
            competitors.append([name])
        urls.append([url])

    last = len(rows)
    comp_range = f"{_column_letter(competitor_col)}2:{_column_letter(competitor_col)}{last}"
    url_range = f"{_column_letter(url_col)}2:{_column_letter(url_col)}{last}"

    try:
        worksheet.update(competitors, comp_range, value_input_option="USER_ENTERED")
        worksheet.update(urls, url_range, value_input_option="USER_ENTERED")
    except Exception as exc:  # noqa: BLE001
        log(f"  (could not add links to '{worksheet.title}': {exc})")
        return 0
    return len(urls)


def polish_all(workbook, data_tab, alerts_tab, log=print) -> None:
    """Apply the house look to the history and alert tabs."""
    linkify_tab(data_tab, url_col=config.DATA_HEADERS.index("URL"), log=log)
    linkify_tab(alerts_tab, url_col=config.ALERT_HEADERS.index("URL"), log=log)
    polish(workbook, data_tab, config.DATA_HEADERS, log=log)
    polish(workbook, alerts_tab, config.ALERT_HEADERS, log=log)


def _only_missing_columns(current: list[str], wanted: list[str]) -> bool:
    """True if `current` is `wanted` with some columns left out, order intact."""
    remaining = iter(wanted)
    return bool(current) and len(current) < len(wanted) \
        and all(name in remaining for name in current)


def ensure_tabs(workbook, log=print):
    """Make sure both tabs exist with the right column headings."""
    existing = {ws.title: ws for ws in workbook.worksheets()}

    def build(title: str, headers: list[str]):
        if title in existing:
            worksheet = existing[title]
            current = [c.strip().lower() for c in worksheet.row_values(1)]
            wanted = [h.lower() for h in headers]
            if current == wanted:
                return worksheet

            if _only_missing_columns(current, wanted):
                # An older layout: same columns in the same order, some new
                # ones missing. Insert each as a real column so every existing
                # row shifts across with it - rewriting the header alone
                # would put each old value under the wrong heading.
                added = []
                for position, name in enumerate(wanted):
                    if position >= len(current) or current[position] != name:
                        worksheet.insert_cols([[headers[position]]], col=position + 1)
                        current.insert(position, name)
                        added.append(headers[position])
                log(f"  Added {', '.join(repr(a) for a in added)} to the '{title}' tab "
                    f"(existing rows kept in place).")
                return worksheet

            if not any(current):
                worksheet.update([headers], "A1")
                return worksheet

            # Some other layout with data under it. Relabelling it would
            # silently misfile every value, so stop and say why instead.
            raise SheetsError(
                f"The '{title}' tab has column headings this version does not "
                f"recognise ({', '.join(current)}). Rename the tab to keep its "
                f"data, and the next run will start a fresh '{title}' tab."
            )

        worksheet = workbook.add_worksheet(title=title, rows=1000, cols=max(26, len(headers)))
        worksheet.update([headers], "A1")
        worksheet.freeze(rows=1)
        worksheet.format("A1:Z1", {"textFormat": {"bold": True}})
        log(f"  Created the '{title}' tab.")
        return worksheet

    data_tab = build(config.DATA_TAB, config.DATA_HEADERS)
    alerts_tab = build(config.ALERTS_TAB, config.ALERT_HEADERS)

    # Google always creates a workbook with a tab called "Sheet1". Remove it
    # once our real tabs exist so the sheet is not confusing to open.
    for title, worksheet in existing.items():
        if title in ("Sheet1", "Sheet 1") and len(workbook.worksheets()) > 2 \
                and title not in (config.DATA_TAB, config.ALERTS_TAB, config.LATEST_TAB):
            try:
                workbook.del_worksheet(worksheet)
            except Exception:  # noqa: BLE001
                pass

    return data_tab, alerts_tab


# ----------------------------------------------------------------------
# Reading the previous run
# ----------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def previous_run(data_tab) -> tuple[str, dict[str, dict]]:
    """
    Return (date_of_previous_run, {url: row}) for the most recent date
    already on the Data tab. Empty dict on the very first run.
    """
    try:
        rows = data_tab.get_all_values()
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(f"Could not read the Data tab: {exc}") from exc

    if len(rows) < 2:
        return "", {}

    header = [h.strip().lower() for h in rows[0]]

    def column(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    idx_date = column("date")
    idx_price = column("price")
    idx_sale = column("sale price")
    idx_stock = column("in stock")
    idx_url = column("url")
    idx_product = column("product")
    idx_variant = column("variant")

    if min(idx_date, idx_url) < 0:
        return "", {}

    dates = {r[idx_date].strip() for r in rows[1:] if len(r) > idx_date and r[idx_date].strip()}
    if not dates:
        return "", {}
    last_date = max(dates)

    snapshot: dict[str, dict] = {}
    for row in rows[1:]:
        if len(row) <= max(idx_date, idx_url):
            continue
        if row[idx_date].strip() != last_date:
            continue
        url = row[idx_url].strip()
        if not url:
            continue
        snapshot[url] = {
            "product": row[idx_product].strip() if 0 <= idx_product < len(row) else "",
            "variant": row[idx_variant].strip() if 0 <= idx_variant < len(row) else "",
            "price": _to_float(row[idx_price]) if 0 <= idx_price < len(row) else None,
            "sale_price": _to_float(row[idx_sale]) if 0 <= idx_sale < len(row) else None,
            "in_stock": row[idx_stock].strip() if 0 <= idx_stock < len(row) else "",
        }

    return last_date, snapshot


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

def _link(url: str, label: str) -> str:
    """
    A clickable cell.

    Sheets only turns a cell into a link when it is a HYPERLINK formula (or a
    bare URL it decides to auto-link). Writing the formula makes it certain.
    get_all_values() reads back the DISPLAY text, so the label is what the
    comparison logic sees - which is why the label stays the literal value.
    """
    if not url:
        return label or ""
    safe_url = str(url).replace('"', "%22")
    safe_label = str(label if label else url).replace('"', "'")
    return f'=HYPERLINK("{safe_url}","{safe_label}")'


def _home_page(url: str) -> str:
    """https://shop.example.com/a/b -> https://shop.example.com"""
    try:
        parts = urlparse(str(url))
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _text(value) -> str:
    """
    A cell that must stay exactly the text it is.

    Rows are written in USER_ENTERED mode so the competitor link formula
    works - but in that mode Sheets also *interprets* everything else. A
    variant called "3/4" becomes a date, "250" becomes a number shown as
    250.00, and a product name beginning with "=" becomes a formula. That
    last one matters: names come from competitors' web pages, so a hostile
    page could put a formula into this sheet. A leading apostrophe tells
    Sheets "this is text"; it is not displayed and is not part of the value.
    """
    value = "" if value is None else str(value)
    return "'" + value if value else ""


def _merge_requests(sheet_id: int, column: int, blocks: list[tuple[int, int]]) -> list[dict]:
    """mergeCells requests for each block of 2+ rows, 0-based [start, end)."""
    return [
        {"mergeCells": {"range": {"sheetId": sheet_id,
                                  "startRowIndex": start, "endRowIndex": end,
                                  "startColumnIndex": column, "endColumnIndex": column + 1},
                        "mergeType": "MERGE_ALL"}}
        for start, end in blocks if end - start > 1
    ]


def _product_blocks(products, first_row: int) -> list[tuple[int, int]]:
    """
    Row ranges, 0-based [start, end), of consecutive rows belonging to the
    same product page. Variants of one product are always written together.
    """
    blocks: list[tuple[int, int]] = []
    current, start = None, first_row
    for offset, product in enumerate(products):
        key = product.page_url or product.url
        if key != current:
            if current is not None:
                blocks.append((start, first_row + offset))
            current, start = key, first_row + offset
    if current is not None:
        blocks.append((start, first_row + len(products)))
    return blocks


def product_row(product, run_date: str) -> list:
    # Stock is written lower case ("yes"/"no") to match the rows already on
    # the Results tab. The comparison logic lower-cases before comparing, so
    # old and new rows read the same either way.
    if product.error:
        stock = "unknown"
    else:
        stock = product.stock_text().lower()

    return [
        run_date,
        _link(_home_page(product.url), product.competitor),
        _text(product.name or ("ERROR: " + product.error if product.error else "")),
        _text(product.variant),
        product.price if product.price is not None else "",
        product.sale_price if product.sale_price is not None else "",
        stock,
        # The plain URL, not a HYPERLINK formula. Sheets auto-links a bare URL
        # so it stays clickable, and the cell holds the real address - which
        # copies, exports and reads back cleanly instead of showing formula text.
        product.url,
    ]


def append_data(data_tab, products, run_date: str) -> int:
    rows = [product_row(p, run_date) for p in products]
    if not rows:
        return 0
    response = data_tab.append_rows(rows, value_input_option="USER_ENTERED")

    if config.MERGE_PRODUCT_NAMES:
        # Where did the rows land? The API reports it, e.g. "Results!A220:H437".
        import re
        updated = ((response or {}).get("updates") or {}).get("updatedRange", "")
        match = re.search(r"![A-Z]+(\d+):", updated)
        if match:
            first_row = int(match.group(1)) - 1                  # 0-based
            column = config.DATA_HEADERS.index("product")
            requests = _merge_requests(data_tab.id, column,
                                       _product_blocks(products, first_row))
            if requests:
                try:
                    data_tab.spreadsheet.batch_update({"requests": requests})
                except Exception:  # noqa: BLE001 - cosmetic; the data is written
                    pass
    return len(rows)


def append_alerts(alerts_tab, alerts: list[dict], run_date: str) -> int:
    rows = [
        [
            run_date,
            _link(_home_page(a["url"]), a["competitor"]),
            _text(a["product"]),
            _text(a.get("variant", "")),
            a["alert"],
            # Text, so "18.00" stays "18.00" instead of becoming the number 18.
            _text(a["was"]),
            _text(a["now"]),
            a["change"] if isinstance(a.get("change"), (int, float)) else "",
            a["url"],
        ]
        for a in alerts
    ]
    if rows:
        alerts_tab.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)



# ----------------------------------------------------------------------
# The "Latest" tab - today's picture, grouped by product
# ----------------------------------------------------------------------
#
# Results is the full history: every run appended, which makes it the right
# place to track a price over time and the wrong place to check today's
# prices across 20 variants. Latest is rewritten on every run with just the
# current rows, in the order competitors.txt lists them, each product's
# variants kept together and shaded as one block, and a column saying what
# changed since the last run.

def _page_of(url: str) -> str:
    """https://shop/p?variant=3#x -> https://shop/p"""
    return url.split("#", 1)[0].split("?", 1)[0]


def change_text(product, previous: dict | None, snapshot: dict,
                known_pages: set[str] | None = None) -> str:
    """Plain-English difference between this row and the previous run."""
    if product.error:
        return "could not be read"
    if not snapshot:
        return ""                                   # first run ever
    if previous is None:
        # Switching a URL between normal and "variants:" changes its rows'
        # identities - one product row becomes many variant rows, or the
        # reverse. The product itself is not new; it just has no history in
        # this shape yet, so say nothing rather than "new".
        if known_pages is None:
            known_pages = {_page_of(url) for url in snapshot}
        page = product.page_url or _page_of(product.url)
        if page in snapshot or page in known_pages:
            return ""
        return "new"

    notes = []
    was = previous.get("sale_price")
    if was is None:
        was = previous.get("price")
    now = product.effective_price
    if was is not None and now is not None and abs(now - was) >= 0.01:
        direction = "down" if now < was else "up"
        notes.append(f"price {direction} {abs(now - was):,.2f} (was {was:,.2f})")

    was_stock = str(previous.get("in_stock") or "").strip().lower()
    now_stock = product.stock_text().lower()
    if was_stock == "yes" and now_stock == "no":
        notes.append("went out of stock")
    elif was_stock == "no" and now_stock == "yes":
        notes.append("back in stock")
    return "; ".join(notes)


def latest_row(product, snapshot: dict, known_pages: set[str] | None = None) -> list:
    stock = "unknown" if product.error else product.stock_text().lower()
    return [
        _link(_home_page(product.url), product.competitor),
        _text(product.name or ("ERROR: " + product.error if product.error else "")),
        _text(product.variant),
        product.price if product.price is not None else "",
        product.sale_price if product.sale_price is not None else "",
        stock,
        change_text(product, (snapshot or {}).get(product.url), snapshot or {}, known_pages),
        product.url,
    ]


def _latest_tab(workbook, rows_needed: int, log=print):
    title = config.LATEST_TAB
    for worksheet in workbook.worksheets():
        if worksheet.title == title:
            return worksheet
    worksheet = workbook.add_worksheet(title=title, rows=max(1000, rows_needed),
                                       cols=max(26, len(config.LATEST_HEADERS)))
    # First tab, so it is what the workbook opens on - except in test mode,
    # where the TEST tab must not push the real one aside.
    try:
        if config.TEST_MODE:
            raise RuntimeError("leave test tabs where they are")
        workbook.batch_update({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": worksheet.id, "index": 0},
            "fields": "index"}}]})
    except Exception:  # noqa: BLE001
        pass
    log(f"  Created the '{title}' tab.")
    return worksheet


def write_latest(workbook, products, snapshot: dict | None, log=print) -> int:
    """Rewrite the Latest tab with this run's rows. Returns rows written."""
    snapshot = snapshot or {}

    # Keep run order; gather each product's variants into one block.
    groups: dict[str, list] = {}
    for product in products:
        groups.setdefault(product.page_url or product.url, []).append(product)

    known_pages = {_page_of(url) for url in snapshot}
    body: list[list] = []
    blocks: list[tuple[int, int]] = []          # (first row, end row), 0-based, header = 0
    for group in groups.values():
        start = len(body) + 1
        body.extend(latest_row(p, snapshot, known_pages) for p in group)
        blocks.append((start, len(body) + 1))

    headers = config.LATEST_HEADERS
    worksheet = _latest_tab(workbook, len(body) + 1, log=log)

    if worksheet.row_count < len(body) + 1:
        worksheet.add_rows(len(body) + 1 - worksheet.row_count)
    if worksheet.col_count < len(headers):
        worksheet.add_cols(len(headers) - worksheet.col_count)

    # Last run's merged blocks fall on different rows; undo them first.
    try:
        workbook.batch_update({"requests": [{"unmergeCells": {"range": {
            "sheetId": worksheet.id, "startRowIndex": 0,
            "endRowIndex": worksheet.row_count,
            "startColumnIndex": 0, "endColumnIndex": len(headers)}}}]})
    except Exception:  # noqa: BLE001
        pass
    worksheet.clear()
    worksheet.update([headers] + body, "A1", value_input_option="USER_ENTERED")

    polish(workbook, worksheet, headers, log=log)

    sheet_id = worksheet.id
    requests: list[dict] = []

    # One product name per product: merge its cell down across all of that
    # product's variant rows.
    if config.MERGE_PRODUCT_NAMES:
        requests.extend(_merge_requests(sheet_id, headers.index("product"), blocks))

    # Colour key, one empty column to the right of the data.
    legend_column = len(headers) + 1
    legend_values, legend_format = _legend_requests(worksheet, legend_column)
    worksheet.update(legend_values, f"{_column_letter(legend_column)}1",
                     value_input_option="RAW")
    requests.extend(legend_format)

    try:
        workbook.batch_update({"requests": requests})
    except Exception as exc:  # noqa: BLE001 - cosmetic only
        log(f"  (could not fully format '{worksheet.title}': {exc})")

    return len(body)


def today() -> str:
    """
    Timestamp in the shape the existing Results rows use: '2026-09-23 9:57'.
    Date plus time, and no leading zero on the hour.
    """
    now = dt.datetime.now()
    return f"{now:%Y-%m-%d} {now.hour}:{now.minute:02d}"
