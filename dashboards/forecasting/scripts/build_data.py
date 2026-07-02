#!/usr/bin/env python3
"""Build data.json for the Network Stock Forecast dashboard.

Blends three Looker exports into a per-warehouse/item forecast:

  1. Combined Models Dump  -> model consumption rate (PU/day), net inventory,
                              PO status, on-hand value, item/vendor names
  2. On Hand & ETA         -> quantity on order (PU), trailing 60-day
                              consumption (eaches + PU conversion)
  3. Total Cons Network    -> trailing 30-day consumption rate (PU/day)

All forecasting logic is hardcoded here, not pulled from Looker:

  blended daily demand = weighted average of the demand signals present
                         (weights below, renormalized over available signals)
  days of cover        = net on hand / daily demand
  w/ inbound cover     = (net on hand + on order) / daily demand
  safety stock         = daily demand * SAFETY_STOCK_DAYS
  reorder point        = daily demand * (LEAD_TIME_DAYS + SAFETY_STOCK_DAYS)
  reorder flag         = on hand + on order <= reorder point
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _lib.sheets import fetch_table, parse_num, sheets_service

# --- Sources -----------------------------------------------------------------
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:BU"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:R"
CONS30_SPREADSHEET_ID = "1kivMVt86rNXoiOpsfodZ_gdSLZcElVU3P8EFT5WAggI"
CONS30_RANGE = "'Total Cons Network.csv'!A1:G"

# --- Hardcoded forecast assumptions ------------------------------------------
DEMAND_WEIGHTS = {"model": 0.5, "t30": 0.3, "t60": 0.2}
LEAD_TIME_DAYS = 10
SAFETY_STOCK_DAYS = 4
PROJECTION_WEEKS = 4
DETAIL_HORIZON_DAYS = 60   # items with cover beyond this are aggregated only
DETAIL_CAP_PER_WH = 300    # max detail rows per warehouse in data.json
RED_DAYS = 7
AMBER_DAYS = 14


def blend_demand(signals):
    """Weighted average of the demand signals present (PU/day)."""
    total_w = 0.0
    total = 0.0
    used = []
    for key, weight in DEMAND_WEIGHTS.items():
        v = signals.get(key)
        if v is not None:
            total += weight * v
            total_w += weight
            used.append(key)
    if total_w == 0:
        return None, used
    return total / total_w, used


def main(out_path):
    svc = sheets_service()

    models_rows, m = fetch_table(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    onhand_rows, o = fetch_table(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    cons30_rows, c = fetch_table(svc, CONS30_SPREADSHEET_ID, CONS30_RANGE)

    # --- Secondary lookups ----------------------------------------------------
    # On Hand & ETA repeats item-level totals across per-location rows, so take
    # max / first-non-null per (warehouse, item) instead of summing.
    onhand_by_uuid = {}
    onhand_by_name = {}
    for r in onhand_rows:
        wh = o(r, "Warehouse Name").strip()
        if not wh:
            continue
        rec = {
            "onOrder": parse_num(o(r, "Quantity on Order Purchase Units")),
            "cons60": parse_num(o(r, "Consumption 60 Days")),
            "conv": parse_num(o(r, "Purchase Unit Conversion Rate")),
        }
        uuid_key = (wh, o(r, "Item Extid").strip())
        name_key = (wh, o(r, "Item Name").strip().lower())
        for store, key in ((onhand_by_uuid, uuid_key), (onhand_by_name, name_key)):
            if not key[1]:
                continue
            cur = store.setdefault(key, {"onOrder": None, "cons60": None, "conv": None})
            if rec["onOrder"] is not None:
                cur["onOrder"] = max(cur["onOrder"] or 0, rec["onOrder"])
            for f in ("cons60", "conv"):
                if cur[f] is None and rec[f] is not None:
                    cur[f] = rec[f]

    cons30_by_name = {}
    for r in cons30_rows:
        wh = c(r, "Warehouse Name").strip()
        name = c(r, "Item Name").strip().lower()
        rate = parse_num(c(r, "Purchase Unit Cons"))
        if wh and name and rate is not None and (wh, name) not in cons30_by_name:
            cons30_by_name[(wh, name)] = rate

    # --- Base date from the models dump's as_of stamp --------------------------
    base = date.today()
    for r in models_rows:
        stamp = m(r, "as_of").strip()
        if stamp:
            try:
                base = datetime.strptime(stamp.split(" ")[0], "%Y-%m-%d").date()
            except ValueError:
                pass
            break

    def out_date(cover):
        if cover is None or cover > 365:
            return None
        return (base + timedelta(days=cover)).isoformat()

    # --- Walk the primary file, compute the forecast ---------------------------
    items = []
    seen = set()
    for r in models_rows:
        wh = m(r, "warehouse_name").strip()
        item_uuid = m(r, "item_uuid").strip()
        name = m(r, "item_name").strip()
        if not wh or not name:
            continue
        key = (wh, item_uuid or name.lower())
        if key in seen:
            continue
        seen.add(key)

        on_hand = parse_num(m(r, "net_inventory"))
        if on_hand is None:
            on_hand = parse_num(m(r, "inventory"))

        oh = (
            onhand_by_uuid.get((wh, item_uuid))
            or onhand_by_name.get((wh, name.lower()))
            or {}
        )
        t60 = None
        if oh.get("cons60") is not None and (oh.get("conv") or 0) > 0:
            t60 = oh["cons60"] / oh["conv"] / 60.0

        demand, signals_used = blend_demand({
            "model": parse_num(m(r, "consumption_rate")),
            "t30": cons30_by_name.get((wh, name.lower())),
            "t60": t60,
        })

        on_order = oh.get("onOrder")
        if on_order is None:
            on_order = parse_num(m(r, "next_po_quantity")) or 0.0

        if (on_hand or 0) <= 0 and not demand:
            continue

        cover = cover_in = rop = None
        flag = False
        if demand and demand > 0:
            cover = max((on_hand or 0), 0) / demand
            cover_in = max((on_hand or 0) + (on_order or 0), 0) / demand
            rop = demand * (LEAD_TIME_DAYS + SAFETY_STOCK_DAYS)
            flag = ((on_hand or 0) + (on_order or 0)) <= rop

        past_due = (parse_num(m(r, "past_due")) or 0) > 0 or (
            parse_num(m(r, "purchase_orders_past_due")) or 0
        ) > 0

        items.append({
            "w": wh,
            "n": name,
            "v": m(r, "vendor_name").strip(),
            "cls": m(r, "item_class").strip(),
            "oh": round(on_hand or 0, 1),
            "oo": round(on_order or 0, 1),
            "val": parse_num(m(r, "on_hand_value")) or 0,
            "dr": round(demand, 3) if demand is not None else None,
            "doc": round(cover, 1) if cover is not None else None,
            "docIn": round(cover_in, 1) if cover_in is not None else None,
            "so": out_date(cover),
            "soIn": out_date(cover_in),
            "rop": round(rop, 1) if rop is not None else None,
            "flag": flag,
            "pd": past_due,
            "sig": signals_used,
        })

    # --- Aggregates per warehouse (over ALL items, not just detail rows) -------
    warehouses = sorted({it["w"] for it in items})

    def aggregate(subset):
        tracked = [it for it in subset if it["doc"] is not None]
        covers = sorted(it["doc"] for it in tracked)
        median = covers[len(covers) // 2] if covers else None
        weeks = []
        for wk in range(1, PROJECTION_WEEKS + 1):
            d = wk * 7
            weeks.append({
                "week": wk,
                "by": (base + timedelta(days=d)).isoformat(),
                "handOnly": sum(1 for it in tracked if it["doc"] <= d),
                "withInbound": sum(1 for it in tracked if it["docIn"] <= d),
            })
        return {
            "kpis": {
                "skus": len(subset),
                "tracked": len(tracked),
                "atRisk7": sum(1 for it in tracked if it["doc"] < RED_DAYS),
                "atRisk14": sum(1 for it in tracked if it["doc"] < AMBER_DAYS),
                "belowRop": sum(1 for it in tracked if it["flag"]),
                "pastDue": sum(1 for it in subset if it["pd"]),
                "medianCover": round(median, 1) if median is not None else None,
                "onHandValue": round(sum(it["val"] for it in subset)),
            },
            "riskMix": {
                "red": sum(1 for it in tracked if it["doc"] < RED_DAYS),
                "amber": sum(1 for it in tracked if RED_DAYS <= it["doc"] < AMBER_DAYS),
                "green": sum(1 for it in tracked if it["doc"] >= AMBER_DAYS),
            },
            "projection": weeks,
        }

    by_wh = {"ALL": aggregate(items)}
    for wh in warehouses:
        by_wh[wh] = aggregate([it for it in items if it["w"] == wh])

    # --- Detail rows: at-risk items only, capped per warehouse -----------------
    detail = []
    for wh in warehouses:
        rows = [
            it for it in items
            if it["w"] == wh and (
                it["flag"]
                or (it["doc"] is not None and it["doc"] <= DETAIL_HORIZON_DAYS)
                or (it["docIn"] is not None and it["docIn"] <= DETAIL_HORIZON_DAYS)
            )
        ]
        rows.sort(key=lambda x: (x["doc"] is None, x["doc"] or 0))
        detail.extend(rows[:DETAIL_CAP_PER_WH])

    for it in detail:
        it.pop("val", None)
        it.pop("sig", None)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asOf": base.isoformat(),
        "assumptions": {
            "demandWeights": DEMAND_WEIGHTS,
            "leadTimeDays": LEAD_TIME_DAYS,
            "safetyStockDays": SAFETY_STOCK_DAYS,
            "detailHorizonDays": DETAIL_HORIZON_DAYS,
            "detailCapPerWarehouse": DETAIL_CAP_PER_WH,
            "redDays": RED_DAYS,
            "amberDays": AMBER_DAYS,
        },
        "warehouses": warehouses,
        "aggregates": by_wh,
        "items": detail,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"Wrote {len(detail)} detail items across {len(warehouses)} warehouses "
        f"({len(items)} total) to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
