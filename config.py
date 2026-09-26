"""
Every setting lives in the .env file next to this script.

Nothing sensitive is ever written into the code. If a required setting is
missing, the script stops with a plain-English message naming the setting
rather than failing halfway through a run.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

load_dotenv(ENV_PATH)

LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
COMPETITORS_FILE = ROOT / "competitors.txt"

# Per-site CSS rules, for shops that publish no standard product data.
SELECTORS_FILE = ROOT / "selectors.json"

# Scrapling's adaptive-selector memory. Kept inside the project ON PURPOSE:
# Scrapling's default puts this SQLite file inside its own site-packages
# folder, where a `pip install --upgrade scrapling` wipes it and every
# selector fingerprint learned so far is silently lost.
ADAPTIVE_STORAGE = DATA_DIR / "adaptive_selectors.db"

# A local copy of the last run, used only when the Google Sheet cannot be
# read. The sheet stays the source of truth.
SNAPSHOT_FILE = DATA_DIR / "last_run.json"

for _directory in (LOG_DIR, DATA_DIR):
    _directory.mkdir(parents=True, exist_ok=True)


class ConfigError(Exception):
    """Raised when a setting is missing or unusable."""


def _text(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _text(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _number(name: str, default: float) -> float:
    raw = _text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(
            f"{name} in .env should be a number, but it says {raw!r}."
        ) from None


def _alias(*names: str, default: str = "") -> str:
    """First non-empty value among several accepted variable names."""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return default


def _alias_number(default: float, *names: str) -> float:
    raw = _alias(*names)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(
            f"{names[0]} in .env should be a number, but it says {raw!r}."
        ) from None


# --- identity ----------------------------------------------------------
CLIENT_NAME = _text("CLIENT_NAME")
ALERT_EMAIL_TO = _alias("ALERT_RECIPIENT", "ALERT_EMAIL_TO")

# --- proxy -------------------------------------------------------------
# The proxy is used when PROXY_SERVER is filled in, and not otherwise.
PROXY_SERVER = _text("PROXY_SERVER")
PROXY_USERNAME = _text("PROXY_USERNAME")
PROXY_PASSWORD = _text("PROXY_PASSWORD")
PROXY_ENABLED = bool(PROXY_SERVER) and _flag("PROXY_ENABLED", True)

# --- google sheets -----------------------------------------------------
GOOGLE_SERVICE_ACCOUNT_FILE = _alias(
    "GOOGLE_CREDENTIALS_FILENAME", "GOOGLE_SERVICE_ACCOUNT_FILE",
    default="google-service-account.json",
)
GOOGLE_SHEET_ID = _text("GOOGLE_SHEET_ID")

# --- email -------------------------------------------------------------
SMTP_HOST = _text("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_number("SMTP_PORT", 587))
SMTP_USERNAME = _alias("GMAIL_ADDRESS", "SMTP_USERNAME")
# Google shows app passwords as four groups of four ("abcd efgh ijkl mnop").
# The spaces are for readability only and are not part of the password -
# pasting them in gets you an authentication failure that looks like a wrong
# password, so strip them rather than making that your problem.
SMTP_PASSWORD = _alias("GMAIL_APP_PASSWORD", "SMTP_PASSWORD").replace(" ", "")
SMTP_FROM = _alias("SMTP_FROM", "GMAIL_ADDRESS") or SMTP_USERNAME

# --- scraping behaviour ------------------------------------------------
REQUEST_DELAY_SECONDS = _alias_number(6, "DELAY_BETWEEN_URLS", "REQUEST_DELAY_SECONDS")
PAGE_TIMEOUT_MS = int(_number("PAGE_TIMEOUT_MS", 45000))
MAX_RETRIES = int(_number("MAX_RETRIES", 2))
HEADLESS = _flag("HEADLESS", True)
SOLVE_CLOUDFLARE = _flag("SOLVE_CLOUDFLARE", False)
# Drop images, fonts and stylesheets. Large saving on proxy bandwidth.
DISABLE_RESOURCES = _flag("DISABLE_RESOURCES", True)

# --- speed ---------------------------------------------------------------
# Try a plain HTTP request (impersonating Chrome) before starting a browser.
# Most shops put their product data straight into the HTML, so this takes
# well under a second where the browser takes ten or more. A page is only
# accepted from this path when it holds real product data; anything else -
# a bot check, a page built by JavaScript - goes to the stealth browser as
# before. Set to false to use the browser for every page.
FAST_MODE = _flag("FAST_MODE", True)
HTTP_TIMEOUT_SECONDS = _number("HTTP_TIMEOUT_SECONDS", 20)
# Waiting for "network idle" means waiting until the page makes no requests
# for half a second. Shops running analytics, chat widgets or ad pixels may
# never get there, so the browser sits out the whole timeout on every page.
# Off by default; turn on only for a site whose prices arrive late.
NETWORK_IDLE = _flag("NETWORK_IDLE", False)
# Try IPv4 before IPv6 for Google Sheets and email. On a network where IPv6
# is on but broken, each connection otherwise waits a minute or more for
# IPv6 to time out, and Sheets often gives up first. See netfix.py.
PREFER_IPV4 = _flag("PREFER_IPV4", True)

# --- what counts as an alert -------------------------------------------
# Price drops and stock-outs always alert - that was the original brief.
# Back-in-stock is useful but noisier, so it is opt-in.
ALERT_ON_BACK_IN_STOCK = _flag("ALERT_ON_BACK_IN_STOCK", False)
# Price rises are recorded as alerts too (and emailed), so every price
# movement shows on the Alerts tab. Set false for drops only.
ALERT_ON_PRICE_RISE = _flag("ALERT_ON_PRICE_RISE", True)

SHEET_TITLE = f"{CLIENT_NAME} - Competitor Intel" if CLIENT_NAME else ""
# "Results" is the tab the existing history lives in. Writing there means each
# run can compare against the previous one from day one.
DATA_TAB = "Results"
ALERTS_TAB = "Alerts"
# Today's picture only, rewritten every run and grouped by product - the tab
# to open when you want to check prices. Results keeps the full history.
LATEST_TAB = "Latest"

# --- test mode (run.ps1 -Test) -------------------------------------------
# Reads the local test shop (start_testshop.ps1) and writes to separate
# TEST tabs, so testing the alerts never touches the real history.
TEST_MODE = False
TEST_SHOP_URL = "list:variants:http://127.0.0.1:8765/"
# Show a product's name once, in one tall cell beside all of its variant
# rows, instead of repeating it on every row. Easier to read; the cost is
# that Google Sheets cannot sort a range containing merged cells, and a
# filter on the product column only matches each product's first row.
MERGE_PRODUCT_NAMES = _flag("MERGE_PRODUCT_NAMES", True)

# Each row is one variant (a size, colour, grind...). A product without
# options is a single row with the variant cell left empty.
DATA_HEADERS = [
    "date", "competitor", "product", "variant", "price", "sale price", "in stock", "URL",
]
# "change" is the signed price difference, e.g. +9.99 or -7.89.
ALERT_HEADERS = [
    "date", "competitor", "product", "variant", "alert", "was", "now", "change", "URL",
]
LATEST_HEADERS = [
    "competitor", "product", "variant", "price", "sale price", "in stock",
    "change since last run", "URL",
]

# The layouts before variants existed. A tab still in this shape gets a
# variant column inserted - shifting its history across properly - instead of
# having its header overwritten, which would misalign every existing row.
LEGACY_DATA_HEADERS = [
    "date", "competitor", "product", "price", "sale price", "in stock", "URL",
]
LEGACY_ALERT_HEADERS = [
    "date", "competitor", "product", "alert", "was", "now", "URL",
]

# --- category pages ------------------------------------------------------
# A "list:variants:" line opens every product on the category page to read its
# variants - one page visit per product. This caps how many, so one huge
# category cannot turn a run into hours. (Whether to open them at all is
# decided per line in competitors.txt by the "variants:" prefix.)
LISTING_MAX_PRODUCTS = int(_number("LISTING_MAX_PRODUCTS", 100))


def proxy() -> dict | None:
    """
    Build the proxy setting Scrapling expects.

    Providers hand out credentials in several shapes, so all of these work:

        PROXY_SERVER=http://gate.provider.com:7000   (+ USERNAME / PASSWORD)
        PROXY_SERVER=gate.provider.com:7000          (+ USERNAME / PASSWORD)
        PROXY_SERVER=http://user:pass@gate.provider.com:7000
        PROXY_SERVER=user:pass@gate.provider.com:7000
        PROXY_SERVER=socks5://gate.provider.com:1080

    A dict is returned rather than a URL string so a password containing @ or
    : is handled correctly, and so it never sits inside a URL that could end
    up in a log line.
    """
    if not PROXY_ENABLED or not PROXY_SERVER:
        return None

    server = PROXY_SERVER
    username, password = PROXY_USERNAME, PROXY_PASSWORD

    scheme = "http://"
    for known in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
        if server.lower().startswith(known):
            scheme, server = known, server[len(known):]
            break

    # Credentials pasted inline as user:pass@host:port.
    if "@" in server:
        credentials, _, host = server.rpartition("@")
        if credentials:
            inline_user, _, inline_password = credentials.partition(":")
            username = username or unquote(inline_user)
            password = password or unquote(inline_password)
        server = host

    settings: dict[str, str] = {"server": scheme + server}
    if username:
        settings["username"] = username
    if password:
        settings["password"] = password
    return settings


SECRETS_DIR = ROOT / "secrets"


def service_account_path() -> Path:
    """Look in secrets/ first, then beside the scripts."""
    path = Path(GOOGLE_SERVICE_ACCOUNT_FILE)
    if path.is_absolute():
        return path
    in_secrets = SECRETS_DIR / path
    return in_secrets if in_secrets.exists() else (ROOT / path)


def redacted_summary() -> str:
    """A safe-to-log picture of the current settings. No secrets."""
    proxy_settings = proxy()
    if proxy_settings:
        proxy_line = proxy_settings["server"]
        if proxy_settings.get("username"):
            proxy_line += f" (as {proxy_settings['username']}, password hidden)"
    else:
        proxy_line = "not in use - scraping from this machine's own IP address"

    return "\n".join([
        f"  Client        : {CLIENT_NAME or '(not set)'}",
        f"  Alerts to     : {ALERT_EMAIL_TO or '(not set)'}",
        f"  Proxy         : {proxy_line}",
        f"  Sheet         : {SHEET_TITLE or '(not set)'}",
        f"  Delay         : {REQUEST_DELAY_SECONDS}s between pages on the same site",
        f"  Fetching      : {'fast request first, browser if needed' if FAST_MODE else 'browser for every page'}",
        f"  Page timeout  : {PAGE_TIMEOUT_MS / 1000:.0f}s",
    ])


def check(need_sheets: bool = True, need_email: bool = True) -> list[str]:
    """Return a list of plain-English problems. Empty list means good to go."""
    problems: list[str] = []

    if not ENV_PATH.exists():
        problems.append(
            "There is no .env file yet. Copy .env.example to .env and fill it in."
        )
        return problems

    if not CLIENT_NAME:
        problems.append("CLIENT_NAME is empty in .env - it names the Google Sheet.")

    if need_sheets:
        key_file = service_account_path()
        if not key_file.exists():
            problems.append(
                f"The Google key file was not found.\n"
                f"      Looked in: {SECRETS_DIR / GOOGLE_SERVICE_ACCOUNT_FILE}\n"
                f"             and: {ROOT / GOOGLE_SERVICE_ACCOUNT_FILE}\n"
                f"      See step 3 of README.md."
            )

    if need_email:
        if not ALERT_EMAIL_TO:
            problems.append("ALERT_RECIPIENT is empty in .env - nowhere to send alerts.")
        if not SMTP_USERNAME:
            problems.append("GMAIL_ADDRESS is empty in .env.")
        if not SMTP_PASSWORD:
            problems.append(
                "GMAIL_APP_PASSWORD is empty in .env. It is a 16-character app "
                "password from https://myaccount.google.com/apppasswords, not "
                "your normal Google password."
            )

    if not COMPETITORS_FILE.exists():
        problems.append(f"{COMPETITORS_FILE.name} is missing.")

    return problems
