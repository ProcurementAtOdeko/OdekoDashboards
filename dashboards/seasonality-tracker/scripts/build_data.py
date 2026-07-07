#!/usr/bin/env python3
"""Build data.json for the Seasonality Tracker dashboard.

Reads the Combined Models Dump (`Warehouse Raw`) Looker export, tags
seasonal SKUs by name keywords, and for each (season, warehouse, item)
line computes:

  forecast uplift   = season-window predicted daily rate vs the current
                      consumption rate. Predicted rates come from the
                      model's pred_current_month / pred_next_month /
                      pred_following_month columns (monthly PU totals),
                      so uplift is only available for seasons that
                      overlap the ~90-day prediction horizon.
  season coverage   = projected inventory at season start (net on hand,
                      drained at the current rate, plus the next PO if
                      it lands before the window) divided by forecast
                      season demand.
  expiration risk   = units of the nearest-expiring lot that will NOT be
                      consumed at the current rate before they expire
                      (min_expiration_date / min_expiration_quantity),
                      valued at the item's average on-hand cost.

Risk flags per line:
  stockout   coverage ratio < STOCKOUT_COVERAGE (demand > 0)
  expiring   expiration-risk units > 0 and the lot dies before the
             season window closes

Season keyword lists are intentionally simple - edit SEASONS below to
tune targeting. An item may belong to multiple seasons.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
SHEET_RANGE = "'Warehouse Raw'!A1:BU"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

STOCKOUT_COVERAGE = 0.75   # coverage ratio below this flags stockout risk
WATCH_COVERAGE = 1.0       # below this (but above stockout) flags watch
# Beyond this many days to season start, stockout risk isn't scored (POs
# will land in between and the drain projection is meaningless); lines
# fall back to "preseason" and only expiration risk applies.
STOCKOUT_LOOKAHEAD_DAYS = 90
EXCLUDE_NAME_KEYWORDS = ["s_ample", "sample)"]

# season -> (start month/day, end month/day, keyword list)
SEASONS = {
    "fall": {
        "label": "Fall",
        "start": (9, 1),
        "end": (11, 30),
        "keywords": [
            "pumpkin", "maple", "chai", "apple cider", "cinnamon", "spice",
            "pecan", "caramel apple", "butterscotch", "gourd", "harvest",
            "autumn", "hot cup", "hot chocolate", "cocoa",
        ],
    },
    "winter": {
        "label": "Winter",
        "start": (12, 1),
        "end": (2, 28),
        "keywords": [
            "peppermint", "eggnog", "gingerbread", "candy cane", "holiday",
            "winter", "noel", "toasted marshmallow", "white chocolate",
            "chestnut", "sugar cookie", "mulled", "frosted", "cranberry",
        ],
    },
    "spring": {
        "label": "Spring",
        "start": (3, 1),
        "end": (5, 31),
        "keywords": [
            "lavender", "rose", "cherry blossom", "elderflower", "floral",
            "lilac", "honey", "matcha", "spring", "violet",
        ],
    },
    "summer": {
        "label": "Summer",
        "start": (6, 1),
        "end": (8, 31),
        "keywords": [
            "lemonade", "refresher", "iced", "cold brew", "cold cup",
            "watermelon", "peach", "mango", "passion", "pineapple",
            "coconut", "hibiscus", "frozen", "smoothie", "strawberry",
            "dragon fruit", "guava", "summer",
        ],
    },
}


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


def month_bounds(year, month):
    return date(year, month, 1), date(year, month, days_in_month(year, month))


def season_window(cfg, today):
    """Current-or-next occurrence of the season window."""
    sm, sd = cfg["start"]
    em, ed = cfg["end"]
    for start_year in (today.year - 1, today.year, today.year + 1):
        start = date(start_year, sm, sd)
        end_year = start_year + (1 if (em, ed) < (sm, sd) else 0)
        # clamp Feb end day for leap-year safety
        end = date(end_year, em, min(ed, days_in_month(end_year, em)))
        if end >= today:
            return start, end
    raise AssertionError("unreachable")


def overlap_days(a_start, a_end, b_start, b_end):
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    return max(0, (hi - lo).days + 1)


def season_rate(preds, window_start, window_end, today):
    """Overlap-weighted mean predicted daily rate inside the window.

    preds: list of (month_start, month_end, daily_rate). Only the part of
    each month from `today` forward counts. Returns None with no overlap.
    """
    total_days = 0
    total_pu = 0.0
    for m_start, m_end, daily in preds:
        if daily is None:
            continue
        d = overlap_days(max(m_start, today), m_end, window_start, window_end)
        if d > 0:
            total_days += d
            total_pu += daily * d
    return (total_pu / total_days) if total_days else None


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
        "warehouse_name", "item_id", "item_name", "item_class", "vendor_name",
        "in_catalog", "consumption_rate", "net_inventory", "inventory",
        "on_hand_value", "outstanding_restock_quantity",
        "pred_current_month", "pred_next_month", "pred_following_month",
        "min_expiration_date", "min_expiration_quantity", "shelf_life",
        "next_po_delivery_date", "next_po_quantity", "as_of",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    def get(r, name):
        i = col[name]
        return r[i] if i < len(r) else ""

    # Anchor "today" to the dump's as_of stamp so reruns are reproducible.
    as_of = None
    for r in rows[1:]:
        as_of = parse_date(get(r, "as_of"))
        if as_of:
            break
    today = as_of or datetime.now(timezone.utc).date()

    # The three pred_* columns are monthly PU totals for the current,
    # next, and following calendar months.
    pred_months = []
    y, m = today.year, today.month
    for _ in range(3):
        pred_months.append((y, m))
        y, m = (y + (m == 12), m % 12 + 1)

    windows = {}
    for key, cfg in SEASONS.items():
        start, end = season_window(cfg, today)
        windows[key] = {
            "start": start,
            "end": end,
            "active": start <= today <= end,
            "daysToStart": max(0, (start - today).days),
        }

    lines = []
    for r in rows[1:]:
        name = get(r, "item_name")
        if not name:
            continue
        lname = name.lower()
        if any(k in lname for k in EXCLUDE_NAME_KEYWORDS):
            continue
        seasons = [k for k, cfg in SEASONS.items()
                   if any(kw in lname for kw in cfg["keywords"])]
        if not seasons:
            continue

        net_inv = parse_num(get(r, "net_inventory"))
        in_catalog = get(r, "in_catalog") == "TRUE"
        if not in_catalog and not (net_inv and net_inv > 0):
            continue

        rate = parse_num(get(r, "consumption_rate")) or 0.0
        inventory = parse_num(get(r, "inventory"))
        on_hand_value = parse_num(get(r, "on_hand_value"))
        unit_value = (
            on_hand_value / inventory
            if on_hand_value and inventory and inventory > 0 else None
        )
        on_order = parse_num(get(r, "outstanding_restock_quantity")) or 0.0
        po_date = parse_date(get(r, "next_po_delivery_date"))
        po_qty = parse_num(get(r, "next_po_quantity")) or 0.0

        preds = []
        for (py, pm), cname in zip(
            pred_months,
            ["pred_current_month", "pred_next_month", "pred_following_month"],
        ):
            total = parse_num(get(r, cname))
            m_start, m_end = month_bounds(py, pm)
            daily = total / days_in_month(py, pm) if total is not None else None
            preds.append((m_start, m_end, daily))

        exp_date = parse_date(get(r, "min_expiration_date"))
        exp_qty = parse_num(get(r, "min_expiration_quantity"))
        days_to_exp = (exp_date - today).days if exp_date else None
        exp_units = None
        if exp_qty and exp_qty > 0 and days_to_exp is not None:
            consumed = rate * max(0, days_to_exp)
            exp_units = max(0.0, exp_qty - consumed)

        base = {
            "w": get(r, "warehouse_name"),
            "id": get(r, "item_id"),
            "n": name,
            "cls": get(r, "item_class"),
            "v": get(r, "vendor_name"),
            "rate": round(rate, 4),
            "inv": round(net_inv, 1) if net_inv is not None else None,
            "onOrder": round(on_order, 1),
            "val": round(on_hand_value, 2) if on_hand_value is not None else None,
            "exp": exp_date.isoformat() if exp_date else None,
            "expDays": days_to_exp,
            "cat": in_catalog,
        }

        for key in seasons:
            win = windows[key]
            s_start, s_end = win["start"], win["end"]
            eff_start = max(s_start, today)
            season_days = (s_end - eff_start).days + 1

            s_rate = season_rate(preds, s_start, s_end, today)
            uplift = None
            if s_rate is not None and rate > 0:
                uplift = (s_rate - rate) / rate

            days_to_start = win["daysToStart"]
            proj_inv = None
            coverage = None
            demand_rate = s_rate if s_rate is not None else (rate or None)
            scoreable = days_to_start <= STOCKOUT_LOOKAHEAD_DAYS
            if net_inv is not None and scoreable:
                proj_inv = net_inv - rate * days_to_start
                if po_date and po_qty and today <= po_date < eff_start:
                    proj_inv += po_qty
                proj_inv = max(0.0, proj_inv)
                if demand_rate and demand_rate > 0:
                    coverage = proj_inv / (demand_rate * season_days)

            # expiration risk scoped to the season: leftover units whose
            # lot dies before the window closes
            season_exp_units = None
            season_exp_value = None
            if exp_units is not None and exp_date and exp_date <= s_end:
                season_exp_units = exp_units
                if unit_value is not None:
                    season_exp_value = exp_units * unit_value

            stockout = coverage is not None and coverage < STOCKOUT_COVERAGE
            watch = (
                coverage is not None
                and STOCKOUT_COVERAGE <= coverage < WATCH_COVERAGE
            )
            expiring = bool(season_exp_units and season_exp_units > 0.01)

            lines.append({
                **base,
                "s": key,
                "sRate": round(s_rate, 4) if s_rate is not None else None,
                "uplift": round(uplift, 4) if uplift is not None else None,
                "projInv": round(proj_inv, 1) if proj_inv is not None else None,
                "covRatio": round(coverage, 3) if coverage is not None else None,
                "expUnits": round(season_exp_units, 1) if season_exp_units is not None else None,
                "expValue": round(season_exp_value, 2) if season_exp_value is not None else None,
                "risk": (
                    "both" if stockout and expiring
                    else "stockout" if stockout
                    else "expiring" if expiring
                    else "watch" if watch
                    else "ready" if scoreable
                    else "preseason"
                ),
            })

    warehouses = sorted({ln["w"] for ln in lines})

    # per-season / per-market rollups for the risk chart
    market_risk = {k: defaultdict(lambda: defaultdict(int)) for k in SEASONS}
    for ln in lines:
        market_risk[ln["s"]][ln["w"]][ln["risk"]] += 1

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asOf": today.isoformat(),
        "predWindow": {
            "start": today.isoformat(),
            "end": month_bounds(*pred_months[-1])[1].isoformat(),
        },
        "seasons": [
            {
                "key": k,
                "label": SEASONS[k]["label"],
                "start": windows[k]["start"].isoformat(),
                "end": windows[k]["end"].isoformat(),
                "active": windows[k]["active"],
                "daysToStart": windows[k]["daysToStart"],
                "keywords": SEASONS[k]["keywords"],
            }
            for k in ["spring", "summer", "fall", "winter"]
        ],
        "warehouses": warehouses,
        "marketRisk": {
            k: {w: dict(risks) for w, risks in v.items()}
            for k, v in market_risk.items()
        },
        "lines": lines,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    counts = defaultdict(int)
    for ln in lines:
        counts[ln["s"]] += 1
    print(f"Wrote {len(lines)} season lines to {out_path} "
          f"({dict(counts)}, {len(warehouses)} markets, as_of {today})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
