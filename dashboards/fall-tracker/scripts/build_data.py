#!/usr/bin/env python3
"""Build data.json for the Fall Tracker dashboard.

A short-horizon, fall-only companion to the Seasonality Tracker. Where that
dashboard scores a full 90-day season across all four seasons, this one looks
at the next SHORT_TERM_DAYS while the fall ramp is actually happening, and
answers three questions:

  pipeline            what inbound stock lands inside the window, and does it
                      land before the item is projected to run dry
                      (date_out_of_stock vs next_po_delivery_date)

  over/under forecast where the trailing actual run-rate sits against the
                      model's forecast rate for the same month. The four
                      rate_-N columns are weekly PU/day actuals, and
                      pred_current_month is a monthly PU total, so dividing
                      by the month's length puts both on a PU/day basis.

  double tap          items that already have stock inbound AND still trip
                      the v2 reorder trigger - i.e. the first order is not
                      going to be enough and a second needs to go out on top
                      of it. These are the ones that get missed, because the
                      open PO makes them look handled.

Fall targeting is keyword-based on item name, split into two tiers:
  core      fall flavor SKUs (pumpkin, maple, cinnamon, ...)
  adjacent  hot-beverage packaging and chai/cocoa, which ramp with the
            season but are not fall-exclusive
Both ship in the payload; the dashboard can filter to either.

The season window starts mid-August on purpose: commercial fall (PSL launch)
runs ahead of meteorological fall, and the trailing actuals confirm the ramp
is already underway in August. Tune SEASON_START / SEASON_END below.
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

SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
SHEET_RANGE = "'Warehouse Raw'!A1:BU"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Short-term lens. The whole point of this dashboard vs the seasonality
# tracker: score what happens inside the next month, not the whole season.
SHORT_TERM_DAYS = 30

# Commercial fall window (see module docstring).
SEASON_START = (8, 15)
SEASON_END = (11, 30)

# Actual-vs-forecast banding. A line is only banded when there is enough
# volume on either side to be worth reading - tiny rates swing wildly.
VARIANCE_BAND = 0.25       # +/-25% off forecast => over / under
MIN_BAND_RATE = 0.05       # PU/day floor on max(recent, forecast)

CORE_KEYWORDS = [
    "pumpkin", "maple", "apple cider", "caramel apple", "pecan",
    "butterscotch", "autumn", "gourd", "cinnamon", "spice",
]
ADJACENT_KEYWORDS = ["chai", "hot cup", "hot chocolate", "cocoa"]
EXCLUDE_NAME_KEYWORDS = ["s_ample", "sample)"]

# "harvest" is deliberately absent: it matches brand names far more often
# than fall product (Harmless Harvest coconut water, Sweet Harvest Foods
# honey, Harvest Greens puree) and contributed almost no real fall SKUs.


def keyword_pattern(words):
    """Whole-word keyword matcher, tolerant of simple plural/past forms.

    Substring matching is too loose for item names: 'maple' hits the dairy
    brand Mapleline. Matching on word boundaries keeps 'Maple Syrup' while
    dropping 'Mapleline', and the optional suffix still catches 'Spiced'
    and 'Chais'.
    """
    body = "|".join(re.escape(w) for w in words)
    return re.compile(rf"\b(?:{body})(?:s|d|es|ed)?\b", re.IGNORECASE)


CORE_RE = keyword_pattern(CORE_KEYWORDS)
ADJACENT_RE = keyword_pattern(ADJACENT_KEYWORDS)


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_loose_num(s):
    """Numbers that arrive with a trailing qualifier, e.g. days of cover '84+'."""
    if s is None or s == "":
        return None
    m = re.match(r"\s*-?\d+(\.\d+)?", str(s))
    return float(m.group(0)) if m else None


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


def season_window(today):
    """Current-or-next occurrence of the fall window."""
    sm, sd = SEASON_START
    em, ed = SEASON_END
    for start_year in (today.year - 1, today.year, today.year + 1):
        start = date(start_year, sm, sd)
        end_year = start_year + (1 if (em, ed) < (sm, sd) else 0)
        end = date(end_year, em, min(ed, days_in_month(end_year, em)))
        if end >= today:
            return start, end
    raise AssertionError("unreachable")


def mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def r(v, d=1):
    return round(v, d) if v is not None else None


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
        "on_hand_value", "outstanding_restock_quantity", "arriving_today",
        "past_due", "purchase_unit", "date_out_of_stock", "delivery_date",
        "thru_date", "est_days_of_cover",
        "net_days_of_cover", "rate_-1", "rate_-2", "rate_-3", "rate_-4",
        "pred_current_month", "pred_next_month", "next_po_delivery_date",
        "next_po_quantity", "v2_order_trigger", "v2_order_quantity_pu",
        "v2_vendor_min", "as_of",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    def get(row, name):
        i = col[name]
        return row[i] if i < len(row) else ""

    # Anchor "today" to the dump's as_of stamp so reruns are reproducible.
    as_of = None
    for row in rows[1:]:
        as_of = parse_date(get(row, "as_of"))
        if as_of:
            break
    today = as_of or datetime.now(timezone.utc).date()

    win_end = today + timedelta(days=SHORT_TERM_DAYS - 1)
    season_start, season_end = season_window(today)

    # pred_current_month / pred_next_month are monthly PU totals for the
    # calendar month containing as_of and the one after it.
    cur_y, cur_m = today.year, today.month
    nxt_y, nxt_m = (cur_y + (cur_m == 12), cur_m % 12 + 1)
    cur_days = days_in_month(cur_y, cur_m)
    nxt_days = days_in_month(nxt_y, nxt_m)

    # Trailing weekly buckets: rate_-1 is the week ending today, rate_-4 the
    # week ending three weeks before that.
    week_windows = []
    for i in (4, 3, 2, 1):
        w_end = today - timedelta(days=(i - 1) * 7)
        week_windows.append({
            "key": f"w-{i}",
            "start": (w_end - timedelta(days=6)).isoformat(),
            "end": w_end.isoformat(),
        })

    lines = []
    for row in rows[1:]:
        name = get(row, "item_name")
        if not name:
            continue
        lname = name.lower()
        if any(k in lname for k in EXCLUDE_NAME_KEYWORDS):
            continue
        is_core = bool(CORE_RE.search(name))
        is_adj = bool(ADJACENT_RE.search(name))
        if not (is_core or is_adj):
            continue

        net_inv = parse_num(get(row, "net_inventory"))
        in_catalog = get(row, "in_catalog") == "TRUE"
        if not in_catalog and not (net_inv and net_inv > 0):
            continue

        rate = parse_num(get(row, "consumption_rate")) or 0.0
        inventory = parse_num(get(row, "inventory"))
        on_hand_value = parse_num(get(row, "on_hand_value"))
        unit_value = (
            on_hand_value / inventory
            if on_hand_value and inventory and inventory > 0 else None
        )

        weeks = [parse_num(get(row, f"rate_-{i}")) for i in (4, 3, 2, 1)]
        prior = mean(weeks[0:2])     # weeks -4, -3
        recent = mean(weeks[2:4])    # weeks -2, -1

        ramp = None
        new_demand = False
        if recent is not None and prior is not None:
            if prior > 0:
                ramp = (recent - prior) / prior
            elif recent > 0:
                new_demand = True

        pred_cur = parse_num(get(row, "pred_current_month"))
        pred_nxt = parse_num(get(row, "pred_next_month"))
        fcst = pred_cur / cur_days if pred_cur is not None else None
        fcst_next = pred_nxt / nxt_days if pred_nxt is not None else None

        variance = None
        band = None
        if fcst is not None and recent is not None:
            if max(recent, fcst) >= MIN_BAND_RATE:
                if fcst > 0:
                    variance = (recent - fcst) / fcst
                    band = (
                        "over" if variance >= VARIANCE_BAND
                        else "under" if variance <= -VARIANCE_BAND
                        else "on"
                    )
                elif recent > 0:
                    band = "over"  # demand with no forecast behind it

        # Forward expectation: where the model thinks the rate goes next month
        # relative to where it actually is now.
        exp_ramp = None
        if fcst_next is not None and recent is not None and recent > 0:
            exp_ramp = (fcst_next - recent) / recent

        on_order = parse_num(get(row, "outstanding_restock_quantity")) or 0.0
        arriving = parse_num(get(row, "arriving_today")) or 0.0
        past_due = parse_num(get(row, "past_due")) or 0.0
        po_date = parse_date(get(row, "next_po_delivery_date"))
        po_qty = parse_num(get(row, "next_po_quantity")) or 0.0
        has_pipeline = on_order > 0 or (po_qty > 0 and po_date is not None)
        po_in_window = bool(po_date and today <= po_date <= win_end)

        oos = parse_date(get(row, "date_out_of_stock"))
        oos_days = (oos - today).days if oos else None
        cover = (
            parse_loose_num(get(row, "net_days_of_cover"))
            if get(row, "net_days_of_cover")
            else parse_loose_num(get(row, "est_days_of_cover"))
        )

        # Earliest arrival achievable by ordering on the model's order_date,
        # i.e. the lead time a fresh order actually faces.
        eta = parse_date(get(row, "delivery_date"))
        thru = parse_date(get(row, "thru_date"))

        # Short-term status. Running dry inside the window is normal here -
        # the model reorders continuously, so most lines project a stockout
        # within a month. What matters is whether anything lands in time:
        #   tight     an already-placed PO arrives on or before the dry date
        #   gap       nothing inbound in time, but ordering now still bridges
        #   critical  even a fresh order lands after the dry date
        dry_in_window = oos_days is not None and oos_days <= SHORT_TERM_DAYS
        po_saves = bool(po_date and oos and po_date <= oos)
        gap_days = None
        if dry_in_window and po_saves:
            status = "tight"
        elif dry_in_window:
            status = "critical" if (eta and oos and eta > oos) else "gap"
            if po_date and oos:
                gap_days = (po_date - oos).days
        elif oos_days is None and rate <= 0:
            status = "idle"
        else:
            status = "ok"

        # Size the exposure. A binary flag is not much use when a third of
        # the book runs dry inside the window, so quantify how long the item
        # actually sits at zero before anything lands, and what that costs.
        # A past-due PO is clamped to today rather than trusted at its stale
        # date - `past` carries the past-due units for anyone checking.
        arrivals = [max(d, today) for d in (po_date, eta) if d]
        arrival = min(arrivals) if arrivals else None
        # Exposure is clipped to the window on both ends: this dashboard only
        # claims what is at risk inside the next SHORT_TERM_DAYS. An item with
        # nothing inbound at all stays dry through the end of the window.
        dry_days = 0
        if oos:
            dry_start = max(oos, today)
            dry_end = min(arrival, win_end) if arrival else win_end
            dry_days = max(0, (dry_end - dry_start).days)
        # Prefer the forward-looking rate: this is demand that has not
        # happened yet, and fall rates are climbing.
        miss_rate = next(
            (x for x in (fcst_next, recent, rate) if x not in (None, 0)), 0.0
        )
        miss_units = dry_days * miss_rate
        miss_val = miss_units * unit_value if unit_value is not None else None

        trigger = get(row, "v2_order_trigger") == "TRUE"
        order_qty = parse_num(get(row, "v2_order_quantity_pu")) or 0.0
        vendor_min = parse_num(get(row, "v2_vendor_min"))
        double_tap = bool(has_pipeline and trigger)

        lines.append({
            "w": get(row, "warehouse_name"),
            "id": get(row, "item_id"),
            "n": name,
            "cls": get(row, "item_class"),
            "v": get(row, "vendor_name"),
            "pu": get(row, "purchase_unit"),
            "tier": "core" if is_core else "adj",
            "cat": in_catalog,
            # demand
            "rate": r(rate, 4),
            "wk": [r(w, 4) for w in weeks],
            "rec": r(recent, 4),
            "pri": r(prior, 4),
            "ramp": r(ramp, 4),
            "new": new_demand,
            "fc": r(fcst, 4),
            "fcn": r(fcst_next, 4),
            "var": r(variance, 4),
            "band": band,
            "xRamp": r(exp_ramp, 4),
            # stock + pipeline
            "inv": r(net_inv, 1),
            "onOrder": r(on_order, 1),
            "arr": r(arriving, 1),
            "past": r(past_due, 1),
            "poDate": po_date.isoformat() if po_date else None,
            "poQty": r(po_qty, 1),
            "poIn": po_in_window,
            "oos": oos.isoformat() if oos else None,
            "oosDays": oos_days,
            "eta": eta.isoformat() if eta else None,
            "thru": thru.isoformat() if thru else None,
            "cover": r(cover, 1),
            "val": r(on_hand_value, 2),
            "uval": r(unit_value, 4),
            "poVal": r(po_qty * unit_value, 2) if unit_value is not None else None,
            # reorder
            "trig": trigger,
            "oq": r(order_qty, 1),
            "vmin": r(vendor_min, 1),
            "dt": double_tap,
            "st": status,
            "gapDays": gap_days,
            "dryDays": dry_days,
            "missUnits": r(miss_units, 1),
            "missVal": r(miss_val, 2),
        })

    warehouses = sorted({ln["w"] for ln in lines})
    classes = sorted({ln["cls"] for ln in lines if ln["cls"]})

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asOf": today.isoformat(),
        "window": {
            "start": today.isoformat(),
            "end": win_end.isoformat(),
            "days": SHORT_TERM_DAYS,
        },
        "season": {
            "label": "Fall",
            "start": season_start.isoformat(),
            "end": season_end.isoformat(),
            "active": season_start <= today <= season_end,
            "dayOf": (today - season_start).days + 1,
            "length": (season_end - season_start).days + 1,
        },
        "weekWindows": week_windows,
        "fcstMonths": {
            "current": {
                "label": date(cur_y, cur_m, 1).strftime("%B"),
                "start": month_bounds(cur_y, cur_m)[0].isoformat(),
                "end": month_bounds(cur_y, cur_m)[1].isoformat(),
            },
            "next": {
                "label": date(nxt_y, nxt_m, 1).strftime("%B"),
                "start": month_bounds(nxt_y, nxt_m)[0].isoformat(),
                "end": month_bounds(nxt_y, nxt_m)[1].isoformat(),
            },
        },
        "thresholds": {
            "varianceBand": VARIANCE_BAND,
            "minBandRate": MIN_BAND_RATE,
            "shortTermDays": SHORT_TERM_DAYS,
        },
        "keywords": {"core": CORE_KEYWORDS, "adjacent": ADJACENT_KEYWORDS},
        "warehouses": warehouses,
        "classes": classes,
        "lines": lines,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))

    counts = defaultdict(int)
    for ln in lines:
        counts[ln["st"]] += 1
        if ln["dt"]:
            counts["doubleTap"] += 1
        if ln["band"]:
            counts[ln["band"]] += 1
    print(
        f"Wrote {len(lines)} fall lines to {out_path} "
        f"({dict(counts)}, {len(warehouses)} markets, as_of {today}, "
        f"window {today}..{win_end})"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
