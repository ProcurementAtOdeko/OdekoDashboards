#!/usr/bin/env python3
"""Build data.json for the Network Overstock dashboard.

Overstock = too much inventory relative to how fast an item actually SELLS.
Per the model brief, the burn rate is driven by *historic sales* (the Network
Sales Tracker exports), NOT the model consumption_rate. Everything else
(on-hand position, cost, expiration lot) comes from the Combined Models Dump.

For each (warehouse, item):

  daily burn (PU/day) = trailing-90-day sold purchase units / 90
                        sold PU per sales line = SO Item Qty / Conversion Rate
  days of cover (DOC)  = net on-hand PU / daily burn
  overstock flag       = DOC > 180  (market by market), OR the item is holding
                         stock with zero trailing-90 sales (non-mover)
  next-30 projection   = daily burn bent by the model's seasonal shape
                         (pred_next_month vs pred_current_month daily rate),
                         so DOC context reflects where demand is heading
  excess $             = value of the on-hand beyond a 180-day target
  expiration-by-burn   = units of the nearest-expiring lot that will NOT sell
                         through before they expire, at the current sales
                         burn, valued at the item's average on-hand cost

Only overstock-flagged items land in each market's table; KPIs summarize them.
Markets are the warehouses that have a Network Sales Tracker export (the
sales-driven model needs real sales); inventory-only warehouses are skipped.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- Sources -----------------------------------------------------------------
LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:BU"
SALES_FILE_PATTERN = re.compile(r"^Network Sales Tracker - ([A-Za-z0-9]+)\.csv$")
SALES_RANGE = "A1:N"
# Warehouses whose sales export doesn't follow the network naming convention.
STATIC_SALES_SOURCES = {
    "DCA1": "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs",
}

# --- Model knobs -------------------------------------------------------------
SALES_WINDOW_DAYS = 90     # trailing sales window that defines the burn rate
DOC_THRESHOLD = 180        # DOC above this is overstock, market by market
NEXT_30_DAYS = 30
EXP_HORIZON_DAYS = 90      # "expiring soon" window for the headline KPI
TOP_BUYERS = 10            # buyers listed per item; the rest roll up to "others"
SEASONAL_FACTOR_CLAMP = (0.1, 5.0)  # keep pred ratios sane

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Sales-tracker columns
COL_DATE = "Date Date"
COL_WAREHOUSE = "Warehouse Name"
COL_QTY = "SO Item Qty"
COL_ITEM_UUID = "Item Uuid"
COL_CONVERSION = "Conversion Rate"
COL_CUSTOMER = "Customer Name"


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return None


def days_in_month(year, month):
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - date(year, month, 1)).days


def iso(d):
    return d.isoformat() if d else None


# --- Sales side --------------------------------------------------------------
def discover_sales_sources(drive):
    """Newest 'Network Sales Tracker - <WH>.csv' per warehouse, plus statics."""
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
        m = SALES_FILE_PATTERN.match(f["name"])
        if m:
            sources.setdefault(m.group(1).upper(), f["id"])
    for wh, file_id in STATIC_SALES_SOURCES.items():
        sources.setdefault(wh, file_id)
    return sources


def sales_burn_for_warehouse(rows, warehouse):
    """Return ({item_uuid: daily_burn_PU}, {item_uuid: {customer: stats}})
    from trailing-90 sales, plus the sheet's max sale date."""
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [COL_DATE, COL_WAREHOUSE, COL_QTY, COL_ITEM_UUID, COL_CONVERSION]
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"missing expected columns: {missing}")
    has_customer = COL_CUSTOMER in col

    # First pass: find the latest sale date so the window is anchored to the
    # data, not the clock (Looker exports lag a day or two).
    max_date = None
    for r in rows[1:]:
        d = parse_date(r[col[COL_DATE]]) if col[COL_DATE] < len(r) else None
        if d and (max_date is None or d > max_date):
            max_date = d
    if max_date is None:
        raise ValueError("no dated sales rows")
    window_start = max_date - timedelta(days=SALES_WINDOW_DAYS - 1)

    units = defaultdict(float)
    buyers = defaultdict(dict)  # uuid -> {customer: {units, lines, last}}
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if r[col[COL_WAREHOUSE]].strip() != warehouse:
            continue
        d = parse_date(r[col[COL_DATE]])
        if d is None or d < window_start or d > max_date:
            continue
        uuid = r[col[COL_ITEM_UUID]].strip()
        qty = parse_num(r[col[COL_QTY]])
        if not uuid or qty is None:
            continue
        conv = parse_num(r[col[COL_CONVERSION]])
        if not conv:  # missing/zero conversion -> qty already in purchase units
            conv = 1.0
        sold = qty / conv
        units[uuid] += sold
        cust = r[col[COL_CUSTOMER]].strip() if has_customer else ""
        if cust:
            b = buyers[uuid].setdefault(cust, {"units": 0.0, "lines": 0, "last": None})
            b["units"] += sold
            b["lines"] += 1
            if b["last"] is None or d > b["last"]:
                b["last"] = d

    burn = {u: total / SALES_WINDOW_DAYS for u, total in units.items()}
    return burn, buyers, max_date


