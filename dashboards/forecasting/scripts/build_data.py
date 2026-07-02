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
DEVIATION_SPREADSHEET_ID = "1Q1ChGZ8PQZGhoohnBBVuaGtcdzdhLmgRodmpOOSN8bs"
DEVIATION_RANGE = "'PO Expected Vs Actual Receive Deviation.csv'!A1:G"
CUSTOMERS_SPREADSHEET_ID = "1DlVvTpy1z1Gdv6VATAQtbeP0aUQK-EH0z1GRLfpks80"
CUSTOMERS_RANGE = "'Active SKU/WH Customers Automation.csv'!A1:F"
FEOOS_SPREADSHEET_ID = "1tNBL8WXowviHwF5ywOaYl0xkUQCnr9c56Idmfx7kf8Y"
FEOOS_RANGE = "'Trailing 14 FEOOS.csv'!A1:J"
PRICE_SPREADSHEET_ID = "1tnp8NgcveLolvQ9IoB813LpK4_vkRJN6AzIUszIYikA"
PRICE_RANGE = "'Purchase Price Push.csv'!A1:G"
PODATA_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
PODATA_RANGE = "'PO Data for Automating.csv'!A1:M"
FIRSTFUL_SPREADSHEET_ID = "1xQ4up0z56zvCKZH1g5fLpbgv2R1rFRFt6GL-6kUFUlE"
FIRSTFUL_RANGE = "'First Fulfillment Ledger.csv'!A1:H"
MDSL_SPREADSHEET_ID = "1-uO3LjbNXbmbiN3rUcWvtB0S-urWYqRkuYoaFVL9IiU"
MDSL_RANGE = "'60 Days out MDSL.csv'!A1:J"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# --- Hardcoded forecast assumptions ------------------------------------------
DEMAND_WEIGHTS = {"model": 0.5, "t30": 0.3, "t60": 0.2}
LEAD_TIME_DAYS = 10          # fallback when the item has no mean_lead_time
SAFETY_STOCK_DAYS = 4
REVIEW_PERIOD_DAYS = 7       # ordering cadence covered by the order-up-to target
LAYER_ROUND_FRACTION = 0.75  # round qty up to a full layer when >= 75% of one
PALLET_ROUND_FRACTION = 0.85 # round qty up to a full pallet when >= 85% of one
RELIABILITY_MIN_POS = 3      # pad lead time by a vendor's avg receive delay
                             # only when based on at least this many received POs
NEW_ITEM_DAYS = 45           # "NEW" badge: first fulfillment within this window
EXPIRY_WARN_DAYS = 30        # warn when on-hand stock hits MDSL within this window
MAX_PAD_COVER_DAYS = 45      # never pad a SKU beyond this many days of cover
                             # when building an order up to the vendor MOQ
