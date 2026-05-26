#!/usr/bin/env python3
"""Build data.json for the DCA1 On Hand & ETA dashboard.

Pulls rows from the source Google Sheet via the Sheets API (service-account
auth), filters to the DCA1 warehouse, aggregates by item, and writes a JSON
file the dashboard front-end consumes.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
SHEET_RANGE = "'On Hand & ETA.csv'!A1:R"
WAREHOUSE_FILTER = "DCA1"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def cover_bucket(days):
    if days is None:
        return "unknown"
    if days < 7:
        return "<7"
    if days < 14:
        return "7-14"
    if days < 30:
        return "14-30"
    if days < 60:
        return "30-60"
    return "60+"


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
        "Warehouse Name",
        "Procurement Vendor",
        "Item Name",
        "Item Extid",
        "On Hand Purchase Units",
        "Quantity on Order Purchase Units",
        "Days of Cover 60 Days Eaches",
        "Consumption 60 Days",
        "Quantity Available Purchase Units",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    items = {}
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if r[col["Warehouse Name"]] != WAREHOUSE_FILTER:
            continue
        uuid = r[col["Item Extid"]]
        if not uuid:
            continue
        entry = items.setdefault(
            uuid,
            {
                "uuid": uuid,
                "name": r[col["Item Name"]],
                "vendor": r[col["Procurement Vendor"]],
                "onHand": 0.0,
                "qtyOnOrder": 0.0,
                "qtyAvailable": 0.0,
                "consumption60": None,
                "daysOfCover": None,
                "lines": 0,
            },
        )
        entry["lines"] += 1
        for src, dst in [
            ("On Hand Purchase Units", "onHand"),
            ("Quantity on Order Purchase Units", "qtyOnOrder"),
            ("Quantity Available Purchase Units", "qtyAvailable"),
        ]:
            v = parse_num(r[col[src]])
            if v is not None:
                entry[dst] += v
        for src, dst in [
            ("Consumption 60 Days", "consumption60"),
            ("Days of Cover 60 Days Eaches", "daysOfCover"),
        ]:
            v = parse_num(r[col[src]])
            if v is not None and entry[dst] is None:
                entry[dst] = v

    items_list = list(items.values())

    # Treat days_of_cover == 0 when consumption is missing as "no signal", not 0
    for it in items_list:
        if it["daysOfCover"] == 0 and not it["consumption60"]:
            it["daysOfCover"] = None

    buckets = defaultdict(int)
    for it in items_list:
        buckets[cover_bucket(it["daysOfCover"])] += 1

    cover_values = [it["daysOfCover"] for it in items_list if it["daysOfCover"] is not None]
    avg_cover = sum(cover_values) / len(cover_values) if cover_values else 0
    low_cover = sum(1 for v in cover_values if v < 14)
    no_incoming = sum(1 for it in items_list if it["qtyOnOrder"] == 0)

    vendor_totals = defaultdict(float)
    for it in items_list:
        vendor_totals[it["vendor"]] += it["onHand"]
    top_vendors = sorted(vendor_totals.items(), key=lambda x: -x[1])[:10]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouse": WAREHOUSE_FILTER,
        "summary": {
            "itemCount": len(items_list),
            "lowCoverCount": low_cover,
            "avgDaysOfCover": round(avg_cover, 1),
            "noIncomingCount": no_incoming,
        },
        "coverDistribution": [
            {"bucket": b, "count": buckets[b]}
            for b in ["<7", "7-14", "14-30", "30-60", "60+", "unknown"]
        ],
        "topVendorsOnHand": [{"vendor": v, "onHand": round(t, 2)} for v, t in top_vendors],
        "items": sorted(
            items_list,
            key=lambda x: (x["daysOfCover"] is None, x["daysOfCover"] or 0),
        ),
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(items_list)} DCA1 items to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
