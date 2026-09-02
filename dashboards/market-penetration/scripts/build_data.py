#!/usr/bin/env python3
"""Build data.json for the Market Penetration dashboard.

Penetration answers "what share of a market's active customers buy this
category?". For each warehouse we take the trailing-window order lines from
the Looker "Network Sales Tracker - <WH>.csv" exports, attach a category /
sub-category to every line via the item taxonomy, then count DISTINCT
accounts per category. Distinct-account counts are why this is built from
order lines rather than from a pre-aggregated per-SKU export: customers who
buy three syrups are one penetrated account, not three.

  penetration(category, warehouse) = accounts buying category / active accounts

The network benchmark is the same ratio pooled across every warehouse, so a
market can be read against the network as a whole rather than against an
average of ratios (which would over-weight small warehouses).

Sources, all in the Looker Data Dumps Drive folder:
  * Network Sales Tracker - <WH>.csv  order lines (account, item, date, qty)
  * Combined Models Dump for Dashbaord / "Warehouse Raw"
        item_class, "Category : Sub-Category", keyed by item uuid + name
  * SKU Margins.csv                   per-each sale price, for revenue share

Revenue is derived (units x per-each sale price) rather than read from a
revenue export, so it covers exactly the same window and business-line
filter as the penetration numbers.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
FILE_PATTERN = re.compile(r"^Network Sales Tracker - ([A-Za-z0-9]+)\.csv$")

# Warehouses whose exports predate the "Network Sales Tracker - <WH>" naming.
# A discovered file for the same warehouse wins.
STATIC_SOURCES = {
    "DCA1": "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs",
    "BOS1": "1Kf3G7NztpiE08zfbOA_os0lLipqukIGqsZ61b0N9LoE",
}

TAXONOMY_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
TAXONOMY_RANGE = "'Warehouse Raw'!A1:BA"
MARGINS_ID = "1zR77QnG5FLdOfJgn8eqVgYopejdLxqDU6xCTWXyXRwA"
MARGINS_RANGE = "A1:H"

# Last-mile delivery. Shipping / Pallet / Services move through different
# economics and are not comparable market-to-market, so they are excluded.
INCLUDED_BUSINESS_LINES = {"local distribution", "metrobi"}

WINDOWS = [30, 90]

# A fresh build with fewer than this fraction of the previous build's order
# lines is treated as a partially written export rather than real data.
PARTIAL_READ_RATIO = 0.8

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

COL_ITEM_NAME = "Item Name"
COL_ACCOUNT_UUID = "Odeko Account Uuid"
COL_CUSTOMER = "Customer Name"
COL_DATE = "Date Date"
COL_WAREHOUSE = "Warehouse Name"
COL_QTY = "SO Item Qty"
COL_ITEM_UUID = "Item Uuid"
COL_CONVERSION = "Conversion Rate"
COL_BUSINESS_LINE = "Business Line"

UNSPECIFIED_SUB = "General"


# Looker rewrites these exports in place, and the Sheets API sheds load with
# 5xx under it, so both an empty read and a transient server error are normal
# mid-refresh states rather than build failures.
READ_ATTEMPTS = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}


def read_values(sheets, spreadsheet_id, cell_range, allow_empty=False):
    """Read a range, retrying transient API errors and mid-rewrite empties."""
    last = None
    for attempt in range(READ_ATTEMPTS):
        if attempt:
            time.sleep(min(2 ** attempt, 16) + random.random())
        try:
            rows = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=cell_range)
                .execute()
                .get("values", [])
            )
        except HttpError as exc:
            if exc.resp.status not in RETRY_STATUSES:
                raise
            last = exc
            continue
        if rows or allow_empty:
            return rows
        last = ValueError("sheet returned no rows")
    if isinstance(last, Exception):
        raise last
    return []


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def split_class(item_class):
    """'Paper & Disposables : Cups' -> ('Paper & Disposables', 'Cups')."""
    parts = [p.strip() for p in item_class.split(":", 1)]
    cat = parts[0]
    sub = parts[1] if len(parts) > 1 and parts[1] else UNSPECIFIED_SUB
    return cat, sub


def load_taxonomy(sheets):
    """item uuid -> (category, sub), plus a lowercased item-name fallback."""
    rows = read_values(sheets, TAXONOMY_ID, TAXONOMY_RANGE)
    col = {name: i for i, name in enumerate(rows[0])}
    for required in ("item_class", "item_uuid", "item_name"):
        if required not in col:
            sys.exit(f"taxonomy sheet missing column: {required}")

    by_uuid, by_name = {}, {}
    for r in rows[1:]:
        def get(key):
            i = col[key]
            return (r[i] if i < len(r) else "").strip()

        item_class = get("item_class")
        if not item_class:
            continue
        pair = split_class(item_class)
        if get("item_uuid"):
            by_uuid.setdefault(get("item_uuid"), pair)
        if get("item_name"):
            by_name.setdefault(get("item_name").lower(), pair)
    if not by_uuid:
        sys.exit("taxonomy sheet produced no item classes")
    return by_uuid, by_name


def load_prices(sheets):
    """Lowercased item name -> sale price per each."""
    rows = read_values(sheets, MARGINS_ID, MARGINS_RANGE, allow_empty=True)
    if not rows:
        print("warning: SKU margins sheet returned no rows; revenue will be 0")
        return {}
    col = {name: i for i, name in enumerate(rows[0])}
    name_i = col.get(COL_ITEM_NAME)
    if name_i is None:
        print("warning: SKU margins sheet has no Item Name column")
        return {}
    price_cols = [
        col[c]
        for c in ("Sale Price Dollars Eaches", "Regular Sale Price Dollars")
        if c in col
    ]
    prices = {}
    for r in rows[1:]:
        name = (r[name_i] if name_i < len(r) else "").strip().lower()
        if not name or name in prices:
            continue
        for i in price_cols:
            price = parse_num(r[i] if i < len(r) else "")
            if price is not None and price > 0:
                prices[name] = price
                break
    return prices


def discover_sources(drive):
    """Newest 'Network Sales Tracker - <WH>.csv' per warehouse, merged over
    STATIC_SOURCES (discovered files win)."""
    res = (
        drive.files()
        .list(
            q=(
                f"'{LOOKER_FOLDER_ID}' in parents"
                " and name contains 'Network Sales Tracker - '"
                " and mimeType = 'application/vnd.google-apps.spreadsheet'"
                " and trashed = false"
            ),
            orderBy="modifiedTime desc",
            fields="files(id, name, modifiedTime)",
            pageSize=100,
        )
        .execute()
    )
    sources = {}
    for f in res.get("files", []):  # newest first: keep first file per WH
        m = FILE_PATTERN.match(f["name"])
        if m:
            sources.setdefault(m.group(1).upper(), f["id"])
    for wh, file_id in STATIC_SOURCES.items():
        sources.setdefault(wh, file_id)
    return sources


class Tally:
    """Distinct accounts / units / revenue for one window of one market."""

    def __init__(self):
        self.active = set()
        self.cat_accounts = defaultdict(set)
        self.sub_accounts = defaultdict(set)
        self.cat_units = defaultdict(float)
        self.sub_units = defaultdict(float)
        self.cat_revenue = defaultdict(float)
        self.sub_revenue = defaultdict(float)
        self.classified_units = 0.0
        self.total_units = 0.0
        self.order_lines = 0

    def add_line(self, account, cat, sub, units, revenue):
        self.order_lines += 1
        self.total_units += units
        if account:
            self.active.add(account)
        if cat is None:
            return
        self.classified_units += units
        self.cat_units[cat] += units
        self.cat_revenue[cat] += revenue
        key = (cat, sub)
        self.sub_units[key] += units
        self.sub_revenue[key] += revenue
        if account:
            self.cat_accounts[cat].add(account)
            self.sub_accounts[key].add(account)

    def merge(self, other):
        self.active |= other.active
        for k, v in other.cat_accounts.items():
            self.cat_accounts[k] |= v
        for k, v in other.sub_accounts.items():
            self.sub_accounts[k] |= v
        for src, dst in (
            (other.cat_units, self.cat_units),
            (other.sub_units, self.sub_units),
            (other.cat_revenue, self.cat_revenue),
            (other.sub_revenue, self.sub_revenue),
        ):
            for k, v in src.items():
                dst[k] += v
        self.classified_units += other.classified_units
        self.total_units += other.total_units
        self.order_lines += other.order_lines

    def to_json(self):
        active = len(self.active)
        revenue_total = sum(self.cat_revenue.values())

        def rate(n):
            return round(n / active, 5) if active else 0.0

        def share(v):
            return round(v / revenue_total, 5) if revenue_total else 0.0

        cats = {}
        for cat, accounts in self.cat_accounts.items():
            subs = {}
            for (c, sub), sub_accts in self.sub_accounts.items():
                if c != cat:
                    continue
                subs[sub] = {
                    "accounts": len(sub_accts),
                    "pen": rate(len(sub_accts)),
                    "units": round(self.sub_units[(c, sub)], 2),
                    "revenue": round(self.sub_revenue[(c, sub)], 2),
                    "revShare": share(self.sub_revenue[(c, sub)]),
                }
            cats[cat] = {
                "accounts": len(accounts),
                "pen": rate(len(accounts)),
                "units": round(self.cat_units[cat], 2),
                "revenue": round(self.cat_revenue[cat], 2),
                "revShare": share(self.cat_revenue[cat]),
                "subs": subs,
            }
        return {
            "activeAccounts": active,
            "orderLines": self.order_lines,
            "revenue": round(revenue_total, 2),
            "units": round(self.classified_units, 2),
            "classifiedUnitShare": (
                round(self.classified_units / self.total_units, 4)
                if self.total_units
                else 0.0
            ),
            "categories": cats,
        }


def aggregate_warehouse(rows, warehouse, by_uuid, by_name, prices):
    """Order lines for one warehouse -> {window: Tally}, plus diagnostics."""
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [COL_ITEM_NAME, COL_ACCOUNT_UUID, COL_DATE, COL_QTY, COL_CONVERSION]
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"missing expected columns: {missing}")
    wh_col = col.get(COL_WAREHOUSE)
    bl_col = col.get(COL_BUSINESS_LINE)
    uuid_col = col.get(COL_ITEM_UUID)

    # Parse once; the largest exports run to several hundred thousand lines
    # and we need two passes (max date, then the windows relative to it).
    parsed = []
    max_date = None
    unpriced_units = 0.0
    unclassified_lines = 0
    # Non-inventory local items (fresh bakery, direct-ship dairy) never enter
    # the procurement catalog, so they have no item_class. They are reported
    # rather than guessed at: a keyword rule would silently invent penetration.
    unclassified_units = Counter()
    for r in rows[1:]:
        def get(i):
            return (r[i] if i is not None and i < len(r) else "").strip()

        if wh_col is not None and get(wh_col) and get(wh_col) != warehouse:
            continue
        if bl_col is not None:
            if get(bl_col).lower() not in INCLUDED_BUSINESS_LINES:
                continue
        date = parse_date(get(col[COL_DATE]))
        if date is None:
            continue
        if max_date is None or date > max_date:
            max_date = date

        qty = parse_num(get(col[COL_QTY]))
        conversion = parse_num(get(col[COL_CONVERSION])) or 1.0
        if qty is None or conversion == 0:
            continue
        units = qty / conversion

        item_name = get(col[COL_ITEM_NAME])
        key = item_name.lower()
        klass = by_uuid.get(get(uuid_col)) if uuid_col is not None else None
        if klass is None:
            klass = by_name.get(key)
        if klass is None:
            unclassified_lines += 1
            unclassified_units[item_name] += units
            cat = sub = None
        else:
            cat, sub = klass

        price = prices.get(key)
        if price is None and cat is not None:
            unpriced_units += units
        revenue = units * price if price is not None else 0.0

        account = get(col[COL_ACCOUNT_UUID]) or ("n:" + get(col[COL_CUSTOMER]) if COL_CUSTOMER in col and get(col[COL_CUSTOMER]) else "")
        parsed.append((date, account, cat, sub, units, revenue))

    if max_date is None:
        raise ValueError("no dated order lines")

    tallies = {}
    for window in WINDOWS:
        start = max_date - timedelta(days=window - 1)
        tally = Tally()
        for date, account, cat, sub, units, revenue in parsed:
            if date < start:
                continue
            tally.add_line(account, cat, sub, units, revenue)
        tallies[window] = tally

    diagnostics = {
        "asOf": max_date.isoformat(),
        "unclassifiedLines": unclassified_lines,
        "unpricedUnits": round(unpriced_units, 2),
        "topUnclassified": [
            {"item": name, "units": round(units, 1)}
            for name, units in unclassified_units.most_common(10)
        ],
    }
    return tallies, diagnostics


def read_existing(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    by_uuid, by_name = load_taxonomy(sheets)
    prices = load_prices(sheets)
    print(f"taxonomy: {len(by_uuid)} items, prices: {len(prices)} items")

    sources = discover_sources(drive)
    if not sources:
        sys.exit("No sales tracker sources found")

    previous = read_existing(out_path)
    prev_lines = {}
    if previous:
        for wh, entry in previous.get("warehouses", {}).items():
            longest = str(max(WINDOWS))
            if longest in entry.get("windows", {}):
                prev_lines[wh] = entry["windows"][longest]["orderLines"]

    warehouses = {}
    network = {w: Tally() for w in WINDOWS}
    errors = {}
    for wh in sorted(sources):
        try:
            rows = read_values(sheets, sources[wh], "A1:Z")
            tallies, diagnostics = aggregate_warehouse(
                rows, wh, by_uuid, by_name, prices
            )
            # Looker rewrites these exports in place, so a read can land while
            # only part of the file has been written -- which looks like a
            # valid but much smaller export and would silently understate
            # penetration. Treat a big drop in order lines as a partial read.
            longest = tallies[max(WINDOWS)]
            if wh in prev_lines and prev_lines[wh]:
                if longest.order_lines < PARTIAL_READ_RATIO * prev_lines[wh]:
                    raise ValueError(
                        f"partial export: {longest.order_lines} order lines "
                        f"vs {prev_lines[wh]} previously"
                    )
            warehouses[wh] = {
                **diagnostics,
                "windows": {str(w): tallies[w].to_json() for w in WINDOWS},
            }
            for w in WINDOWS:
                network[w].merge(tallies[w])
            print(
                f"{wh}: {longest.order_lines} lines, "
                f"{len(longest.active)} active accounts, "
                f"{len(longest.cat_accounts)} categories"
            )
        except Exception as exc:  # keep one bad export from sinking the build
            errors[wh] = str(exc)
            print(f"{wh}: ERROR {exc}", file=sys.stderr)
            if previous and wh in previous.get("warehouses", {}):
                warehouses[wh] = previous["warehouses"][wh]
                print(f"{wh}: kept previous data")

    if not warehouses:
        sys.exit("no warehouses built")

    # Category / sub-category index, ordered by network reach so the most
    # widely bought categories lead every list in the UI.
    net_longest = network[max(WINDOWS)].to_json()
    catalog = []
    for cat, entry in sorted(
        net_longest["categories"].items(), key=lambda kv: -kv[1]["pen"]
    ):
        catalog.append(
            {
                "name": cat,
                "subs": [
                    s
                    for s, _ in sorted(
                        entry["subs"].items(), key=lambda kv: -kv[1]["pen"]
                    )
                ],
            }
        )

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "windows": [str(w) for w in WINDOWS],
        "businessLines": sorted(INCLUDED_BUSINESS_LINES),
        "catalog": catalog,
        "network": {str(w): network[w].to_json() for w in WINDOWS},
        "warehouses": warehouses,
        "errors": errors,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"wrote {out_path}: {len(warehouses)} warehouses, "
        f"{len(catalog)} categories, {len(errors)} errors"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dashboards/market-penetration/data.json")