# --- Inventory side ----------------------------------------------------------
def load_inventory(sheets):
    """Read the Combined Models Dump, grouped by warehouse -> list of item
    inventory records. Also returns the dump's as_of date."""
    res = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=MODELS_SPREADSHEET_ID, range=MODELS_RANGE)
        .execute()
    )
    rows = res.get("values", [])
    if not rows:
        sys.exit("Models dump returned no rows")
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        "warehouse_name", "item_name", "item_uuid", "vendor_name", "item_class",
        "in_catalog", "inventory", "net_inventory", "on_hand_value",
        "min_expiration_date", "min_expiration_quantity",
        "pred_current_month", "pred_next_month", "refrigeration_state", "as_of",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Models dump missing columns: {missing}")

    def get(r, name):
        i = col[name]
        return r[i] if i < len(r) else ""

    as_of = None
    for r in rows[1:]:
        as_of = parse_date(get(r, "as_of"))
        if as_of:
            break

    by_wh = defaultdict(list)
    for r in rows[1:]:
        uuid = get(r, "item_uuid").strip()
        wh = get(r, "warehouse_name").strip()
        if not uuid or not wh:
            continue
        net_inv = parse_num(get(r, "net_inventory"))
        inv = parse_num(get(r, "inventory"))
        on_hand_pu = net_inv if net_inv is not None else inv
        if not on_hand_pu or on_hand_pu <= 0:
            continue  # nothing on hand -> can't be overstock
        on_hand_value = parse_num(get(r, "on_hand_value"))
        cost_basis = inv if (inv and inv > 0) else on_hand_pu
        cost_per_pu = (
            on_hand_value / cost_basis
            if on_hand_value is not None and cost_basis else None
        )
        by_wh[wh].append({
            "uuid": uuid,
            "name": get(r, "item_name"),
            "vendor": get(r, "vendor_name"),
            "cls": get(r, "item_class"),
            "refrig": get(r, "refrigeration_state"),
            "onHandPU": on_hand_pu,
            "onHandValue": on_hand_value,
            "costPerPU": cost_per_pu,
            "predCurrent": parse_num(get(r, "pred_current_month")),
            "predNext": parse_num(get(r, "pred_next_month")),
            "expDate": parse_date(get(r, "min_expiration_date")),
            "expQty": parse_num(get(r, "min_expiration_quantity")),
        })
    return by_wh, as_of


def seasonal_factor(rec, today):
    """pred_next daily rate / pred_current daily rate, clamped. None if the
    model has no usable prediction pair."""
    cur, nxt = rec["predCurrent"], rec["predNext"]
    if cur is None or nxt is None or cur <= 0:
        return None
    ny, nm = (today.year + (today.month == 12), today.month % 12 + 1)
    cur_daily = cur / days_in_month(today.year, today.month)
    nxt_daily = nxt / days_in_month(ny, nm)
    if cur_daily <= 0:
        return None
    f = nxt_daily / cur_daily
    lo, hi = SEASONAL_FACTOR_CLAMP
    return max(lo, min(hi, f))