MIN_ORDER_DOLLARS = 50       # hold trivially small orders with nothing critical
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
    dev_rows, dv = fetch(svc, DEVIATION_SPREADSHEET_ID, DEVIATION_RANGE)
    cust_rows, cu = fetch(svc, CUSTOMERS_SPREADSHEET_ID, CUSTOMERS_RANGE)
    feoos_rows, fe = fetch(svc, FEOOS_SPREADSHEET_ID, FEOOS_RANGE)
    price_rows, pr = fetch(svc, PRICE_SPREADSHEET_ID, PRICE_RANGE)
    podata_rows, po = fetch(svc, PODATA_SPREADSHEET_ID, PODATA_RANGE)
    firstful_rows, ff = fetch(svc, FIRSTFUL_SPREADSHEET_ID, FIRSTFUL_RANGE)
    mdsl_rows, md = fetch(svc, MDSL_SPREADSHEET_ID, MDSL_RANGE)

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

    # PO receive deviation -> per-vendor reliability (avg days late vs expected).
    def parse_date(s):
        try:
            return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            return None

    dev_samples = defaultdict(list)
    for r in dev_rows:
        expected = parse_date(dv(r, "Receive By Date Date"))
        received = parse_date(dv(r, "Shipment Received Utc Date"))
        vendor = re.sub(r"^VEN\d+\s+", "", dv(r, "Full Vendor Name").strip()).lower()
        if vendor and expected and received:
            dev_samples[vendor].append((received - expected).days)

    reliability_stats = {
        v: {"avgDev": round(sum(s) / len(s), 1), "n": len(s)}
        for v, s in dev_samples.items()
    }
    _rel_cache = {}

    def reliability_for(vendor_name):
        key = vendor_name.lower()
        if key in _rel_cache:
            return _rel_cache[key]
        rel = reliability_stats.get(key)
        if rel is None and len(key) >= 6:
            for v, stats in reliability_stats.items():
                if len(v) >= 6 and (key in v or v in key):
                    rel = stats
                    break
        _rel_cache[key] = rel
        return rel

    # Active customers + net revenue per (warehouse, item_id).
    cust_by_key = {}
    for r in cust_rows:
        key = (cu(r, "Warehouse Name").strip(), cu(r, "Item ID").strip())
        if key[0] and key[1] and key not in cust_by_key:
            cust_by_key[key] = {
                "customers": parse_num(cu(r, "# of Ordering Customers")) or 0,
                "revenue": parse_num(cu(r, "net revenue")) or 0,
            }

    # FEOOS events (actual out-of-stocks at order time), trailing 14 days,
    # aggregated per (warehouse, item name).
    feoos_by_key = defaultdict(lambda: {"events": 0, "units": 0.0})
    for r in feoos_rows:
        key = (fe(r, "Warehouse").strip(), fe(r, "Item Name").strip().lower())
        if not key[0] or not key[1]:
            continue
        agg = feoos_by_key[key]
        agg["events"] += 1
        agg["units"] += parse_num(
            fe(r, "Quantity of FEOOS Items Requested in Sales Units")
        ) or 0

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

    # Purchase Price Push: network purchase price per PU, by item uuid.
    price_by_uuid = {}
    for r in price_rows:
        uuid = pr(r, "Item Extid").strip()
        price = parse_num(pr(r, "Purchase Price Dollars"))
        if uuid and price is not None and uuid not in price_by_uuid:
            price_by_uuid[uuid] = price

    # PO Data for Automating: earliest open inbound line per (warehouse, item).
    inbound_by_key = {}
    for r in podata_rows:
        key = (po(r, "Warehouse Name").strip(), po(r, "Item Uuid").strip())
        if not key[0] or not key[1]:
            continue
        ordered = parse_num(po(r, "Purchase Order Units")) or 0
        received = parse_num(po(r, "Quantity Received PU")) or 0
        open_qty = ordered - received
        expected = parse_date(po(r, "Expected Receipt Date Date"))
        if open_qty <= 0 or expected is None:
            continue
        cur = inbound_by_key.get(key)
        if cur is None:
            inbound_by_key[key] = {"d": expected, "q": open_qty}
        else:
            cur["q"] += open_qty
            if expected < cur["d"]:
                cur["d"] = expected

    # First Fulfillment Ledger: items first sold recently are still ramping.
    firstful_by_key = {}
    for r in firstful_rows:
        key = (ff(r, "Warehouse Name").strip(), ff(r, "Item ID").strip())
        d = parse_date(ff(r, "Min Date"))
        if key[0] and key[1] and d and (key not in firstful_by_key or d < firstful_by_key[key]):
            firstful_by_key[key] = d

    # 60 Days out MDSL: on-hand stock hitting minimum deliverable shelf life soon.
    expiry_by_key = {}
    for r in mdsl_rows:
        key = (md(r, "Warehouse Name").strip(), md(r, "Item Name").strip().lower())
        qty = parse_num(md(r, "Quantity Each on Hand")) or 0
        days_to_mdsl = parse_num(md(r, "Days to MDSL"))
        exp_date = parse_date(md(r, "Expiration Date"))
        if not key[0] or not key[1] or qty <= 0 or days_to_mdsl is None:
            continue
        if days_to_mdsl > EXPIRY_WARN_DAYS:
            continue
        cur = expiry_by_key.setdefault(key, {"q": 0.0, "d": exp_date})
        cur["q"] += qty
        if exp_date and (cur["d"] is None or exp_date < cur["d"]):
            cur["d"] = exp_date

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
        vendor_name = m(r, "vendor_name").strip()
        rel = reliability_for(vendor_name) if vendor_name else None
        lead_pad = 0
        if rel and rel["n"] >= RELIABILITY_MIN_POS and rel["avgDev"] > 0:
            lead_pad = round(rel["avgDev"])
        lead += lead_pad

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

        item_id = m(r, "item_id").strip()
        cust = cust_by_key.get((wh, item_id), {})
        oos = feoos_by_key.get((wh, name.lower()))

        cost_pu = v2i.get("costPu")
        if cost_pu is None:
            cost_pu = price_by_uuid.get(item_uuid)

        first = firstful_by_key.get((wh, item_id))
        is_new = first is not None and 0 <= (base - first).days <= NEW_ITEM_DAYS

        inb_raw = inbound_by_key.get((wh, item_uuid))
        inb = None
        if inb_raw:
            inb = {
                "d": inb_raw["d"].isoformat(),
                "q": round(inb_raw["q"], 1),
                "late": inb_raw["d"] < base,
            }

        exp_raw = expiry_by_key.get((wh, name.lower()))
        exp = None
        if exp_raw:
            exp = {
                "q": round(exp_raw["q"]),
                "d": exp_raw["d"].isoformat() if exp_raw["d"] else None,
            }

        items.append({
            "w": wh,
            "n": name,
            "id": item_id,
            "v": vendor_name or "Unknown vendor",
            "vid": m(r, "vendor_id").strip(),
            "rnd": rounded_to,
            "cust": int(cust.get("customers") or 0),
            "rev": round(cust.get("revenue") or 0),
            "oosN": oos["events"] if oos else 0,
            "pad": lead_pad,
            "cpu": cost_pu,
            "new": is_new,
            "inb": inb,
            "exp": exp,
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
    # Orders are DECISIONS, not arithmetic: each PO is auto-built up to the
    # vendor MOQ (padding capped at MAX_PAD_COVER_DAYS of cover per SKU) and
    # carries a recommendation — order / hold / review — with the reason.
    order_groups = defaultdict(list)
    for it in items:
        if it["flag"] and it["sq"] > 0:
            order_groups[(it["w"], it["v"])].append(it)

    # Top-up candidates for MOQ building: same vendor+warehouse SKUs with
    # demand that aren't below their reorder point yet, closest-to-needing first.
    topup_by_group = defaultdict(list)
    for it in items:
        if not it["flag"] and (it["dr"] or 0) > 0:
            topup_by_group[(it["w"], it["v"])].append(it)
    for cands in topup_by_group.values():
        cands.sort(key=lambda x: (x["docIn"] is None, x["docIn"] or 0))

    def contribution_per_pu(it, min_type):
        if min_type == "dollars":
            return it["cpu"]
        if min_type == "pu":
            return 1.0
        if min_type == "weight_lbs":
            return it["v2"].get("caseWeight")
        if min_type == "pallets":
            cpp = it["v2"].get("casesPerPallet")
            return (1.0 / cpp) if cpp else None
        return None

    def pad_room(it, current_qty):
        # PUs this SKU can absorb before exceeding MAX_PAD_COVER_DAYS of cover.
        position = (it["oh"] or 0) + (it["oo"] or 0)
        return int(max(0, math.floor(it["dr"] * MAX_PAD_COVER_DAYS - position - current_qty)))

    def make_line(it, qty, pad=0, fill=False):
        v2i = it["v2"]
        cost_pu = it["cpu"]
        tihi = None
        if v2i.get("ti") and v2i.get("hi"):
            tihi = f"{int(v2i['ti'])}×{int(v2i['hi'])}"
        return {
            "n": it["n"],
            "id": it["id"],
            "cust": it["cust"],
            "oosN": it["oosN"],
            "new": it["new"],
            "inb": it["inb"],
            "exp": it["exp"],
            "unit": it["unit"],
            "qty": qty,
            "pad": pad,
            "fill": fill,
            "tihi": tihi,
            "rnd": it["rnd"] if not fill else None,
            "costPu": round(cost_pu, 2) if cost_pu is not None else None,
            "cost": round(qty * cost_pu, 2) if cost_pu is not None else None,
            "wt": v2i.get("caseWeight"),
            "cpp": v2i.get("casesPerPallet"),
            "oh": it["oh"],
            "oo": it["oo"],
            "dr": it["dr"],
            "docIn": it["docIn"],
            "ob": it["ob"],
        }

    def totals_from(lines):
        cost = pallets = weight = 0.0
        cost_complete = True
        for l in lines:
            if l["costPu"] is None:
                cost_complete = False
            else:
                cost += l["qty"] * l["costPu"]
            if l["cpp"]:
                pallets += l["qty"] / l["cpp"]
            if l["wt"] is not None:
                weight += l["qty"] * l["wt"]
        qty = sum(l["qty"] for l in lines)
        return {
            "lines": len(lines),
            "qty": qty,
            "cost": round(cost, 2) if cost_complete else None,
            "costPartial": None if cost_complete else round(cost, 2),
            "pallets": round(pallets, 2) if pallets else None,
            "weightLbs": round(weight, 1) if weight else None,
        }

    def min_progress(min_type, t):
        return {
            "dollars": t["cost"],
            "pu": t["qty"],
            "pallets": t["pallets"],
            "weight_lbs": t["weightLbs"],
        }.get(min_type)

    orders = []
    for (wh, vendor), group in order_groups.items():
        group.sort(key=lambda x: (x["ob"] or "9999", x["docIn"] or 0))
        lines = [make_line(it, it["sq"]) for it in group]
        line_items = list(group)

        # Vendor minimum: MOQ sheet first (by vendor_id, then name), V2 fallback.
        moq = moq_by_id.get(group[0]["vid"]) or moq_by_name.get(vendor.lower())
        vmin = vmin_type = None
        for it in group:
            v2i = it["v2"]
            if v2i.get("min") and v2i.get("minType"):
                if vmin is None or v2i["min"] > vmin:
                    vmin, vmin_type = v2i["min"], v2i["minType"]
        if moq:
            min_info = {"src": "moq", "type": moq["type"], "note": moq.get("note"),
                        "value": moq.get("value")}
        elif vmin:
            min_info = {"src": "v2", "type": vmin_type, "note": None, "value": vmin}
        else:
            min_info = None

        rec, reason, revisit = "order", None, None
        pad_total = 0

        risky = any(
            it["doc"] is not None and it["doc"] < RED_DAYS
            and (it["cust"] > 0 or it["oosN"] > 0)
            for it in group
        )

        # Rule 1: nobody orders these SKUs — refill is probably dead stock.
        if all(it["cust"] == 0 and it["oosN"] == 0 and not it["new"] for it in group):
            rec, reason = "hold", "No active customers order these SKUs — verify demand before buying"

        # Rule 2: build the order up to the MOQ, within the cover cap.
        t = totals_from(lines)
        if rec == "order" and min_info and min_info.get("value"):
            progress = min_progress(min_info["type"], t)
            if progress is not None and progress < min_info["value"]:
                shortfall = min_info["value"] - progress
                # Phase 1: raise SKUs already on the order (fastest movers first).
                for li, it in sorted(enumerate(line_items), key=lambda p: -(p[1]["dr"] or 0)):
                    if shortfall <= 0:
                        break
                    uv = contribution_per_pu(it, min_info["type"])
                    if not uv or uv <= 0:
                        continue
                    add = min(pad_room(it, lines[li]["qty"]), math.ceil(shortfall / uv))
                    if add <= 0:
                        continue
                    lines[li] = make_line(it, lines[li]["qty"] + add, pad=lines[li]["pad"] + add)
                    pad_total += add
                    shortfall -= add * uv
                # Phase 2: add same-vendor SKUs that are closest to needing a buy.
                for it in topup_by_group.get((wh, vendor), []):
                    if shortfall <= 0:
                        break
                    uv = contribution_per_pu(it, min_info["type"])
                    if not uv or uv <= 0:
                        continue
                    add = min(pad_room(it, 0), math.ceil(shortfall / uv))
                    if add <= 0:
                        continue
                    lines.append(make_line(it, add, pad=add, fill=True))
                    line_items.append(it)
                    pad_total += add
                    shortfall -= add * uv
                t = totals_from(lines)
                if shortfall > 0:
                    # Can't reach the MOQ within the cover cap.
                    daily = sum(
                        (it["dr"] or 0) * (contribution_per_pu(it, min_info["type"]) or 0)
                        for it in group + topup_by_group.get((wh, vendor), [])
                    )
                    if daily > 0:
                        revisit = (base + timedelta(days=math.ceil(shortfall / daily))).isoformat()
                    if risky:
                        rec = "review"
                        reason = (
                            f"Can't reach the MOQ within {MAX_PAD_COVER_DAYS}d of cover, but "
                            "SKUs with active customers are at stockout risk — call the vendor "
                            "or transfer stock"
                        )
                    else:
                        rec = "hold"
                        reason = (
                            f"Reaching the MOQ would exceed {MAX_PAD_COVER_DAYS} days of cover — "
                            "let demand accrue"
                        )

        # Rule 3: order too small to be worth a PO, nothing critical on it.
        if rec == "order" and not risky and t["cost"] is not None and t["cost"] < MIN_ORDER_DOLLARS:
            rec, reason = "hold", (
                f"Under ${MIN_ORDER_DOLLARS} with nothing critical — batch with a future order"
            )

        # Final min status after building.
        if min_info is not None:
            progress = min_progress(min_info.get("type"), t) if min_info.get("value") else None
            min_info["progress"] = round(progress, 2) if progress is not None else None
            min_info["met"] = (
                progress >= min_info["value"]
                if progress is not None and min_info.get("value") else None
            )

        order_by = min((l["ob"] for l in lines if l["ob"]), default=None)
        rel = reliability_for(vendor)
        orders.append({
            "w": wh,
            "vendor": vendor,
            "orderBy": order_by,
            "urgent": rec == "order" and order_by is not None and order_by <= base.isoformat(),
            "rec": rec,
            "reason": reason,
            "revisit": revisit,
            "padPu": pad_total,
            "cust": sum(l["cust"] for l in lines),
            "oosN": sum(l["oosN"] for l in lines),
            "rel": rel if rel and rel["n"] >= RELIABILITY_MIN_POS else None,
            "lines": lines,
            "totals": t,
            "min": min_info,
        })
    rec_rank = {"order": 0, "review": 1, "hold": 2}
    orders.sort(key=lambda x: (
        rec_rank[x["rec"]], x["orderBy"] or "9999", -x["cust"], -x["totals"]["lines"]
    ))

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
                "heldPos": sum(1 for o in wh_orders if o["rec"] != "order"),
                "recentOos": sum(1 for it in subset if it["oosN"] > 0),
                "custAtRisk": sum(
                    it["cust"] for it in tracked if it["doc"] < AMBER_DAYS
                ),
                "orderValue": round(sum(
                    o["totals"]["cost"] or o["totals"]["costPartial"] or 0
                    for o in wh_orders if o["rec"] == "order"
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
        it.pop("rev", None)
        it.pop("pad", None)
        it.pop("cpu", None)

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
            "reliabilityMinPos": RELIABILITY_MIN_POS,
            "newItemDays": NEW_ITEM_DAYS,
            "expiryWarnDays": EXPIRY_WARN_DAYS,
            "maxPadCoverDays": MAX_PAD_COVER_DAYS,
            "minOrderDollars": MIN_ORDER_DOLLARS,
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
