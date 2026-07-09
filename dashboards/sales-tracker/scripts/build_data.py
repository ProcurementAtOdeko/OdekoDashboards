#!/usr/bin/env python3
"""Build per-warehouse data files for the network Sales Tracker dashboard.

Discovers every "Network Sales Tracker - <WAREHOUSE>.csv" Looker export in
the Looker Data Dumps Drive folder (newest per warehouse), plus the static
DCA1 sheet, aggregates each into data/<WAREHOUSE>.json, and writes a
data/manifest.json the front-end uses to render the warehouse switcher.

Per order line, actual sold units = SO Item Qty / Conversion Rate.
"Min Date" is the location/SKU first order date, so a pair whose Min Date
falls inside the trailing window is a new placement.

Some exports occasionally contain a Looker SQL error instead of data; those
warehouses are recorded in the manifest with status "error" and skipped.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
FILE_PATTERN = re.compile(r"^Network Sales Tracker - ([A-Za-z0-9]+)\.csv$")
# Warehouses with dedicated exports that don't follow the network naming.
# Discovered "Network Sales Tracker - <WH>" files override these.
STATIC_SOURCES = {
    "DCA1": "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs",
}
NEW_PLACEMENT_DAYS = 30
NEW_LOCATION_DAYS = 14
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Generic sub-page stub: the warehouse code is the folder name, everything
# else lives in ../app.js and ../style.css. Written for any warehouse that
# doesn't have a page yet, so new exports get a URL automatically.
PAGE_STUB = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="theme-color" content="#FFD100" />
<title>Odeko · Sales Tracker</title>
<link rel="icon" type="image/png" href="../../_shared/odeko-logo.png" />
<link rel="apple-touch-icon" href="../../_shared/odeko-logo.png" />
<link rel="stylesheet" href="../style.css" />
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>
<script src="../app.js"></script>
</body>
</html>
"""

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
COL_ENTERPRISE = "Enterprise"  # optional


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


def iso(d):
    return d.isoformat() if d else None


def aggregate(rows, warehouse):
    """Aggregate raw sheet rows for one warehouse into the dashboard JSON."""
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        COL_ITEM_NAME, COL_BRAND, COL_CUSTOMER, COL_ACCOUNT_UUID, COL_DATE,
        COL_WAREHOUSE, COL_QTY, COL_ITEM_UUID, COL_CONVERSION, COL_MIN_DATE,
    ]
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"missing expected columns: {missing}")
    has_enterprise = COL_ENTERPRISE in col

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
        if r[col[COL_WAREHOUSE]] != warehouse:
            continue
        # Some lines (legacy / off-platform sales) have no uuids; fall back to
        # name-based keys so their volume still counts.
        item_uuid = r[col[COL_ITEM_UUID]].strip() or (
            r[col[COL_ITEM_NAME]].strip() and "n:" + r[col[COL_ITEM_NAME]].strip()
        )
        account_uuid = r[col[COL_ACCOUNT_UUID]].strip() or (
            r[col[COL_CUSTOMER]].strip() and "n:" + r[col[COL_CUSTOMER]].strip()
        )
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
        wk = week_start(order_date)

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
                "weekly": defaultdict(float),
            },
        )
        it["units"] += units
        it["lines"] += 1
        it["customers"].add(account_uuid)
        it["weekly"][wk] += units
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
                "weekly": defaultdict(float),
                "enterprise": False,
            },
        )
        cu["units"] += units
        cu["lines"] += 1
        cu["items"].add(item_uuid)
        cu["weekly"][wk] += units
        if has_enterprise and str(r[col[COL_ENTERPRISE]]).strip().upper() == "TRUE":
            cu["enterprise"] = True
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

        weekly[wk]["units"] += units
        weekly[wk]["lines"] += 1
        if r[col[COL_BRAND]]:
            brand_units[r[col[COL_BRAND]]] += units

    if not items:
        raise ValueError(f"no rows matched warehouse {warehouse}")

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

    # Trend window: last 4 complete weeks (a week is complete once its Sunday
    # has passed), so the in-progress week doesn't drag every trend down.
    complete_weeks = [w for w in weeks if w + timedelta(days=6) <= max_date]
    trend_weeks = complete_weeks[-4:]

    def trend_fields(weekly_units):
        series = [round(weekly_units.get(w, 0.0), 1) for w in trend_weeks]
        if len(series) < 2:
            return {"trend": series, "trendDelta": 0.0, "trendDir": "flat"}
        half = len(series) // 2
        first = sum(series[:half]) / half
        last = sum(series[-half:]) / half
        delta = last - first
        eps = max(0.5, 0.05 * max(first, 1.0))
        direction = "up" if delta > eps else "down" if delta < -eps else "flat"
        return {"trend": series, "trendDelta": round(delta, 1), "trendDir": direction}

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouse": warehouse,
        "dateRange": {"start": iso(min_order_date), "end": iso(max_date)},
        "newPlacementDays": NEW_PLACEMENT_DAYS,
        "newLocationDays": NEW_LOCATION_DAYS,
        "trendWeeks": [iso(w) for w in trend_weeks],
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
                **trend_fields(it["weekly"]),
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
                "enterprise": cu["enterprise"],
                "firstOrder": iso(cu["firstOrder"]),
                "lastOrder": iso(cu["lastOrder"]),
                **trend_fields(cu["weekly"]),
            }
            for cu in customers_list
        ],
        "pairs": pairs_out,
    }, skipped


