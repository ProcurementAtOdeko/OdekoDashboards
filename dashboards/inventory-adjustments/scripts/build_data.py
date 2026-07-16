#!/usr/bin/env python3
"""Build data.json for the Inventory Adjustments dashboard.

Pulls rows from the "Inventory Adjustments (Detailed View) for Automation"
Looker export via the Sheets API (service-account auth). The dashboard leads
with market-level expirations (the "Expired/Donated" adjustment type) and also
compares all adjustment types.

Sign convention in the source sheet: a negative Total Net Amount is a loss /
write-off (inventory removed), a positive amount is a gain (e.g. customer
returns adding value back). Expiration loss is reported here as a positive
dollar magnitude for readability.
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

SPREADSHEET_ID = "1IGygVDmlJIbhbAjpx9E7E-OlmZ6D5qexwCDoNYBwYss"
SHEET_RANGE = "'Inventory Adjustments (Detailed View) for Automation.csv'!A1:M"
EXPIRED_TYPE = "Expired/Donated"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def parse_money(s):
    """Parse a currency string like '$1,234.56', '-$5.00' or '($5.00)'."""
    if s is None or s == "":
        return 0.0
    t = str(s).strip()
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1]
    t = t.replace("$", "").replace(",", "").strip()
    if t.startswith("-"):
        neg = True
        t = t[1:]
    try:
        v = float(t)
    except ValueError:
        return 0.0
    return -v if neg else v


def parse_int(s):
    if s is None or s == "":
        return 0
    t = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return int(round(float(t)))
    except ValueError:
        return 0


def short_type(cat):
    """Last segment of 'Cost of Goods Sold : Inventory Adjustment : X' -> 'X'."""
    if not cat:
        return "Unknown"
    return cat.split(" : ")[-1].strip()


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
        "Inventory Adjustment",
        "Inventory Adjustment Date Date",
        "Item Name",
        "Total Item Count",
        "Total Net Amount",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    ci_wh = col["Warehouse Name"]
    ci_type = col["Inventory Adjustment"]
    ci_date = col["Inventory Adjustment Date Date"]
    ci_item = col["Item Name"]
    ci_count = col["Total Item Count"]
    ci_net = col["Total Net Amount"]

    # Expiration aggregates (loss reported as positive magnitude)
    exp_market = defaultdict(lambda: {"value": 0.0, "units": 0, "lines": 0})
    exp_month = defaultdict(lambda: {"value": 0.0, "units": 0, "lines": 0})
    exp_heat = defaultdict(lambda: defaultdict(float))  # market -> month -> value
    exp_item = defaultdict(lambda: {"value": 0.0, "units": 0, "lines": 0})  # (item, market)

    # All-type comparison (signed net; loss shown separately)
    type_agg = defaultdict(lambda: {"net": 0.0, "units": 0, "lines": 0})

    dates = []

    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        wh = (r[ci_wh] or "").strip()
        if not wh:
            continue
        cat = short_type(r[ci_type])
        net = parse_money(r[ci_net])
        units = parse_int(r[ci_count])
        date = (r[ci_date] or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            dates.append(date)
        month = date[:7] if len(date) >= 7 else "unknown"

        t = type_agg[cat]
        t["net"] += net
        t["units"] += units
        t["lines"] += 1

        if cat == EXPIRED_TYPE:
            loss = -net  # negative net -> positive loss magnitude
            uloss = units  # count is already positive (units expired) for this type
            m = exp_market[wh]
            m["value"] += loss
            m["units"] += uloss
            m["lines"] += 1
            mo = exp_month[month]
            mo["value"] += loss
            mo["units"] += uloss
            mo["lines"] += 1
            if month != "unknown":
                exp_heat[wh][month] += loss
            item = (r[ci_item] or "").strip() or "(unnamed)"
            it = exp_item[(item, wh)]
            it["value"] += loss
            it["units"] += uloss
            it["lines"] += 1

    # --- shape output ---
    markets = sorted(
        (
            {"market": k, "value": round(v["value"], 2), "units": v["units"], "lines": v["lines"]}
            for k, v in exp_market.items()
        ),
        key=lambda x: -x["value"],
    )

    months = sorted(m for m in exp_month if m != "unknown")
    monthly = [
        {
            "month": m,
            "value": round(exp_month[m]["value"], 2),
            "units": exp_month[m]["units"],
            "lines": exp_month[m]["lines"],
        }
        for m in months
    ]

    # heatmap: markets ordered by total loss, months chronological
    heat_market_order = [m["market"] for m in markets]
    matrix = {
        wh: {mo: round(exp_heat[wh].get(mo, 0.0), 2) for mo in months}
        for wh in heat_market_order
    }

    items = sorted(
        (
            {
                "item": item,
                "market": wh,
                "value": round(v["value"], 2),
                "units": v["units"],
                "lines": v["lines"],
            }
            for (item, wh), v in exp_item.items()
        ),
        key=lambda x: -x["value"],
    )

    by_type = sorted(
        (
            {
                "type": k,
                "net": round(v["net"], 2),
                "loss": round(-v["net"], 2) if v["net"] < 0 else 0.0,
                "units": v["units"],
                "lines": v["lines"],
            }
            for k, v in type_agg.items()
        ),
        key=lambda x: x["net"],  # most negative (biggest loss) first
    )

    total_value = round(sum(m["value"] for m in markets), 2)
    total_units = sum(m["units"] for m in markets)
    total_lines = sum(m["lines"] for m in markets)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dateRange": {
            "start": min(dates) if dates else None,
            "end": max(dates) if dates else None,
        },
        "summary": {
            "expiredValue": total_value,
            "expiredUnits": total_units,
            "expiredLines": total_lines,
            "marketCount": len(markets),
            "worstMarket": markets[0]["market"] if markets else None,
            "worstMarketValue": markets[0]["value"] if markets else 0.0,
            "monthsCovered": len(months),
        },
        "markets": markets,
        "monthly": monthly,
        "heatmap": {"markets": heat_market_order, "months": months, "matrix": matrix},
        "items": items,
        "byType": by_type,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {len(markets)} markets, {len(items)} expired item rows, "
        f"${total_value:,.0f} total expiration loss to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
