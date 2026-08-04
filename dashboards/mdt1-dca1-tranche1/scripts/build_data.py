#!/usr/bin/env python3
"""Build data.json for the MDT1 → DCA1 conversion — Tranche 1 dashboard.

Same logic as the full mdt1-dca1-conversion dashboard, filtered to the
first tranche of customer locations from the DCA/MDT routing plan.

For a fixed list of MDT1 customer location UUIDs being converted to DCA1,
this answers two questions:

  1. What have these customers been purchasing (from the MDT1 sales feed)?
  2. Which of those SKUs are NOT currently carried in DCA1 — i.e. the
     bring-in candidates the conversion needs.

Sources (all in the Looker Data Dumps folder / Combined models dump):
  - MDT1 sales:   newest "Network Sales Tracker - MDT1.csv" in the Looker
                  folder (per-order-line: item, customer UUID, qty, ...).
  - DCA1 carried: union of three signals so we don't falsely flag a SKU as
                  a gap when DCA1 already has it —
                    * Combined Models Dump "Warehouse Raw" rows where
                      warehouse_name == DCA1 and in_catalog is truthy,
                    * "On Hand & ETA.csv" DCA1 rows (has inventory),
                    * "DCA1 Sales Tracker Trailing 90.csv" (recently sold).

SKUs join across sources by normalized item name (lower-cased, with a
leading "(DUPLICATE) " stripped) because the numeric/uuid item keys are not
populated consistently across these exports.

Actual sold units per line = SO Item Qty / Conversion Rate, matching the
network Sales Tracker convention.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
MDT1_FILE_NAME = "Network Sales Tracker - MDT1.csv"

# DCA1 "carried" signal sources.
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:D"
DCA1_SOLD_SPREADSHEET_ID = "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs"
DCA1_SOLD_RANGE = "A1:C"

WAREHOUSE = "DCA1"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# The MDT1 customer locations being converted to DCA1.
CUSTOMER_UUIDS = [
    "48e19e54-5515-4f80-a414-c479b69c493f", "b2ac9bdb-f07c-441f-8eb7-7d8793636cdb",
    "847b9501-2908-48df-8a65-e5e98a75fe3e", "7a6d3a58-3118-4f4a-b72c-4134e17a12c0",
    "7b890e64-9771-4344-ae84-cb3d03d4ecd9", "fa9f6ea7-5d6b-444f-ab65-53069ca01e26",
    "efb7e2b9-1162-45c4-8dd3-3e44e010e6d4", "0ee21cde-237f-4f88-84a8-fe238d9ddabe",
    "2c3b698a-7b67-4afc-8dbe-a6a617d4ed5c", "ad13b290-d24f-4713-9c19-87dd12c49766",
    "2cdb0293-1e1b-4bc9-b17e-84209f455ee3", "0f33d26d-264d-4e41-8096-99e90ff2bccf",
    "7c3582a4-6f79-4363-a197-43902f3a7a68", "f062df16-1b93-43bc-82ce-10ac9333428b",
    "d8143089-c480-4143-bd2c-a05f54d62cec", "be0f9db5-a0e0-4469-9052-bb8b088ef23b",
]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def get_values(svc, sid, rng):
    return (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sid, range=rng)
        .execute()
        .get("values", [])
    )


def find_mdt1_file(drive):
    """Newest 'Network Sales Tracker - MDT1.csv' in the Looker folder."""
    q = (
        f"'{LOOKER_FOLDER_ID}' in parents and "
        f"name = '{MDT1_FILE_NAME}' and trashed = false"
    )
    res = (
        drive.files()
        .list(
            q=q,
            orderBy="modifiedTime desc",
            pageSize=5,
            fields="files(id,name,modifiedTime)",
        )
        .execute()
    )
    files = res.get("files", [])
    if not files:
        sys.exit(f"Could not find '{MDT1_FILE_NAME}' in Looker folder")
    return files[0]["id"]


def build_carried_set(svc):
    """Normalized names of SKUs DCA1 already carries (union of 3 signals)."""
    carried = defaultdict(set)  # norm_name -> {source, ...}

    def add(name, source):
        k = norm_name(name)
        if k:
            carried[k].add(source)

    def truthy(v):
        return str(v).strip().upper() in ("TRUE", "1", "YES", "Y")

    # 1. Combined Models Dump — DCA1 rows flagged in_catalog.
    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni, ci = col.get("warehouse_name"), col.get("item_name"), col.get("in_catalog")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ci is not None and len(r) > ci and truthy(r[ci]):
                    if ni is not None and len(r) > ni:
                        add(r[ni], "catalog")

    # 2. On Hand & ETA — DCA1 rows (has inventory).
    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni = col.get("Warehouse Name"), col.get("Item Name")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ni is not None and len(r) > ni:
                    add(r[ni], "onhand")

    # 3. DCA1 Sales Tracker Trailing 90 — recently sold at DCA1.
    rows = get_values(svc, DCA1_SOLD_SPREADSHEET_ID, DCA1_SOLD_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        ni = col.get("Item Name")
        for r in rows[1:]:
            if ni is not None and len(r) > ni:
                add(r[ni], "sold90")

    return carried


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    wanted = set(CUSTOMER_UUIDS)
    carried = build_carried_set(svc)

    mdt1_id = find_mdt1_file(drive)
    rows = get_values(svc, mdt1_id, "A1:M")
    if not rows:
        sys.exit("MDT1 sales sheet returned no rows")
    col = {n: i for i, n in enumerate(rows[0])}
    for c in ["Item Name", "Customer Name", "Odeko Account Uuid", "SO Item Qty",
              "Conversion Rate", "Date Date"]:
        if c not in col:
            sys.exit(f"MDT1 sheet missing expected column: {c}")
    c_name, c_brand = col["Item Name"], col.get("Brand Name")
    c_cust, c_uuid = col["Customer Name"], col["Odeko Account Uuid"]
    c_qty, c_conv, c_date = col["SO Item Qty"], col["Conversion Rate"], col["Date Date"]
    c_item_uuid = col.get("Item Uuid")

    skus = {}            # norm_name -> aggregate
    cust_names = {}      # uuid -> most recent/seen customer name
    cust_agg = {}        # uuid -> {units, skus:set, gapSkus:set}
    dates = []

    for r in rows[1:]:
        if len(r) <= c_uuid:
            continue
        uuid = r[c_uuid]
        if uuid not in wanted:
            continue
        name = r[c_name] if len(r) > c_name else ""
        if not name:
            continue
        key = norm_name(name)
        d = r[c_date] if len(r) > c_date else ""
        if d:
            dates.append(d)

        cn = r[c_cust] if len(r) > c_cust else ""
        if cn:
            cust_names.setdefault(uuid, cn)

        qty = parse_num(r[c_qty]) if len(r) > c_qty else None
        conv = parse_num(r[c_conv]) if len(r) > c_conv else None
        units = qty / conv if (qty is not None and conv and conv > 0) else 0.0

        s = skus.get(key)
        if s is None:
            s = skus[key] = {
                "name": name,
                "brand": (r[c_brand] if c_brand is not None and len(r) > c_brand else "") or "",
                "itemUuid": (r[c_item_uuid] if c_item_uuid is not None and len(r) > c_item_uuid else "") or "",
                "units": 0.0,
                "lines": 0,
                "_custs": {},
                "inDca1": key in carried,
                "dca1Sources": sorted(carried.get(key, [])),
            }
        s["units"] += units
        s["lines"] += 1
        sc = s["_custs"].get(uuid)
        if sc is None:
            sc = s["_custs"][uuid] = {"units": 0.0, "lines": 0}
        sc["units"] += units
        sc["lines"] += 1

        ca = cust_agg.setdefault(
            uuid, {"units": 0.0, "skus": set(), "gapSkus": set(), "items": {}}
        )
        ca["units"] += units
        ca["skus"].add(key)
        if key not in carried:
            ca["gapSkus"].add(key)
        ci = ca["items"].get(key)
        if ci is None:
            ci = ca["items"][key] = {
                "name": name,
                "brand": (r[c_brand] if c_brand is not None and len(r) > c_brand else "") or "",
                "units": 0.0,
                "lines": 0,
                "inDca1": key in carried,
            }
        ci["units"] += units
        ci["lines"] += 1

    sku_list = []
    for s in skus.values():
        cust_map = s.pop("_custs")
        s["customers"] = len(cust_map)
        s["custDetail"] = sorted(
            (
                {
                    "uuid": u,
                    "name": cust_names.get(u, ""),
                    "units": round(v["units"], 1),
                    "lines": v["lines"],
                }
                for u, v in cust_map.items()
            ),
            key=lambda x: -x["units"],
        )
        s["units"] = round(s["units"], 1)
        sku_list.append(s)
    sku_list.sort(key=lambda x: -x["units"])

    # Guard: the Looker export is periodically cleared and rewritten. If we
    # catch it mid-refresh (header row present but no data rows, or no rows
    # for any target customer), do NOT overwrite the last-good data.json with
    # an empty snapshot — fail loudly so the caller keeps the existing file.
    if not sku_list:
        sys.exit(
            "MDT1 sheet has no purchase rows for the target customers "
            f"(read {len(rows) - 1} data rows) — source likely mid-refresh; "
            "leaving existing data.json untouched."
        )

    gaps = [s for s in sku_list if not s["inDca1"]]
    covered = [s for s in sku_list if s["inDca1"]]

    # Top gap SKUs and brands (bring-in candidates).
    top_gap_skus = [
        {"name": s["name"], "units": s["units"], "customers": s["customers"]}
        for s in gaps[:20]
    ]
    brand_units = defaultdict(float)
    brand_gapcount = defaultdict(int)
    for s in gaps:
        b = s["brand"] or "(no brand)"
        brand_units[b] += s["units"]
        brand_gapcount[b] += 1
    top_gap_brands = sorted(
        ({"brand": b, "units": round(u, 1), "skus": brand_gapcount[b]}
         for b, u in brand_units.items()),
        key=lambda x: -x["units"],
    )[:12]

    customers = []
    for uuid in CUSTOMER_UUIDS:
        ca = cust_agg.get(uuid)
        items = []
        if ca:
            for it in ca["items"].values():
                items.append({
                    "name": it["name"],
                    "brand": it["brand"],
                    "units": round(it["units"], 1),
                    "lines": it["lines"],
                    "inDca1": it["inDca1"],
                })
            items.sort(key=lambda x: -x["units"])
        customers.append({
            "uuid": uuid,
            "name": cust_names.get(uuid, ""),
            "hasHistory": ca is not None,
            "skus": len(ca["skus"]) if ca else 0,
            "gapSkus": len(ca["gapSkus"]) if ca else 0,
            "units": round(ca["units"], 1) if ca else 0.0,
            "items": items,
        })
    customers.sort(key=lambda c: (-c["units"], c["name"]))

    active = sum(1 for c in customers if c["hasHistory"])
    gap_units = round(sum(s["units"] for s in gaps), 1)
    total_units = round(sum(s["units"] for s in sku_list), 1)
    coverage_pct = round(100 * len(covered) / len(sku_list)) if sku_list else 0

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dateRange": {
            "min": min(dates) if dates else None,
            "max": max(dates) if dates else None,
        },
        "summary": {
            "customersListed": len(CUSTOMER_UUIDS),
            "customersActive": active,
            "skusPurchased": len(sku_list),
            "skusCarried": len(covered),
            "skusGap": len(gaps),
            "coveragePct": coverage_pct,
            "unitsTotal": total_units,
            "gapUnitsTotal": gap_units,
        },
        "coverage": [
            {"status": "Carried in DCA1", "count": len(covered)},
            {"status": "Not carried (gap)", "count": len(gaps)},
        ],
        "topGapSkus": top_gap_skus,
        "topGapBrands": top_gap_brands,
        "skus": sku_list,
        "customers": customers,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {len(sku_list)} SKUs ({len(gaps)} gaps) across {active} active "
        f"customers to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
