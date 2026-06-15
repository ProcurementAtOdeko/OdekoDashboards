#!/usr/bin/env python3
"""Build data.json for the PO Approval dashboard.

Reads the Looker-fed "Combined Models Dump" sheet, surfaces every item the
v2 model recommends ordering, computes a trailing-4-week sanity forecast,
joins the latest approval state from the PO Approval Log sheet, and writes
a JSON payload the dashboard consumes.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

MODEL_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODEL_RANGE = "'Warehouse Raw'!A1:BU"
APPROVALS_SPREADSHEET_ID = "19kWUzVFHTzVDb64fU9r83oaKmbO8rgEaNUJG6r2oyOk"
APPROVALS_RANGE = "'PO Approval Log'!A1:K"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_bool(s):
    return str(s).strip().upper() == "TRUE"


def doc_bucket(d):
    if d is None:
        return "unknown"
    if d < 0:
        return "past-due"
    if d < 7:
        return "<7"
    if d < 14:
        return "7-14"
    if d < 30:
        return "14-30"
    return "30+"


def load_approvals(svc):
    """Return {(warehouse, item_uuid): latest_approval_dict}."""
    try:
        res = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=APPROVALS_SPREADSHEET_ID, range=APPROVALS_RANGE)
            .execute()
        )
    except Exception as e:
        print(f"warn: could not read approvals sheet ({e}); proceeding empty", file=sys.stderr)
        return {}
    rows = res.get("values", [])
    if not rows or len(rows) < 2:
        return {}
    header = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    required = ["timestamp_utc", "warehouse", "item_uuid", "status"]
    if any(r not in idx for r in required):
        print(f"warn: approvals sheet missing columns; header={header}", file=sys.stderr)
        return {}
    latest = {}
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        key = (r[idx["warehouse"]], r[idx["item_uuid"]])
        ts = r[idx["timestamp_utc"]]
        entry = {
            "timestamp": ts,
            "approver": r[idx.get("approver_email", -1)] if "approver_email" in idx else "",
            "status": r[idx["status"]],
            "approvedQty": parse_num(r[idx["approved_qty"]]) if "approved_qty" in idx else None,
            "notes": r[idx["notes"]] if "notes" in idx else "",
        }
        prev = latest.get(key)
        if prev is None or (ts and ts > prev["timestamp"]):
            latest[key] = entry
    return latest


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
        .get(spreadsheetId=MODEL_SPREADSHEET_ID, range=MODEL_RANGE)
        .execute()
    )
    rows = res.get("values", [])
    if not rows:
        sys.exit("Model sheet returned no rows")

    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        "warehouse_name", "vendor_name", "item_name", "item_uuid",
        "purchase_unit", "inventory", "net_inventory", "consumption_rate",
        "days_of_cover", "net_days_of_cover", "forecast_days_of_cover",
        "rate_-4", "rate_-3", "rate_-2", "rate_-1",
        "pred_current_month", "pred_next_month", "pred_following_month",
        "v2_order_trigger", "v2_order_quantity_pu", "v2_raw_order_quantity_pu",
        "v2_vendor_min", "v2_vendor_min_type",
        "next_po_delivery_date", "next_po_quantity",
        "on_hand_value", "purchase_units_per_sales_unit",
        "in_catalog", "as_of",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns in model dump: {missing}")

    recs = {}
    as_of = ""
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if not parse_bool(r[col["in_catalog"]]):
            continue
        trigger = parse_bool(r[col["v2_order_trigger"]])
        rec_qty = parse_num(r[col["v2_order_quantity_pu"]]) or 0
        raw_qty = parse_num(r[col["v2_raw_order_quantity_pu"]]) or 0
        if not (trigger or rec_qty > 0 or raw_qty > 0):
            continue

        warehouse = r[col["warehouse_name"]]
        uuid = r[col["item_uuid"]]
        if not warehouse or not uuid:
            continue
        key = (warehouse, uuid)
        as_of = r[col["as_of"]] or as_of

        rates = [parse_num(r[col[f"rate_-{i}"]]) for i in (4, 3, 2, 1)]
        rates_present = [x for x in rates if x is not None]
        trailing_avg_daily = (sum(rates_present) / len(rates_present)) if rates_present else None
        trailing_stdev = statistics.pstdev(rates_present) if len(rates_present) > 1 else 0
        sanity_30d = trailing_avg_daily * 30 if trailing_avg_daily is not None else None

        pred_current = parse_num(r[col["pred_current_month"]])
        # variance: how far is the model from a simple trailing avg projection (positive = model higher)
        variance_pct = None
        if pred_current is not None and sanity_30d is not None and sanity_30d > 0:
            variance_pct = round((pred_current - sanity_30d) / sanity_30d * 100, 1)

        entry = {
            "uuid": uuid,
            "warehouse": warehouse,
            "vendor": r[col["vendor_name"]],
            "name": r[col["item_name"]],
            "purchaseUnit": r[col["purchase_unit"]],
            "onHand": parse_num(r[col["inventory"]]),
            "netOnHand": parse_num(r[col["net_inventory"]]),
            "onHandValue": parse_num(r[col["on_hand_value"]]),
            "daysOfCover": parse_num(r[col["days_of_cover"]]),
            "netDaysOfCover": parse_num(r[col["net_days_of_cover"]]),
            "forecastDoc": r[col["forecast_days_of_cover"]] or None,
            "consumptionRate": parse_num(r[col["consumption_rate"]]),
            "trailingRates": rates,
            "trailingAvgDaily": round(trailing_avg_daily, 4) if trailing_avg_daily is not None else None,
            "trailingStdev": round(trailing_stdev, 4) if trailing_stdev else 0,
            "sanityForecast30d": round(sanity_30d, 2) if sanity_30d is not None else None,
            "predCurrentMonth": pred_current,
            "predNextMonth": parse_num(r[col["pred_next_month"]]),
            "predFollowingMonth": parse_num(r[col["pred_following_month"]]),
            "variancePct": variance_pct,
            "v2Trigger": trigger,
            "v2RawQty": raw_qty,
            "v2RecQty": rec_qty,
            "vendorMin": parse_num(r[col["v2_vendor_min"]]),
            "vendorMinType": r[col["v2_vendor_min_type"]] or None,
            "nextPoDate": r[col["next_po_delivery_date"]] or None,
            "nextPoQty": parse_num(r[col["next_po_quantity"]]),
            "puPerSalesUnit": parse_num(r[col["purchase_units_per_sales_unit"]]),
        }
        # If duplicate (warehouse,item), keep the row with higher rec_qty
        prev = recs.get(key)
        if prev is None or (entry["v2RecQty"] or 0) > (prev["v2RecQty"] or 0):
            recs[key] = entry

    items = list(recs.values())

    # Join approvals
    approvals = load_approvals(svc)
    for it in items:
        a = approvals.get((it["warehouse"], it["uuid"]))
        it["approval"] = a  # None when pending

    # Sort: urgency = lowest net_days_of_cover first, then highest rec qty
    def urgency_key(it):
        doc = it.get("netDaysOfCover")
        if doc is None:
            doc = 9999
        return (doc, -(it.get("v2RecQty") or 0))
    items.sort(key=urgency_key)

    # Aggregates
    warehouses = sorted({it["warehouse"] for it in items})
    vendors = sorted({it["vendor"] for it in items if it["vendor"]})

    pending_count = sum(1 for it in items if not it["approval"] or it["approval"].get("status") not in {"approved", "skipped"})
    approved_count = sum(1 for it in items if it["approval"] and it["approval"].get("status") == "approved")
    past_due = sum(1 for it in items if (it.get("netDaysOfCover") or 0) < 0)
    total_rec_value = sum(
        (it["v2RecQty"] or 0) * (it["onHandValue"] or 0) / (it["onHand"] or 1)
        for it in items
        if it["onHand"] and it["onHandValue"]
    )

    doc_buckets = defaultdict(int)
    for it in items:
        doc_buckets[doc_bucket(it.get("netDaysOfCover"))] += 1

    vendor_totals = defaultdict(lambda: {"qty": 0, "items": 0})
    for it in items:
        vt = vendor_totals[it["vendor"] or "(unknown)"]
        vt["qty"] += it["v2RecQty"] or 0
        vt["items"] += 1
    top_vendors = sorted(
        ({"vendor": v, **t} for v, t in vendor_totals.items()),
        key=lambda x: -x["items"],
    )[:10]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modelAsOf": as_of,
        "summary": {
            "itemCount": len(items),
            "pendingCount": pending_count,
            "approvedCount": approved_count,
            "pastDueCount": past_due,
            "warehouses": len(warehouses),
            "estRecValueUsd": round(total_rec_value, 2),
        },
        "warehouses": warehouses,
        "vendors": vendors,
        "docDistribution": [
            {"bucket": b, "count": doc_buckets[b]}
            for b in ["past-due", "<7", "7-14", "14-30", "30+", "unknown"]
        ],
        "topVendors": top_vendors,
        "items": items,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(items)} recommendations to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