def discover_sources(drive):
    """Newest 'Network Sales Tracker - <WH>.csv' per warehouse, merged over
    STATIC_SOURCES (discovered files win)."""
    res = drive.files().list(
        q=(
            f"'{LOOKER_FOLDER_ID}' in parents"
            " and name contains 'Network Sales Tracker - '"
            " and mimeType = 'application/vnd.google-apps.spreadsheet'"
            " and trashed = false"
        ),
        orderBy="modifiedTime desc",
        fields="files(id, name, modifiedTime)",
        pageSize=100,
    ).execute()
    sources = {}
    for f in res.get("files", []):  # newest first: keep first file per WH
        m = FILE_PATTERN.match(f["name"])
        if m:
            sources.setdefault(m.group(1).upper(), f["id"])
    for wh, file_id in STATIC_SOURCES.items():
        sources.setdefault(wh, file_id)
    return sources


def main(out_dir):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    sources = discover_sources(drive)
    if not sources:
        sys.exit("No sales tracker sources found")

    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouses": [],
    }
    failures = 0
    for wh in sorted(sources):
        try:
            res = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=sources[wh], range="A1:Z")
                .execute()
            )
            rows = res.get("values", [])
            if not rows:
                raise ValueError("sheet returned no rows")
            data, skipped = aggregate(rows, wh)
            with open(os.path.join(out_dir, f"{wh}.json"), "w") as f:
                json.dump(data, f, separators=(",", ":"))
            page = os.path.join(os.path.dirname(os.path.abspath(out_dir)), wh, "index.html")
            if not os.path.exists(page):
                os.makedirs(os.path.dirname(page), exist_ok=True)
                with open(page, "w") as f:
                    f.write(PAGE_STUB)
                print(f"{wh}: created sub-page {page}")
            manifest["warehouses"].append(
                {"code": wh, "status": "ok", "summary": data["summary"],
                 "dateRange": data["dateRange"]}
            )
            print(
                f"{wh}: {data['summary']['itemCount']} items, "
                f"{data['summary']['customerCount']} customers ({skipped} rows skipped)"
            )
        except Exception as e:  # bad export (e.g. Looker SQL error) -> skip
            failures += 1
            manifest["warehouses"].append(
                {"code": wh, "status": "error", "error": str(e)[:300]}
            )
            print(f"{wh}: SKIPPED - {e}", file=sys.stderr)

    ok = [w for w in manifest["warehouses"] if w["status"] == "ok"]
    if not ok:
        sys.exit("Every warehouse failed; not writing manifest")
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"Wrote manifest with {len(ok)} ok / {failures} failed warehouses to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data")
