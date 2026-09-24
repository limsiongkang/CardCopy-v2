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

_HEADER_BG = {"red": 0.17, "green": 0.24, "blue": 0.31}
_BAND_BG = {"red": 0.96, "green": 0.97, "blue": 0.98}
_BAD_BG = {"red": 0.99, "green": 0.91, "blue": 0.91}
_BAD_FG = {"red": 0.70, "green": 0.11, "blue": 0.11}
_WARN_BG = {"red": 1.00, "green": 0.96, "blue": 0.88}
_WARN_FG = {"red": 0.60, "green": 0.40, "blue": 0.00}

# date, competitor, product, <4th>, <5th>, <6th>, URL
_WIDTHS = [125, 165, 270, 105, 95, 95, 360]


def polish(workbook, worksheet, headers: list[str], money_columns: list[int],
           flag_column: int, flag_rules: list[tuple[str, dict, dict]], log=print) -> None:
    """
    Make a tab presentable: frozen bold header, sized columns, right-aligned
    money, centred status, zebra striping and colour-coded status cells.

    Safe to re-run - existing banding and conditional rules are cleared first
    so repeated runs do not stack duplicates.
    """
    sheet_id = worksheet.id
    last_col = len(headers)

    # --- clear anything a previous run added -------------------------
    clear: list[dict] = []
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
    except Exception:  # noqa: BLE001 - cosmetic only, never fail a run for it
        pass
    if clear:
        try:
            workbook.batch_update({"requests": clear})
        except Exception:  # noqa: BLE001
            pass

    requests: list[dict] = [
        # Freeze the header so it stays put while scrolling.
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        # Header band.
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": last_col},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 4, "bottom": 4, "left": 10, "right": 10},
                "textFormat": {"bold": True, "fontSize": 10,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 34}, "fields": "pixelSize"}},
        # Body text a touch smaller, vertically centred, with breathing room.
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": last_col},
            "cell": {"userEnteredFormat": {
                "verticalAlignment": "MIDDLE",
                "padding": {"top": 2, "bottom": 2, "left": 10, "right": 10},
                "textFormat": {"fontSize": 10}}},
            "fields": "userEnteredFormat(verticalAlignment,padding,textFormat)"}},
    ]

    # --- column widths ------------------------------------------------
    for index in range(min(last_col, len(_WIDTHS))):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": index, "endIndex": index + 1},
            "properties": {"pixelSize": _WIDTHS[index]}, "fields": "pixelSize"}})

    # --- money columns: two decimals, right aligned --------------------
    for index in money_columns:
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "startColumnIndex": index, "endColumnIndex": index + 1},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "RIGHT",
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"}})

    # --- status column centred ----------------------------------------
    if flag_column >= 0:
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "startColumnIndex": flag_column, "endColumnIndex": flag_column + 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"}})

    # --- link columns look like links ------------------------------------
    # The body repeatCell above replaces textFormat wholesale, so the link
    # styling has to be put back explicitly or the cells render as plain text.
    for index in (1, last_col - 1):
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "startColumnIndex": index, "endColumnIndex": index + 1},
            "cell": {"userEnteredFormat": {"textFormat": {
                "fontSize": 10,
                "underline": True,
                "foregroundColor": {"red": 0.05, "green": 0.35, "blue": 0.75}}}},
            "fields": "userEnteredFormat.textFormat"}})

    # --- zebra striping -------------------------------------------------
    requests.append({"addBanding": {"bandedRange": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": last_col},
        "rowProperties": {
            "firstBandColor": {"red": 1, "green": 1, "blue": 1},
            "secondBandColor": _BAND_BG}}}})

    # --- colour-code the status column ----------------------------------
    for text, background, foreground in flag_rules:
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1,
                        "startColumnIndex": flag_column,
                        "endColumnIndex": flag_column + 1}],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ",
                              "values": [{"userEnteredValue": text}]},
                "format": {"backgroundColor": background,
                           "textFormat": {"bold": True, "foregroundColor": foreground}}}}}})

    # --- a filter across the header --------------------------------------
    requests.append({"setBasicFilter": {"filter": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0,
                  "startColumnIndex": 0, "endColumnIndex": last_col}}}})

    try:
        workbook.batch_update({"requests": requests})
    except Exception as exc:  # noqa: BLE001 - never fail a run over cosmetics
        log(f"  (could not fully format '{worksheet.title}': {exc})")


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
    """Apply the house style to both tabs."""
    linkify_tab(data_tab, log=log)
    linkify_tab(alerts_tab, log=log)
    polish(
        workbook, data_tab, config.DATA_HEADERS,
        money_columns=[3, 4], flag_column=5,
        flag_rules=[("no", _BAD_BG, _BAD_FG),
                    ("unknown", _WARN_BG, _WARN_FG)],
        log=log,
    )
    polish(
        workbook, alerts_tab, config.ALERT_HEADERS,
        money_columns=[], flag_column=3,
        flag_rules=[("out of stock", _BAD_BG, _BAD_FG),
                    ("price drop", _WARN_BG, _WARN_FG)],
        log=log,
    )


def ensure_tabs(workbook, log=print):
    """Make sure both tabs exist with the right column headings."""
    existing = {ws.title: ws for ws in workbook.worksheets()}

    def build(title: str, headers: list[str]):
        if title in existing:
            worksheet = existing[title]
            current = worksheet.row_values(1)
            if [c.strip().lower() for c in current] != [h.lower() for h in headers]:
                worksheet.update([headers], "A1")
            return worksheet

        worksheet = workbook.add_worksheet(title=title, rows=1000, cols=len(headers))
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
        if title in ("Sheet1", "Sheet 1") and len(workbook.worksheets()) > 2:
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
        product.name or ("ERROR: " + product.error if product.error else ""),
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
    if rows:
        data_tab.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


def append_alerts(alerts_tab, alerts: list[dict], run_date: str) -> int:
    rows = [
        [
            run_date,
            _link(_home_page(a["url"]), a["competitor"]),
            a["product"],
            a["alert"],
            a["was"],
            a["now"],
            a["url"],
        ]
        for a in alerts
    ]
    if rows:
        alerts_tab.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


def today() -> str:
    """
    Timestamp in the shape the existing Results rows use: '2026-09-23 9:57'.
    Date plus time, and no leading zero on the hour.
    """
    now = dt.datetime.now()
    return f"{now:%Y-%m-%d} {now.hour}:{now.minute:02d}"
