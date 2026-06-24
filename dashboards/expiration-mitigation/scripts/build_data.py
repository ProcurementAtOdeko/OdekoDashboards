#!/usr/bin/env python3
"""Build data.json for the Expiration Mitigation dashboard.

Reads the `60 Days out MDSL.csv` Looker dump, tiers each at-risk lot by
shelf-life lifecycle stage, computes $ at risk, and recommends a
mitigation action (transfer between warehouses, push, markdown, dispose).
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "1-uO3LjbNXbmbiN3rUcWvtB0S-urWYqRkuYoaFVL9IiU"
SHEET_RANGE = "'60 Days out MDSL.csv'!A1:O"
CATALOG_SPREADSHEET_ID = "1cH-rQQNwOFuPb1Xvj5uUSIk1HKl-8U9xYlVFZZrxrAQ"
CATALOG_RANGE = "'All Items Unit Conversions.csv'!A1:C"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

URGENT_MAX_DAYS = 14
WATCH_MAX_DAYS = 60


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def tier_for(days_to_expiration, days_to_mdsl):
    if days_to_expiration is not None and days_to_expiration < 0:
        return "expired"
    if days_to_mdsl is None:
        return None
    if days_to_mdsl < 0:
        return "past_mdsl"
    if days_to_mdsl <= URGENT_MAX_DAYS:
        return "urgent"
    if days_to_mdsl <= WATCH_MAX_DAYS:
        return "watch"
    return None


def daily_cons(cons30):
    if cons30 is None or cons30 <= 0:
        return None
    return cons30 / 30.0


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

    # Catalog bridge: Item Name -> Item Extid (UUID). The MDSL sheet only
    # carries item names, so we join through the catalog dictionary.
    cat_res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=CATALOG_SPREADSHEET_ID, range=CATALOG_RANGE)
        .execute()
    )
    cat_rows = cat_res.get("values", [])
    name_to_uuid = {}
    if cat_rows:
        cat_header = cat_rows[0]
        cat_col = {name: i for i, name in enumerate(cat_header)}
        n_idx = cat_col.get("Item Name")
        u_idx = cat_col.get("Item Extid")
        if n_idx is not None and u_idx is not None:
            for r in cat_rows[1:]:
                if len(r) <= max(n_idx, u_idx):
                    continue
                name = (r[n_idx] or "").strip()
                uuid = (r[u_idx] or "").strip()
                if name and uuid and name not in name_to_uuid:
                    name_to_uuid[name] = uuid

    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        "Warehouse Name",
        "Item Name",
        "Expiration Date",
        "Bin Number",
        "Item Category Name",
        "Quantity Each on Hand",
        "Days to Expiration",
        "Minimum Deliverable Shelf Life",
        "Days to MDSL",
        "Consumption 30 Days",
        "Consumption 60 Days",
        "Consumption 365 Days",
        "Average Cost Dollars Eaches",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    lots = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        wh = r[col["Warehouse Name"]].strip()
        name = r[col["Item Name"]].strip()
        if not wh or not name:
            continue
        qty = parse_num(r[col["Quantity Each on Hand"]]) or 0
        if qty <= 0:
            continue
        days_to_exp = parse_num(r[col["Days to Expiration"]])
        days_to_mdsl = parse_num(r[col["Days to MDSL"]])
        tier = tier_for(days_to_exp, days_to_mdsl)
        if tier is None:
            continue
        cost = parse_num(r[col["Average Cost Dollars Eaches"]]) or 0
        cons30 = parse_num(r[col["Consumption 30 Days"]])
        cons60 = parse_num(r[col["Consumption 60 Days"]])
        cons365 = parse_num(r[col["Consumption 365 Days"]])
        lots.append({
            "warehouse": wh,
            "name": name,
            "itemExtid": name_to_uuid.get(name),
            "category": r[col["Item Category Name"]].strip(),
            "bin": r[col["Bin Number"]].strip(),
            "expirationDate": r[col["Expiration Date"]].strip() or None,
            "mdslDays": parse_num(r[col["Minimum Deliverable Shelf Life"]]),
            "daysToExpiration": days_to_exp,
            "daysToMdsl": days_to_mdsl,
            "onHand": qty,
            "costEach": cost,
            "dollarsAtRisk": round(qty * cost, 2),
            "cons30": cons30,
            "cons60": cons60,
            "cons365": cons365,
            "tier": tier,
        })

    # Build per-(item, WH) capacity table to find transfer candidates.
    # absorb_capacity = how many extra units WH-X could burn through before
    # its own MDSL clock runs out (negative if WH-X is also over-stocked).
    capacity_by_item_wh = defaultdict(dict)
    for lot in lots:
        rate = daily_cons(lot["cons30"])
        if rate is None or lot["daysToMdsl"] is None or lot["daysToMdsl"] <= 0:
            continue
        max_burn = rate * lot["daysToMdsl"]
        absorb = max_burn - lot["onHand"]
        if absorb > 0:
            prev = capacity_by_item_wh[lot["name"]].get(lot["warehouse"], 0)
            capacity_by_item_wh[lot["name"]][lot["warehouse"]] = prev + absorb

    def recommend(lot):
        if lot["tier"] == "expired":
            return {"action": "DISPOSE", "detail": "Past expiration date"}

        rate = daily_cons(lot["cons30"])
        # Self sell-through check (only meaningful pre-MDSL)
        if lot["tier"] in ("urgent", "watch") and rate is not None and lot["daysToMdsl"] > 0:
            days_to_burn = lot["onHand"] / rate
            if days_to_burn <= lot["daysToMdsl"]:
                if lot["tier"] == "urgent" or days_to_burn > 0.7 * lot["daysToMdsl"]:
                    return {
                        "action": "PUSH",
                        "detail": f"Will sell through in ~{days_to_burn:.0f}d at current pace",
                    }
                return {
                    "action": "MONITOR",
                    "detail": f"~{days_to_burn:.0f}d to burn through ({lot['daysToMdsl']:.0f}d to MDSL)",
                }

        # Looking for a transfer destination.
        candidates = [
            (wh, cap) for wh, cap in capacity_by_item_wh.get(lot["name"], {}).items()
            if wh != lot["warehouse"] and cap >= lot["onHand"] * 0.5
        ]
        if candidates:
            candidates.sort(key=lambda x: -x[1])
            best_wh, best_cap = candidates[0]
            return {
                "action": "TRANSFER",
                "detail": f"→ {best_wh} (absorbs ~{best_cap:.0f} ea)",
                "transferTo": best_wh,
            }

        if lot["tier"] == "past_mdsl":
            return {"action": "MARKDOWN / DONATE", "detail": "Past MDSL — can't ship to customers"}
        if lot["tier"] == "urgent":
            if rate is None:
                return {"action": "MARKDOWN", "detail": "No recent consumption"}
            return {"action": "MARKDOWN", "detail": "Won't sell through at current pace"}
        # watch
        if rate is None:
            return {"action": "SUPPRESS REORDER", "detail": "No recent consumption"}
        return {"action": "MONITOR", "detail": "Approaching MDSL"}

    for lot in lots:
        rec = recommend(lot)
        lot["action"] = rec["action"]
        lot["actionDetail"] = rec["detail"]
        lot["transferTo"] = rec.get("transferTo")

    # Summary aggregates
    tier_counts = defaultdict(int)
    tier_dollars = defaultdict(float)
    action_counts = defaultdict(int)
    wh_dollars = defaultdict(float)
    category_dollars = defaultdict(float)
    for lot in lots:
        tier_counts[lot["tier"]] += 1
        tier_dollars[lot["tier"]] += lot["dollarsAtRisk"]
        action_counts[lot["action"]] += 1
        wh_dollars[lot["warehouse"]] += lot["dollarsAtRisk"]
        if lot["category"]:
            category_dollars[lot["category"]] += lot["dollarsAtRisk"]

    total_dollars = sum(l["dollarsAtRisk"] for l in lots)
    transferable_dollars = sum(l["dollarsAtRisk"] for l in lots if l["action"] == "TRANSFER")
    markdown_dollars = sum(
        l["dollarsAtRisk"] for l in lots
        if l["action"].startswith("MARKDOWN") or l["action"] == "DISPOSE"
    )

    tier_order = ["expired", "past_mdsl", "urgent", "watch"]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "lotCount": len(lots),
            "warehouseCount": len(wh_dollars),
            "dollarsAtRisk": round(total_dollars, 2),
            "transferableDollars": round(transferable_dollars, 2),
            "markdownDollars": round(markdown_dollars, 2),
            "expiredCount": tier_counts["expired"],
            "pastMdslCount": tier_counts["past_mdsl"],
            "urgentCount": tier_counts["urgent"],
            "watchCount": tier_counts["watch"],
        },
        "tierBreakdown": [
            {
                "tier": t,
                "count": tier_counts[t],
                "dollars": round(tier_dollars[t], 2),
            }
            for t in tier_order
        ],
        "actionBreakdown": [
            {"action": a, "count": c}
            for a, c in sorted(action_counts.items(), key=lambda x: -x[1])
        ],
        "warehouseDollars": [
            {"warehouse": wh, "dollars": round(d, 2)}
            for wh, d in sorted(wh_dollars.items(), key=lambda x: -x[1])
        ],
        "categoryDollars": [
            {"category": c, "dollars": round(d, 2)}
            for c, d in sorted(category_dollars.items(), key=lambda x: -x[1])[:12]
        ],
        "lots": sorted(
            lots,
            key=lambda x: (
                -x["dollarsAtRisk"],
                x["daysToMdsl"] if x["daysToMdsl"] is not None else 9999,
            ),
        ),
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(lots)} at-risk lots to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
