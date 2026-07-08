#!/usr/bin/env python3
"""Build data.json for the DCA1 Sales Tracker dashboard.

Pulls order lines from the "DCA1 Sales Tracker Trailing 90" Looker export
(Google Sheet, service-account auth), converts SO item quantities to actual
sold units by dividing by the item's conversion rate, aggregates by item,
customer, and customer/SKU pair, and writes a JSON file the dashboard
front-end consumes.

"Min Date" in the source is the location/SKU first order date, so a pair
whose Min Date falls inside the trailing window is a new placement.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs"
SHEET_RANGE = "A1:M"
WAREHOUSE_FILTER = "DCA1"
NEW_PLACEMENT_DAYS = 30
NEW_LOCATION_DAYS = 14
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

COL_ITEM_NAME = "Item Name"
COL_BRAND = "Brand Name"
COL_CUSTOMER = "Customer Name"
COL_ACCOUNT_UUID = "Odeko Account Uuid"
COL_DATE = "Date Date"
COL_WAREHOUSE = "Warehouse Name"
COL_QTY = "SO Item Qty"
COL_ITEM_UUID = "Item Uuid"
COL_CONVERSION = "Conversion Rate"
COL_MIN_DATE = "Min Date"


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def week_start(d):
    return d - timedelta(days=d.weekday())


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=SHEET_RANGE)
        .execute()
    )
    rows = res.get("values", [])
    if not rows:
        sys.exit("Sheet returned no rows")

    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        COL_ITEM_NAME, COL_BRAND, COL_CUSTOMER, COL_ACCOUNT_UUID, COL_DATE,
        COL_WAREHOUSE, COL_QTY, COL_ITEM_UUID, COL_CONVERSION, COL_MIN_DATE,
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    items = {}
    customers = {}
    pairs = {}
    weekly = defaultdict(lambda: {"units": 0.0, "lines": 0})
    brand_units = defaultdict(float)
    max_date = None
    min_order_date = None
    skipped = 0

    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if r[col[COL_WAREHOUSE]] != WAREHOUSE_FILTER:
            continue
        item_uuid = r[col[COL_ITEM_UUID]].strip()
        account_uuid = r[col[COL_ACCOUNT_UUID]].strip()
        qty = parse_num(r[col[COL_QTY]])
        order_date = parse_date(r[col[COL_DATE]])
        if not item_uuid or not account_uuid or qty is None or order_date is None:
            skipped += 1
            continue
        conv = parse_num(r[col[COL_CONVERSION]])
        if not conv:  # missing or zero conversion -> already in sold units
            conv = 1.0
        units = qty / conv
        pair_min_date = parse_date(r[col[COL_MIN_DATE]])

        if max_date is None or order_date > max_date:
            max_date = order_date
        if min_order_date is None or order_date < min_order_date:
            min_order_date = order_date

        it = items.setdefault(
            item_uuid,
            {
                "uuid": item_uuid,
                "name": r[col[COL_ITEM_NAME]],
                "brand": r[col[COL_BRAND]],
                "units": 0.0,
                "lines": 0,
                "customers": set(),
                "firstOrder": None,
                "lastOrder": None,
            },
        )
        it["units"] += units
        it["lines"] += 1
        it["customers"].add(account_uuid)
        if it["firstOrder"] is None or (pair_min_date and pair_min_date < it["firstOrder"]):
            it["firstOrder"] = pair_min_date
        if it["lastOrder"] is None or order_date > it["lastOrder"]:
            it["lastOrder"] = order_date

        cu = customers.setdefault(
            account_uuid,
            {
                "uuid": account_uuid,
                "name": r[col[COL_CUSTOMER]],
                "units": 0.0,
                "lines": 0,
                "items": set(),
                "firstOrder": None,
                "lastOrder": None,
            },
        )
        cu["units"] += units
        cu["lines"] += 1
        cu["items"].add(item_uuid)
        if cu["firstOrder"] is None or (pair_min_date and pair_min_date < cu["firstOrder"]):
            cu["firstOrder"] = pair_min_date
        if cu["lastOrder"] is None or order_date > cu["lastOrder"]:
            cu["lastOrder"] = order_date

        pr = pairs.setdefault(
            (account_uuid, item_uuid),
            {"units": 0.0, "lines": 0, "minDate": pair_min_date, "lastOrder": None},
        )
        pr["units"] += units
        pr["lines"] += 1
        if pair_min_date and (pr["minDate"] is None or pair_min_date < pr["minDate"]):
            pr["minDate"] = pair_min_date
        if pr["lastOrder"] is None or order_date > pr["lastOrder"]:
            pr["lastOrder"] = order_date

        wk = week_start(order_date)
        weekly[wk]["units"] += units
        weekly[wk]["lines"] += 1
        if r[col[COL_BRAND]]:
            brand_units[r[col[COL_BRAND]]] += units

    if not items:
        sys.exit("No rows matched the warehouse filter")

    iso = lambda d: d.isoformat() if d else None

    items_list = sorted(items.values(), key=lambda x: -x["units"])
    customers_list = sorted(customers.values(), key=lambda x: -x["units"])
    item_index = {it["uuid"]: i for i, it in enumerate(items_list)}
    customer_index = {cu["uuid"]: i for i, cu in enumerate(customers_list)}

    new_cutoff = max_date - timedelta(days=NEW_PLACEMENT_DAYS)
    location_cutoff = max_date - timedelta(days=NEW_LOCATION_DAYS)
    item_new_locations = defaultdict(int)
    pairs_out = []
    new_placements = 0
    for (account_uuid, item_uuid), pr in pairs.items():
        is_new = bool(pr["minDate"] and pr["minDate"] >= new_cutoff)
        if is_new:
            new_placements += 1
        if pr["minDate"] and pr["minDate"] >= location_cutoff:
            item_new_locations[item_uuid] += 1
        pairs_out.append(
            {
                "c": customer_index[account_uuid],
                "i": item_index[item_uuid],
                "units": round(pr["units"], 2),
                "lines": pr["lines"],
                "minDate": iso(pr["minDate"]),
                "lastOrder": iso(pr["lastOrder"]),
                "new": is_new,
            }
        )
    pairs_out.sort(key=lambda p: -p["units"])

    new_customers = sum(
        1 for cu in customers_list if cu["firstOrder"] and cu["firstOrder"] >= new_cutoff
    )
    new_locations = sum(
        1 for cu in customers_list if cu["firstOrder"] and cu["firstOrder"] >= location_cutoff
    )

    weeks = sorted(weekly)
    top_brands = sorted(brand_units.items(), key=lambda x: -x[1])[:10]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouse": WAREHOUSE_FILTER,
        "dateRange": {"start": iso(min_order_date), "end": iso(max_date)},
        "newPlacementDays": NEW_PLACEMENT_DAYS,
        "newLocationDays": NEW_LOCATION_DAYS,
        "summary": {
            "totalUnits": round(sum(it["units"] for it in items_list), 1),
            "orderLines": sum(it["lines"] for it in items_list),
            "customerCount": len(customers_list),
            "itemCount": len(items_list),
            "newPlacements": new_placements,
            "newCustomers": new_customers,
            "newLocations": new_locations,
        },
        "weeklyTrend": [
            {"weekStart": iso(w), "units": round(weekly[w]["units"], 1), "lines": weekly[w]["lines"]}
            for w in weeks
        ],
        "topBrands": [{"brand": b, "units": round(u, 1)} for b, u in top_brands],
        "items": [
            {
                "uuid": it["uuid"],
                "name": it["name"],
                "brand": it["brand"],
                "units": round(it["units"], 2),
                "lines": it["lines"],
                "customers": len(it["customers"]),
                "newLocations": item_new_locations.get(it["uuid"], 0),
                "firstOrder": iso(it["firstOrder"]),
                "lastOrder": iso(it["lastOrder"]),
            }
            for it in items_list
        ],
        "customers": [
            {
                "uuid": cu["uuid"],
                "name": cu["name"],
                "units": round(cu["units"], 2),
                "lines": cu["lines"],
                "items": len(cu["items"]),
                "firstOrder": iso(cu["firstOrder"]),
                "lastOrder": iso(cu["lastOrder"]),
            }
            for cu in customers_list
        ],
        "pairs": pairs_out,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(
        f"Wrote {len(items_list)} items, {len(customers_list)} customers, "
        f"{len(pairs_out)} pairs ({skipped} rows skipped) to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
