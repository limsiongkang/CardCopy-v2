"""
Lead list builder, powered by treg.

Pipeline, per delivered row:

  1. companies  thecompaniesapi.companies.search  (simplified=true -> FREE)
                US + Shopify + 1-50 employees -> company domains
  2. people     treg.people.search                (routed; free children first)
                everyone at that domain, filtered locally to the wanted titles
  3. email      treg.people.email.find            ($0.00483 per SUCCESS, misses free)
  4. verify     treg.people.email.verify          (routed; contactout at $0 first)

Only people whose email comes back verified are written out.

The treg token is read at runtime from ~/.treg/config.json. It is never
written into this file, the CSV, or the log.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

CONFIG = Path.home() / ".treg" / "config.json"
HERE = Path(__file__).resolve().parent

WANTED_TITLE_PATTERNS = (
    re.compile(r"\bfounder\b", re.I),
    re.compile(r"\bco[-\s]?founder\b", re.I),
    re.compile(r"\bowner\b", re.I),
    re.compile(r"\be[-\s]?commerce\s+manager\b", re.I),
    re.compile(r"\becom\s+manager\b", re.I),
)

# The pilot surfaced this: an executive assistant whose title read
# "Executive Assistant to Scott Strode/Founder & Executive Director" matched
# on the word "founder" and was written out as a founder. A title that names
# somebody ELSE's role is not that person's role.
EXCLUDED_TITLE_PATTERNS = (
    re.compile(r"\bassistant\s+to\b", re.I),
    re.compile(r"\bexecutive\s+assistant\b", re.I),
    re.compile(r"\b(ea|pa)\s+to\b", re.I),
    re.compile(r"\breports?\s+to\b", re.I),
    re.compile(r"\bformer\b", re.I),
    re.compile(r"\bretired\b", re.I),
    re.compile(r"\bintern\b", re.I),
)

CSV_HEADERS = [
    "first name", "last name", "title", "company",
    "company website", "verified work email",
]


# ----------------------------------------------------------------------
# treg client
# ----------------------------------------------------------------------

class Treg:
    def __init__(self, max_spend_usd: float):
        if not CONFIG.exists():
            raise SystemExit(
                f"Not signed in to treg ({CONFIG} not found). Run: treg login"
            )
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.token = cfg["token"]
        self.base = cfg.get("base_url", "https://treg.to").rstrip("/")
        self.session = requests.Session()
        self.spent_micro = 0
        self.max_spend_micro = int(max_spend_usd * 1_000_000)
        self.calls = 0

    @property
    def spent_usd(self) -> float:
        return self.spent_micro / 1_000_000

    def budget_left_usd(self) -> float:
        return (self.max_spend_micro - self.spent_micro) / 1_000_000

    def call(self, endpoint: str, *, method: str = "POST",
             body: dict | None = None, params: dict | None = None,
             max_cost: float | None = None) -> tuple[dict | None, int, str]:
        """Returns (parsed_json_or_None, cost_micro, served_by)."""
        if self.spent_micro >= self.max_spend_micro:
            raise BudgetExhausted(f"spend cap of ${self.max_spend_micro/1e6:.2f} reached")

        headers = {"X-Treg-Token": self.token}
        if max_cost is not None:
            headers["X-Treg-Route-Max-Cost"] = str(max_cost)

        url = f"{self.base}/call/{endpoint}"
        try:
            response = self.session.request(
                method, url, headers=headers, json=body, params=params, timeout=180
            )
        except requests.RequestException as exc:
            return None, 0, f"network error: {exc}"

        self.calls += 1
        cost = int(response.headers.get("X-Treg-Cost-Micro") or 0)
        self.spent_micro += cost
        served_by = response.headers.get("X-Treg-Served-By", "")

        if response.status_code == 402:
            raise BudgetExhausted("treg balance exhausted (HTTP 402)")

        try:
            data = response.json()
        except ValueError:
            return None, cost, served_by

        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            return {"_error": detail or f"HTTP {response.status_code}"}, cost, served_by

        return data, cost, served_by


class BudgetExhausted(Exception):
    pass


# ----------------------------------------------------------------------
# Step 1 - companies (free)
# ----------------------------------------------------------------------

# Industry slugs that mean "sells physical goods to consumers". Without this
# the Shopify filter also catches nonprofits and service businesses that happen
# to run a merch store - the pilot pulled in The Phoenix, a recovery charity.
#
# NOTE: this slug list has NOT been validated against live data. Confirming it
# costs about $0.05 in treg credit (one company page), which was out of budget
# at the time of writing. If a run returns zero companies, an unrecognised slug
# is the first thing to suspect - drop back to ["retail"] alone, which IS
# confirmed to work, and widen from there.
ECOMMERCE_INDUSTRIES = [
    "retail",
    "apparel-and-fashion",
    "consumer-goods",
    "consumer-electronics",
    "food-and-beverages",
    "cosmetics",
    "sporting-goods",
    "luxury-goods-and-jewelry",
    "furniture",
    "health-wellness-and-fitness",
]

COMPANY_QUERY = [
    {"attribute": "technologies.active", "operator": "and",
     "sign": "equals", "values": ["shopify"]},
    # 5-50 is not expressible: the provider's bands are 1-10 and 11-50.
    {"attribute": "about.totalEmployees", "operator": "or",
     "sign": "equals", "values": ["1-10", "11-50"]},
    {"attribute": "locations.headquarters.country.code", "operator": "and",
     "sign": "equals", "values": ["us"]},
    {"attribute": "about.industries", "operator": "or",
     "sign": "equals", "values": ECOMMERCE_INDUSTRIES},
]


def fetch_companies(tg: Treg, page: int, size: int, log) -> list[dict]:
    data, cost, _ = tg.call(
        "thecompaniesapi.companies.search",
        method="GET",
        params={
            "query": json.dumps(COMPANY_QUERY),
            "size": size,
            "page": page,
            "simplified": "true",
        },
    )
    if not data or "companies" not in data:
        log(f"    company page {page}: no results ({(data or {}).get('_error', 'empty')})")
        return []

    out = []
    for row in data["companies"]:
        about = row.get("about") or {}
        domain = (row.get("domain") or {}).get("domain")
        if not domain:
            continue
        out.append({
            "domain": domain,
            "name": about.get("name") or domain,
            "industry": about.get("industry") or "",
        })
    if cost:
        log(f"    (company page {page} cost ${cost/1e6:.5f} - expected free)")
    return out


# ----------------------------------------------------------------------
# Step 2 - people at a domain (free)
# ----------------------------------------------------------------------

def _rows_from(payload) -> list[dict]:
    """treg routed responses put provider rows under output.<something>."""
    if not isinstance(payload, dict):
        return []
    output = payload.get("output")
    if isinstance(output, dict):
        for key in ("people", "results", "data", "rows", "contacts"):
            value = output.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    if isinstance(output, list):
        return [r for r in output if isinstance(r, dict)]
    for key in ("people", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _pick(row: dict, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def title_matches(title: str | None) -> bool:
    if not title:
        return False
    if any(p.search(title) for p in EXCLUDED_TITLE_PATTERNS):
        return False
    return any(p.search(title) for p in WANTED_TITLE_PATTERNS)


def split_name(row: dict) -> tuple[str, str]:
    first = _pick(row, "first_name", "firstName", "givenName") or ""
    last = _pick(row, "last_name", "lastName", "familyName") or ""
    if first and last:
        return str(first).strip(), str(last).strip()
    full = _pick(row, "full_name", "fullName", "name") or ""
    parts = str(full).split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return (str(first) or (parts[0] if parts else "")), str(last)


def find_people(tg: Treg, company: dict, log) -> list[dict]:
    data, cost, served = tg.call(
        "treg.people.search",
        body={"company_domain": company["domain"], "limit": 25, "country": "US"},
        max_cost=0.01,
    )
    if cost:
        log(f"      people search cost ${cost/1e6:.5f} via {served}")

    people = []
    for row in _rows_from(data):
        title = _pick(row, "title", "job_title", "jobTitle", "headline", "position")
        if not title_matches(str(title)):
            continue
        first, last = split_name(row)
        if not first or not last:
            continue
        people.append({
            "first_name": first,
            "last_name": last,
            "title": str(title).strip(),
            "company": company["name"],
            "domain": company["domain"],
        })
    return people


# ----------------------------------------------------------------------
# Steps 3 & 4 - find then verify the email
# ----------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_email(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if isinstance(output, dict):
        for key in ("email", "email_address", "work_email", "value"):
            value = output.get(key)
            if isinstance(value, str) and EMAIL_RE.fullmatch(value.strip()):
                return value.strip().lower()
    blob = json.dumps(payload)
    match = EMAIL_RE.search(blob)
    return match.group(0).lower() if match else None


def is_verified(payload) -> tuple[bool, str]:
    """Read a verify response. Only an explicit pass counts."""
    if not isinstance(payload, dict):
        return False, "no response"
    output = payload.get("output")
    if not isinstance(output, dict):
        output = payload

    if output.get("verified") is True:
        return True, "verified=true"
    if output.get("verified") is False:
        return False, "verified=false"

    for key in ("status", "result", "state", "deliverability"):
        value = output.get(key)
        if isinstance(value, str):
            token = value.strip().lower().replace("-", "_")
            if token in ("valid", "deliverable", "ok", "verified", "safe"):
                return True, token
            if token in ("invalid", "undeliverable", "bounce", "disposable",
                         "unknown", "accept_all", "catch_all", "risky", "spamtrap"):
                return False, token
    return False, "unclear"


def resolve_email(tg: Treg, person: dict, log) -> tuple[str | None, str, int]:
    """Find then verify. Returns (verified_email_or_None, reason, cost_micro)."""
    spent = 0

    found, cost, served = tg.call(
        "treg.people.email.find",
        body={
            "domain": person["domain"],
            "first_name": person["first_name"],
            "last_name": person["last_name"],
        },
        max_cost=0.02,
    )
    spent += cost
    email = extract_email(found)
    if not email:
        return None, f"no email found (${cost/1e6:.5f})", spent

    checked, vcost, vserved = tg.call(
        "treg.people.email.verify", body={"email": email}, max_cost=0.02
    )
    spent += vcost
    ok, reason = is_verified(checked)
    if not ok:
        return None, f"found {email} but verify said '{reason}'", spent
    return email, f"verified via {vserved or 'router'}", spent


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build a verified-email lead list via treg.")
    parser.add_argument("--target", type=int, default=10, help="verified records wanted")
    parser.add_argument("--max-spend", type=float, default=0.25, help="hard USD cap")
    parser.add_argument("--out", default=str(HERE / "leads.csv"))
    parser.add_argument("--start-page", type=int, default=1)
    args = parser.parse_args()

    def log(message=""):
        print(message, flush=True)

    tg = Treg(max_spend_usd=args.max_spend)

    log("=" * 68)
    log(f"Lead list - target {args.target} verified records, cap ${args.max_spend:.2f}")
    log("=" * 68)

    rows: list[dict] = []
    attempted = 0
    found_not_verified = 0
    no_email = 0
    page = args.start_page
    seen_domains: set[str] = set()

    try:
        while len(rows) < args.target and page < args.start_page + 40:
            log(f"\n  Company page {page}...")
            companies = fetch_companies(tg, page=page, size=25, log=log)
            page += 1
            if not companies:
                break

            for company in companies:
                if len(rows) >= args.target:
                    break
                if company["domain"] in seen_domains:
                    continue
                seen_domains.add(company["domain"])

                people = find_people(tg, company, log)
                if not people:
                    continue

                log(f"    {company['name'][:34]:36} ({company['domain']}) - {len(people)} match(es)")

                for person in people:
                    if len(rows) >= args.target:
                        break
                    attempted += 1
                    email, reason, _ = resolve_email(tg, person, log)
                    who = f"{person['first_name']} {person['last_name']}"
                    if email:
                        rows.append({
                            "first name": person["first_name"],
                            "last name": person["last_name"],
                            "title": person["title"],
                            "company": person["company"],
                            "company website": f"https://{person['domain']}",
                            "verified work email": email,
                        })
                        log(f"      [{len(rows):>3}/{args.target}] {who[:26]:28} {email}")
                    else:
                        if "no email found" in reason:
                            no_email += 1
                        else:
                            found_not_verified += 1
                        log(f"      ---       {who[:26]:28} {reason}")

    except BudgetExhausted as exc:
        log(f"\n  STOPPED: {exc}")
    except KeyboardInterrupt:
        log("\n  Stopped by you.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    per_record = tg.spent_usd / len(rows) if rows else 0.0

    log("")
    log("=" * 68)
    log(f"  Verified records written : {len(rows)}")
    log(f"  People attempted         : {attempted}")
    log(f"     no email found        : {no_email}")
    log(f"     found but not verified: {found_not_verified}")
    log(f"  treg calls made          : {tg.calls}")
    log(f"  TOTAL SPEND              : ${tg.spent_usd:.4f}")
    log(f"  COST PER DELIVERED RECORD: ${per_record:.4f}")
    log(f"  CSV                      : {out_path}")
    log("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
