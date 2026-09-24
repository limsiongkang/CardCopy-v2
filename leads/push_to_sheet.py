"""
Push leads.csv into a Google Sheet called "Scrapling Leads".

Run this once your Google service-account key is in place:

    python push_to_sheet.py

It reuses the same credential as the competitor monitor - the key file named
by GOOGLE_CREDENTIALS_FILENAME in ../.env, looked for in ../secrets/ first.

Nothing here touches treg, so it costs nothing to run or re-run. Re-running
replaces the sheet's contents rather than appending, so the sheet always
matches the CSV.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import config  # noqa: E402  - needs the path insert above

import gspread  # noqa: E402
from google.oauth2.service_account import Credentials  # noqa: E402

SHEET_TITLE = "Scrapling Leads"
TAB_TITLE = "Leads"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def robot_email(key_file: Path) -> str:
    import json
    try:
        return json.loads(key_file.read_text(encoding="utf-8")).get("client_email", "?")
    except Exception:  # noqa: BLE001
        return "?"


def main() -> int:
    csv_path = HERE / "leads.csv"
    if not csv_path.exists():
        print(f"No {csv_path.name} found. Run leadgen.py first.")
        return 2

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    if len(rows) < 2:
        print(f"{csv_path.name} has no data rows - nothing to push.")
        return 2

    key_file = config.service_account_path()
    if not key_file.exists():
        print("The Google key file is not in place yet.")
        print(f"  Looked in: {config.SECRETS_DIR / config.GOOGLE_SERVICE_ACCOUNT_FILE}")
        print(f"         and: {config.ROOT / config.GOOGLE_SERVICE_ACCOUNT_FILE}")
        print()
        print("  See step 3 of ../README.md. Once the file is there, run this again.")
        return 2

    print(f"Signing in as the robot account from {key_file.name}...")
    credentials = Credentials.from_service_account_file(str(key_file), scopes=SCOPES)
    client = gspread.authorize(credentials)

    robot = robot_email(key_file)
    workbook = None

    # An explicit sheet, if one was given.
    if len(sys.argv) > 1:
        ref = sys.argv[1].strip()
        key = ref
        if "/d/" in ref:                       # a full browser URL
            key = ref.split("/d/", 1)[1].split("/", 1)[0]
        try:
            workbook = client.open_by_key(key)
            print(f"  Opened '{workbook.title}'.")
        except gspread.exceptions.APIError as exc:
            print(f"  Could not open that sheet: {exc}")
            print(f"  Make sure it is shared with {robot} as an Editor.")
            return 2

    if workbook is None:
        try:
            workbook = client.open(SHEET_TITLE)
            print(f"  Found the existing sheet '{SHEET_TITLE}'.")
        except gspread.SpreadsheetNotFound:
            try:
                workbook = client.create(SHEET_TITLE)
                print(f"  Created '{SHEET_TITLE}'.")
                recipient = config.ALERT_EMAIL_TO or config.SMTP_USERNAME
                if recipient:
                    workbook.share(recipient, perm_type="user", role="writer")
                    print(f"  Shared it with {recipient}.")
            except gspread.exceptions.APIError as exc:
                if "storage quota" not in str(exc).lower():
                    raise
                print()
                print("  A service account cannot CREATE a Google Sheet.")
                print("  Google gives robot accounts no Drive storage of their own, so the")
                print("  sheet has to be made by you and then shared with the robot. Once.")
                print()
                print("  1. Open https://sheets.new and name it:  " + SHEET_TITLE)
                print("  2. Click Share, paste in this address, set it to Editor, and Send:")
                print()
                print(f"       {robot}")
                print()
                print("  3. Run this again. It will find the sheet by name:")
                print("       python push_to_sheet.py")
                print()
                print("     Or paste the sheet's web address to be explicit:")
                print("       python push_to_sheet.py \"https://docs.google.com/spreadsheets/d/...\"")
                return 2

    titles = {ws.title: ws for ws in workbook.worksheets()}
    if TAB_TITLE in titles:
        worksheet = titles[TAB_TITLE]
        worksheet.clear()
    else:
        worksheet = workbook.add_worksheet(
            title=TAB_TITLE, rows=max(len(rows) + 50, 200), cols=len(rows[0])
        )

    worksheet.update(rows, "A1")
    worksheet.freeze(rows=1)
    worksheet.format("A1:Z1", {"textFormat": {"bold": True}})

    for title, sheet in titles.items():
        if title in ("Sheet1", "Sheet 1") and len(workbook.worksheets()) > 1:
            try:
                workbook.del_worksheet(sheet)
            except Exception:  # noqa: BLE001
                pass

    print()
    print(f"  Wrote {len(rows) - 1} lead(s) to the '{TAB_TITLE}' tab.")
    print(f"  {workbook.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