def build_market(wh, inv_records, burn, buyers, today):
    """Compute overstock items + KPIs for one warehouse."""
    items = []
    on_hand_value_total = 0.0
    for rec in inv_records:
        if rec["onHandValue"]:
            on_hand_value_total += rec["onHandValue"]
        daily_burn = burn.get(rec["uuid"], 0.0)
        on_hand = rec["onHandPU"]
        no_sales = daily_burn <= 0
        doc = None if no_sales else on_hand / daily_burn

        overstock = no_sales or (doc is not None and doc > DOC_THRESHOLD)
        if not overstock:
            continue

        cost = rec["costPerPU"]
        excess_units = on_hand if no_sales else max(0.0, on_hand - DOC_THRESHOLD * daily_burn)
        excess_value = excess_units * cost if cost is not None else None

        factor = seasonal_factor(rec, today)
        next30_burn = daily_burn * (factor if factor is not None else 1.0)
        next30_demand = next30_burn * NEXT_30_DAYS

        # Expiration at the current SALES burn: of the nearest-expiring lot,
        # how many units won't sell before they expire?
        exp_days = (rec["expDate"] - today).days if rec["expDate"] else None
        exp_units = None
        exp_value = None
        exp_soon = False
        if rec["expQty"] and rec["expQty"] > 0 and exp_days is not None:
            consumed = daily_burn * max(0, exp_days)
            exp_units = max(0.0, rec["expQty"] - consumed)
            exp_value = exp_units * cost if cost is not None else None
            exp_soon = exp_units > 0 and exp_days <= EXP_HORIZON_DAYS

        # Who is buying this item (trailing window): top buyers by units,
        # remainder rolled up so the payload stays bounded.
        blist = sorted(
            buyers.get(rec["uuid"], {}).items(), key=lambda kv: -kv[1]["units"]
        )
        top = [
            {"name": n, "units": round(v["units"], 1), "lines": v["lines"],
             "last": iso(v["last"])}
            for n, v in blist[:TOP_BUYERS]
        ]
        rest = blist[TOP_BUYERS:]

        items.append({
            "uuid": rec["uuid"],
            "name": rec["name"],
            "vendor": rec["vendor"],
            "cls": rec["cls"],
            "refrig": rec["refrig"],
            "onHandPU": round(on_hand, 1),
            "onHandValue": round(rec["onHandValue"], 2) if rec["onHandValue"] is not None else None,
            "dailyBurn": round(daily_burn, 3),
            "doc": round(doc, 1) if doc is not None else None,
            "noSales": no_sales,
            "seasonalFactor": round(factor, 2) if factor is not None else None,
            "next30Demand": round(next30_demand, 1),
            "excessUnits": round(excess_units, 1),
            "excessValue": round(excess_value, 2) if excess_value is not None else None,
            "expDate": iso(rec["expDate"]),
            "expDays": exp_days,
            "expUnits": round(exp_units, 1) if exp_units is not None else None,
            "expValue": round(exp_value, 2) if exp_value is not None else None,
            "expSoon": exp_soon,
            "buyers": top,
            "othersCount": len(rest),
            "othersUnits": round(sum(v["units"] for _, v in rest), 1),
        })

    # Worst first: non-movers (no sales) pinned on top by excess $, then the
    # slow-movers by days of cover descending.
    items.sort(key=lambda x: (
        0 if x["noSales"] else 1,
        -(x["excessValue"] or 0) if x["noSales"] else -(x["doc"] or 0),
    ))

    kpis = {
        "overstockSkus": len(items),
        "overstockValue": round(sum(i["excessValue"] or 0 for i in items), 2),
        "expiringSkus": sum(1 for i in items if i["expSoon"]),
        "expiringValue": round(sum(i["expValue"] or 0 for i in items if i["expSoon"]), 2),
        "noSalesSkus": sum(1 for i in items if i["noSales"]),
        "onHandValue": round(on_hand_value_total, 2),
        "skusTracked": len(inv_records),
    }
    return {"code": wh, "kpis": kpis, "items": items}


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    inv_by_wh, as_of = load_inventory(sheets)
    today = as_of or datetime.now(timezone.utc).date()

    sources = discover_sales_sources(drive)
    if not sources:
        sys.exit("No sales tracker sources found")

    # A batch of Looker exports is often mid-refresh (empty grid) at any given
    # hour. Carry a market's previous entry forward on a transient failure so
    # it doesn't flicker out of the combined file until the export repopulates.
    prev_markets = {}
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                prev = json.load(f)
            prev_markets = {m["code"]: m for m in prev.get("markets", [])}
        except Exception:
            pass

    markets = []
    for wh in sorted(sources):
        inv_records = inv_by_wh.get(wh)
        if not inv_records:
            print(f"{wh}: no inventory rows in models dump; skipped", file=sys.stderr)
            continue
        try:
            res = (
                sheets.spreadsheets().values()
                .get(spreadsheetId=sources[wh], range=SALES_RANGE)
                .execute()
            )
            rows = res.get("values", [])
            if not rows:
                raise ValueError("sales sheet returned no rows")
            burn, buyers, sales_max = sales_burn_for_warehouse(rows, wh)
        except Exception as e:
            if wh in prev_markets:
                stale = dict(prev_markets[wh])
                stale["stale"] = True
                markets.append(stale)
                print(f"{wh}: sales unavailable ({e}); kept previous data", file=sys.stderr)
            else:
                print(f"{wh}: sales unavailable ({e}); skipped", file=sys.stderr)
            continue
        market = build_market(wh, inv_records, burn, buyers, today)
        market["salesThrough"] = iso(sales_max)
        markets.append(market)
        print(
            f"{wh}: {market['kpis']['overstockSkus']} overstock SKUs "
            f"(${market['kpis']['overstockValue']:,.0f} excess, "
            f"${market['kpis']['expiringValue']:,.0f} expiring)"
        )

    if not markets:
        sys.exit("No markets built; not writing data.json")

    network = {
        "marketsCount": len(markets),
        "overstockSkus": sum(m["kpis"]["overstockSkus"] for m in markets),
        "overstockValue": round(sum(m["kpis"]["overstockValue"] for m in markets), 2),
        "expiringSkus": sum(m["kpis"]["expiringSkus"] for m in markets),
        "expiringValue": round(sum(m["kpis"]["expiringValue"] for m in markets), 2),
        "noSalesSkus": sum(m["kpis"]["noSalesSkus"] for m in markets),
        "onHandValue": round(sum(m["kpis"]["onHandValue"] for m in markets), 2),
    }

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asOf": iso(today),
        "salesWindowDays": SALES_WINDOW_DAYS,
        "docThreshold": DOC_THRESHOLD,
        "next30Days": NEXT_30_DAYS,
        "expHorizonDays": EXP_HORIZON_DAYS,
        "network": network,
        "markets": markets,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"Wrote {len(markets)} markets, {network['overstockSkus']} overstock SKUs, "
        f"${network['overstockValue']:,.0f} excess to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
