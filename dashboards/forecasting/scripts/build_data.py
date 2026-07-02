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
  reorder point        = daily demand * (lead time + SAFETY_STOCK_DAYS)
  reorder flag         = on hand + on order <= reorder point

Actionable draft POs: every flagged SKU gets a suggested order quantity that
brings its position up to an order-up-to target (lead time + review period +
safety days of demand). Lines are grouped into vendor-level built orders per
warehouse, with an order-by date, cost totals, and vendor-minimum checks.
Per-item lead times, PU costs, and case/pallet dimensions come from a fourth
export (Combined V2 For Dashboards); lead time falls back to LEAD_TIME_DAYS
when the model has none.

Ti/hi awareness: order quantities round up to a full layer (cases_per_layer)
when within LAYER_ROUND_FRACTION of one, or a full pallet when within
PALLET_ROUND_FRACTION, so suggested buys land on clean pallet math.

Vendor minimums come from the MOQ Surfacing and Automating sheet (typed as
$ spend / case quantity / weight / delivery cadence / no MOQ), matched by
vendor_id then name, with Combined V2's vendor_min as fallback.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- Sources -----------------------------------------------------------------
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:BU"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:R"
CONS30_SPREADSHEET_ID = "1kivMVt86rNXoiOpsfodZ_gdSLZcElVU3P8EFT5WAggI"
CONS30_RANGE = "'Total Cons Network.csv'!A1:G"
V2_SPREADSHEET_ID = "14cQNxWLX4Cqb2Upp-_C6TmRC0-NUNKWYzq4K_3X6mdM"
V2_RANGE = "'Warehouse Raw'!A1:AR"
MOQ_SPREADSHEET_ID = "1zNDxmJETDp6IGFiYL04wZU_lak8CBMNHfoz5KmlFzTs"
MOQ_RANGE = "'Dataset'!A1:G"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# --- Hardcoded forecast assumptions ------------------------------------------
DEMAND_WEIGHTS = {"model": 0.5, "t30": 0.3, "t60": 0.2}
LEAD_TIME_DAYS = 10          # fallback when the item has no mean_lead_time
SAFETY_STOCK_DAYS = 4
REVIEW_PERIOD_DAYS = 7       # ordering cadence covered by the order-up-to target
LAYER_ROUND_FRACTION = 0.75  # round qty up to a full layer when >= 75% of one
PALLET_ROUND_FRACTION = 0.85 # round qty up to a full pallet when >= 85% of one
PROJECTION_WEEKS = 4
DETAIL_HORIZON_DAYS = 60   # items with cover beyond this are aggregated only
DETAIL_CAP_PER_WH = 300    # max detail rows per warehouse in data.json
RED_DAYS = 7
AMBER_DAYS = 14


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def fetch(svc, spreadsheet_id, rng):
    res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
    )
    rows = res.get("values", [])
    if not rows:
        sys.exit(f"{spreadsheet_id} returned no rows for {rng}")
    header = rows[0]
    col = {name.strip(): i for i, name in enumerate(header)}

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    return rows[1:], get


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
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    models_rows, m = fetch(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    onhand_rows, o = fetch(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    cons30_rows, c = fetch(svc, CONS30_SPREADSHEET_ID, CONS30_RANGE)
    v2_rows, v2 = fetch(svc, V2_SPREADSHEET_ID, V2_RANGE)
    moq_rows, mq = fetch(svc, MOQ_SPREADSHEET_ID, MOQ_RANGE)

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

    # Combined V2: lead times, PU costs, vendor minimums, case/pallet dims.
    v2_by_key = {}
    for r in v2_rows:
        key = (v2(r, "warehouse_uuid").strip(), v2(r, "item_uuid").strip())
        if not key[0] or not key[1] or key in v2_by_key:
            continue
        cost_each = parse_num(v2(r, "cost_dollars_per_each"))
        eaches = parse_num(v2(r, "eaches_per_purchase_unit"))
        ti = parse_num(v2(r, "cases_per_layer"))
        hi = parse_num(v2(r, "layers_per_pallet"))
        v2_by_key[key] = {
            "lead": parse_num(v2(r, "mean_lead_time")),
            "costPu": cost_each * eaches if cost_each is not None and eaches else None,
            "min": parse_num(v2(r, "vendor_min")),
            "minType": v2(r, "vendor_min_type").strip(),
            "ti": ti,
            "hi": hi,
            "casesPerPallet": (ti * hi) if ti and hi else None,
            "caseWeight": parse_num(v2(r, "case_weight_lbs")),
        }

    # MOQ Surfacing and Automating: vendor-level order minimums.
    # "MOQ constraint" types: "$ spend", "Quantity" (cases), "Weight",
    # "Cadence (Delivery constraint)", "No MOQ of any type".
    def parse_moq(constraint, measure, qty):
        c = constraint.lower()
        if c.startswith("no moq"):
            return {"type": "none", "note": measure or None}
        if c.startswith("cadence"):
            return {"type": "cadence", "note": measure or None}
        if c.startswith("$"):
            mm = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k?)", measure, re.I)
            if mm:
                value = float(mm.group(1).replace(",", ""))
                if mm.group(2).lower() == "k":
                    value *= 1000
                return {"type": "dollars", "value": value, "note": measure}
            return None
        if c.startswith("quantity"):
            if qty:
                return {"type": "pu", "value": qty, "note": measure}
            return {"type": "unparsed", "note": measure} if measure else None
        if c.startswith("weight"):
            mm = re.search(r"([\d,]+(?:\.\d+)?)\s*lbs", measure, re.I)
            if mm:
                return {
                    "type": "weight_lbs",
                    "value": float(mm.group(1).replace(",", "")),
                    "note": measure,
                }
            return {"type": "unparsed", "note": measure} if measure else None
        return None

    moq_by_id = {}
    moq_by_name = {}
    for r in moq_rows:
        vname = mq(r, "vendor_name").strip()
        vid = mq(r, "vendor_id").strip()
        parsed = parse_moq(
            mq(r, "MOQ constraint").strip(),
            mq(r, "MOQ measure").strip(),
            parse_num(mq(r, "MOQ Quantity")),
        )
        if not vname or parsed is None:
            continue
        if vid and vid not in moq_by_id:
            moq_by_id[vid] = parsed
        if vname.lower() not in moq_by_name:
            moq_by_name[vname.lower()] = parsed

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

        v2i = v2_by_key.get((m(r, "warehouse_uuid").strip(), item_uuid), {})
        lead = v2i.get("lead")
        if not lead or lead <= 0:
            lead = LEAD_TIME_DAYS

        cover = cover_in = rop = None
        flag = False
        order_qty = 0
        order_by = None
        rounded_to = None
        if demand and demand > 0:
            position = (on_hand or 0) + (on_order or 0)
            cover = max((on_hand or 0), 0) / demand
            cover_in = max(position, 0) / demand
            rop = demand * (lead + SAFETY_STOCK_DAYS)
            flag = position <= rop
            if flag:
                target = demand * (lead + REVIEW_PERIOD_DAYS + SAFETY_STOCK_DAYS)
                order_qty = max(1, math.ceil(target - position))
                order_by = out_date(max(0, cover_in - lead))
                # Ti/hi rounding: land on clean pallet math when close.
                cpp = v2i.get("casesPerPallet")
                ti = v2i.get("ti")
                if cpp and (order_qty / cpp) % 1 >= PALLET_ROUND_FRACTION:
                    order_qty = math.ceil(order_qty / cpp) * int(cpp)
                    rounded_to = "pallet"
                elif ti and ti > 1 and (order_qty / ti) % 1 >= LAYER_ROUND_FRACTION:
                    order_qty = math.ceil(order_qty / ti) * int(ti)
                    rounded_to = "layer"

        past_due = (parse_num(m(r, "past_due")) or 0) > 0 or (
            parse_num(m(r, "purchase_orders_past_due")) or 0
        ) > 0

        items.append({
            "w": wh,
            "n": name,
            "v": m(r, "vendor_name").strip() or "Unknown vendor",
            "vid": m(r, "vendor_id").strip(),
            "rnd": rounded_to,
            "cls": m(r, "item_class").strip(),
            "unit": m(r, "purchase_unit").strip(),
            "oh": round(on_hand or 0, 1),
            "oo": round(on_order or 0, 1),
            "val": parse_num(m(r, "on_hand_value")) or 0,
            "dr": round(demand, 3) if demand is not None else None,
            "lt": round(lead, 1),
            "doc": round(cover, 1) if cover is not None else None,
            "docIn": round(cover_in, 1) if cover_in is not None else None,
            "so": out_date(cover),
            "soIn": out_date(cover_in),
            "rop": round(rop, 1) if rop is not None else None,
            "flag": flag,
            "sq": order_qty,
            "ob": order_by,
            "pd": past_due,
            "sig": signals_used,
            "v2": v2i,
        })

    # --- Vendor-level built orders per warehouse --------------------------------
    order_groups = defaultdict(list)
    for it in items:
        if it["flag"] and it["sq"] > 0:
            order_groups[(it["w"], it["v"])].append(it)

    orders = []
    for (wh, vendor), group in order_groups.items():
        group.sort(key=lambda x: (x["ob"] or "9999", x["docIn"] or 0))
        lines = []
        total_cost = pallets = weight = 0.0
        cost_complete = True
        vmin = None
        vmin_type = ""
        for it in group:
            v2i = it["v2"]
            cost_pu = v2i.get("costPu")
            line_cost = round(it["sq"] * cost_pu, 2) if cost_pu is not None else None
            if line_cost is None:
                cost_complete = False
            else:
                total_cost += line_cost
            if v2i.get("casesPerPallet"):
                pallets += it["sq"] / v2i["casesPerPallet"]
            if v2i.get("caseWeight") is not None:
                weight += it["sq"] * v2i["caseWeight"]
            if v2i.get("min") and v2i.get("minType"):
                if vmin is None or v2i["min"] > vmin:
                    vmin, vmin_type = v2i["min"], v2i["minType"]
            tihi = None
            if v2i.get("ti") and v2i.get("hi"):
                tihi = f"{int(v2i['ti'])}×{int(v2i['hi'])}"
            lines.append({
                "n": it["n"],
                "unit": it["unit"],
                "qty": it["sq"],
                "tihi": tihi,
                "rnd": it["rnd"],
                "costPu": round(cost_pu, 2) if cost_pu is not None else None,
                "cost": line_cost,
                "oh": it["oh"],
                "oo": it["oo"],
                "dr": it["dr"],
                "docIn": it["docIn"],
                "ob": it["ob"],
            })
        total_qty = sum(l["qty"] for l in lines)

        # Vendor minimum: MOQ sheet first (by vendor_id, then name), V2 fallback.
        moq = moq_by_id.get(group[0]["vid"]) or moq_by_name.get(vendor.lower())
        min_info = None
        if moq:
            min_info = {"src": "moq", "type": moq["type"], "note": moq.get("note"),
                        "value": moq.get("value"), "progress": None, "met": None}
            if moq.get("value"):
                progress = {
                    "dollars": total_cost if cost_complete else None,
                    "pu": total_qty,
                    "weight_lbs": round(weight, 1) if weight else None,
                }.get(moq["type"])
                min_info["progress"] = round(progress, 2) if progress is not None else None
                min_info["met"] = progress >= moq["value"] if progress is not None else None
        elif vmin:
            progress = {
                "dollars": total_cost if cost_complete else None,
                "pu": total_qty,
                "pallets": round(pallets, 2) if pallets else None,
                "weight_lbs": round(weight, 1) if weight else None,
            }.get(vmin_type)
            min_info = {
                "src": "v2",
                "type": vmin_type,
                "note": None,
                "value": vmin,
                "progress": round(progress, 2) if progress is not None else None,
                "met": progress >= vmin if progress is not None else None,
            }
        order_by = min((l["ob"] for l in lines if l["ob"]), default=None)
        orders.append({
            "w": wh,
            "vendor": vendor,
            "orderBy": order_by,
            "urgent": order_by is not None and order_by <= base.isoformat(),
            "lines": lines,
            "totals": {
                "lines": len(lines),
                "qty": total_qty,
                "cost": round(total_cost, 2) if cost_complete else None,
                "costPartial": None if cost_complete else round(total_cost, 2),
                "pallets": round(pallets, 2) if pallets else None,
                "weightLbs": round(weight, 1) if weight else None,
            },
            "min": min_info,
        })
    orders.sort(key=lambda x: (x["orderBy"] or "9999", -x["totals"]["lines"]))

    # --- Aggregates per warehouse (over ALL items, not just detail rows) -------
    warehouses = sorted({it["w"] for it in items})

    def aggregate(subset, wh_orders):
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
                "draftPos": len(wh_orders),
                "urgentPos": sum(1 for o in wh_orders if o["urgent"]),
                "orderValue": round(sum(
                    o["totals"]["cost"] or o["totals"]["costPartial"] or 0
                    for o in wh_orders
                )),
            },
            "riskMix": {
                "red": sum(1 for it in tracked if it["doc"] < RED_DAYS),
                "amber": sum(1 for it in tracked if RED_DAYS <= it["doc"] < AMBER_DAYS),
                "green": sum(1 for it in tracked if it["doc"] >= AMBER_DAYS),
            },
            "projection": weeks,
        }

    by_wh = {"ALL": aggregate(items, orders)}
    for wh in warehouses:
        by_wh[wh] = aggregate(
            [it for it in items if it["w"] == wh],
            [o for o in orders if o["w"] == wh],
        )

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
        it.pop("v2", None)
        it.pop("unit", None)
        it.pop("vid", None)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asOf": base.isoformat(),
        "assumptions": {
            "demandWeights": DEMAND_WEIGHTS,
            "leadTimeDays": LEAD_TIME_DAYS,
            "safetyStockDays": SAFETY_STOCK_DAYS,
            "reviewPeriodDays": REVIEW_PERIOD_DAYS,
            "layerRoundFraction": LAYER_ROUND_FRACTION,
            "palletRoundFraction": PALLET_ROUND_FRACTION,
            "detailHorizonDays": DETAIL_HORIZON_DAYS,
            "detailCapPerWarehouse": DETAIL_CAP_PER_WH,
            "redDays": RED_DAYS,
            "amberDays": AMBER_DAYS,
        },
        "warehouses": warehouses,
        "aggregates": by_wh,
        "orders": orders,
        "items": detail,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"Wrote {len(detail)} detail items and {len(orders)} draft POs across "
        f"{len(warehouses)} warehouses ({len(items)} total items) to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
