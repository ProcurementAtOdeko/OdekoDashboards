#!/usr/bin/env python3
"""Build per-warehouse data files for the network Sales Tracker dashboard.

Discovers every "Network Sales Tracker - <WAREHOUSE>.csv" Looker export in
the Looker Data Dumps Drive folder (newest per warehouse), plus the static
DCA1 sheet, aggregates each into data/<WAREHOUSE>.json, and writes a
data/manifest.json the front-end uses to render the warehouse switcher.

Per order line, actual sold units = SO Item Qty / Conversion Rate.
The location/SKU first ORDER date drives new-placement logic: we use the
"Min Order Date" column when the export provides it, otherwise the legacy
"Min Date" (first fulfill date) clamped to the earliest observed order date.
A pair whose first order falls inside the trailing window is a new placement.

Some exports occasionally contain a Looker SQL error instead of data; those
warehouses are recorded in the manifest with status "error" and skipped.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
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
# A fresh build with fewer than this fraction of the previous build's order
# lines is treated as a partially written export rather than real data.
PARTIAL_READ_RATIO = 0.8
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
COL_MIN_ORDER_DATE = "Min Order Date"  # preferred when the export provides it
COL_MIN_DATE = "Min Date"  # legacy: first FULFILL date, lags the order by 1-3 days
COL_ENTERPRISE = "Enterprise"  # optional
COL_BUSINESS_LINE = "Business Line"  # optional

# Business-line categories. Local delivery and ecomm (shipping) are the two
# we split items on; everything else rolls up to "other".
LOCAL_LINES = {"metrobi", "local distribution", "roadie", "pickup"}
ECOMM_LINES = {"shipping", "odeko shipping", "parcel - bulk", "drop ship"}


def line_category(name):
    key = (name or "").strip().lower()
    if key in LOCAL_LINES:
        return "local"
    if key in ECOMM_LINES:
        return "ecomm"
    return "other"


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


def read_existing(out_dir, wh):
    """Previously built data for a warehouse, or None."""
    path = os.path.join(out_dir, f"{wh}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def aggregate(rows, warehouse):
    """Aggregate raw sheet rows for one warehouse into the dashboard JSON."""
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        COL_ITEM_NAME, COL_BRAND, COL_CUSTOMER, COL_ACCOUNT_UUID, COL_DATE,
        COL_WAREHOUSE, COL_QTY, COL_ITEM_UUID, COL_CONVERSION,
    ]
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"missing expected columns: {missing}")
    if COL_MIN_ORDER_DATE not in col and COL_MIN_DATE not in col:
        raise ValueError(f"missing expected columns: ['{COL_MIN_ORDER_DATE}']")
    # Prefer the true first-order date; "Min Date" is the first FULFILL date.
    min_date_col = col.get(COL_MIN_ORDER_DATE, col.get(COL_MIN_DATE))
    has_enterprise = COL_ENTERPRISE in col
    has_business_line = COL_BUSINESS_LINE in col

    items = {}
    customers = {}
    pairs = {}
    weekly = defaultdict(lambda: {"units": 0.0, "lines": 0})
    brand_units = defaultdict(float)
    brands = defaultdict(lambda: {
        "units": 0.0, "lines": 0, "customers": set(), "items": set(),
        "localUnits": 0.0, "ecommUnits": 0.0, "weekly": defaultdict(float),
        "firstOrder": None, "lastOrder": None,
    })
    business_lines = defaultdict(lambda: {"units": 0.0, "lines": 0, "customers": set(), "items": set()})
    max_date = None
    min_order_date = None
    skipped = 0

    # Some lines (legacy / off-platform sales) come without uuids. Map names
    # to the uuid they most often appear with, so those lines merge into the
    # same item/customer instead of creating a duplicate name-keyed row.
    item_uuid_votes = defaultdict(Counter)
    cust_uuid_votes = defaultdict(Counter)
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if r[col[COL_WAREHOUSE]] != warehouse:
            continue
        if r[col[COL_ITEM_UUID]].strip() and r[col[COL_ITEM_NAME]].strip():
            item_uuid_votes[r[col[COL_ITEM_NAME]].strip()][r[col[COL_ITEM_UUID]].strip()] += 1
        if r[col[COL_ACCOUNT_UUID]].strip() and r[col[COL_CUSTOMER]].strip():
            cust_uuid_votes[r[col[COL_CUSTOMER]].strip()][r[col[COL_ACCOUNT_UUID]].strip()] += 1
    item_name_to_uuid = {n: c.most_common(1)[0][0] for n, c in item_uuid_votes.items()}
    cust_name_to_uuid = {n: c.most_common(1)[0][0] for n, c in cust_uuid_votes.items()}

    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if r[col[COL_WAREHOUSE]] != warehouse:
            continue
        item_name = r[col[COL_ITEM_NAME]].strip()
        cust_name = r[col[COL_CUSTOMER]].strip()
        item_uuid = (
            r[col[COL_ITEM_UUID]].strip()
            or item_name_to_uuid.get(item_name)
            or (item_name and "n:" + item_name)
        )
        account_uuid = (
            r[col[COL_ACCOUNT_UUID]].strip()
            or cust_name_to_uuid.get(cust_name)
            or (cust_name and "n:" + cust_name)
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
        bl_name = r[col[COL_BUSINESS_LINE]].strip() if has_business_line else ""
        bl_cat = line_category(bl_name)
        # While the export still carries the fulfill-based "Min Date", clamp
        # to the earliest order date we actually observe for the row, so a
        # first order can never postdate a real order (or land in the future).
        pair_min_date = parse_date(r[min_date_col])
        if pair_min_date is None or order_date < pair_min_date:
            pair_min_date = order_date
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
                "localUnits": 0.0,
                "ecommUnits": 0.0,
            },
        )
        it["units"] += units
        it["lines"] += 1
        it["customers"].add(account_uuid)
        it["weekly"][wk] += units
        if bl_cat == "local":
            it["localUnits"] += units
        elif bl_cat == "ecomm":
            it["ecommUnits"] += units
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
                "blUnits": defaultdict(float),
            },
        )
        cu["units"] += units
        cu["lines"] += 1
        cu["items"].add(item_uuid)
        cu["weekly"][wk] += units
        if bl_name:
            cu["blUnits"][bl_name] += units
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
        brand_name = r[col[COL_BRAND]].strip()
        if brand_name:
            brand_units[brand_name] += units
            br = brands[brand_name]
            br["units"] += units
            br["lines"] += 1
            br["customers"].add(account_uuid)
            br["items"].add(item_uuid)
            br["weekly"][wk] += units
            if bl_cat == "local":
                br["localUnits"] += units
            elif bl_cat == "ecomm":
                br["ecommUnits"] += units
            if br["firstOrder"] is None or (pair_min_date and pair_min_date < br["firstOrder"]):
                br["firstOrder"] = pair_min_date
            if br["lastOrder"] is None or order_date > br["lastOrder"]:
                br["lastOrder"] = order_date
        if bl_name:
            bl = business_lines[bl_name]
            bl["units"] += units
            bl["lines"] += 1
            bl["customers"].add(account_uuid)
            bl["items"].add(item_uuid)

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

    business_lines_out = [
        {
            "name": name,
            "category": line_category(name),
            "units": round(bl["units"], 1),
            "lines": bl["lines"],
            "customers": len(bl["customers"]),
            "items": len(bl["items"]),
        }
        for name, bl in sorted(business_lines.items(), key=lambda kv: -kv[1]["units"])
    ]

    def primary_business_line(bl_units):
        return max(bl_units.items(), key=lambda kv: kv[1])[0] if bl_units else None

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

    brands_out = [
        {
            "name": name,
            "units": round(br["units"], 2),
            "lines": br["lines"],
            "customers": len(br["customers"]),
            "items": len(br["items"]),
            "localUnits": round(br["localUnits"], 1),
            "ecommUnits": round(br["ecommUnits"], 1),
            "firstOrder": iso(br["firstOrder"]),
            "lastOrder": iso(br["lastOrder"]),
            **trend_fields(br["weekly"]),
        }
        for name, br in sorted(brands.items(), key=lambda kv: -kv[1]["units"])
    ]

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
        "businessLines": business_lines_out,
        "brands": brands_out,
        "items": [
            {
                "uuid": it["uuid"],
                "name": it["name"],
                "brand": it["brand"],
                "units": round(it["units"], 2),
                "lines": it["lines"],
                "customers": len(it["customers"]),
                "newLocations": item_new_locations.get(it["uuid"], 0),
                "localUnits": round(it["localUnits"], 1),
                "ecommUnits": round(it["ecommUnits"], 1),
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
                "businessLine": primary_business_line(cu["blUnits"]),
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
            # Looker rewrites these exports in place, so a read can land while
            # only part of the file has been written — which looks like a valid
            # (just much smaller) export and would silently drop SKUs. Treat a
            # big drop in order lines as a partial read and keep what we have;
            # a real 90-day window never loses a fifth of its volume in an hour.
            prev = read_existing(out_dir, wh)
            if prev:
                prev_lines = prev["summary"]["orderLines"]
                if prev_lines and data["summary"]["orderLines"] < PARTIAL_READ_RATIO * prev_lines:
                    raise ValueError(
                        f"partial export: {data['summary']['orderLines']} order lines "
                        f"vs {prev_lines} previously"
                    )
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
        except Exception as e:
            # A bad export (Looker mid-refresh returns no rows or a partially
            # written file, a momentary SQL error) shouldn't drop or shrink a
            # market that already has good data. Carry the previously built file
            # forward and keep the market live; only mark "error" when there's
            # nothing to fall back to.
            prev = read_existing(out_dir, wh)
            if prev:
                manifest["warehouses"].append(
                    {"code": wh, "status": "ok", "stale": True,
                     "summary": prev["summary"], "dateRange": prev["dateRange"]}
                )
                print(f"{wh}: export unavailable ({e}); kept previous data", file=sys.stderr)
                continue
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
