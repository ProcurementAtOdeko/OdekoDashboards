#!/usr/bin/env python3
"""Build data.json for the BOS1 new-to-market SKU dashboard.

Which SKUs reached the BOS1 market in the last NEW_WINDOW_DAYS days, and how
are they performing? "New" is exact: a SKU's launch date is its first-ever
fulfillment from BOS1, all time, so an older SKU that simply hadn't been
ordered for a while never counts.

That needs an all-time first date, which none of the network exports carry
("Min Order Date" is clamped to the export's own ~90-day window, and the PO
feed is a rolling window too). Two lean Looks on the fulfillment fact table
supply it, scheduled into the Looker Data Dumps folder:

  - "BOS1 SKU Customer Fulfillment" (Look 2405) — one row per SKU × customer
    ever fulfilled from BOS1: first/last fulfillment date, distinct orders,
    sales units, plus item category/brand/vendor/created. The earliest first
    date across a SKU's customers is its BOS1 launch date; the rows also give
    adoption (customers), repeat (customers with 2+ orders) and since-launch
    units. Customers active in the last 90 days are the penetration base.
  - "BOS1 SKU Weekly Fulfillment" (Look 2406) — SKU × fulfillment week over
    the trailing 13 weeks: units, orders, customers. Drives the trend.

Both Looks read customer_id straight off the fact table rather than joining
the customer view, which drags in NetSuite parent lookups and a per-row
haversine distance; that join is what makes the other BOS1 Look time out.

Performance is read four ways:
  - Velocity & trend — units/week since launch; last 3 complete weeks vs the
    3 before them.
  - Adoption & repeat — share of active BOS1 customers buying it, and the
    share of those who ordered it more than once.
  - Network benchmark — the same SKU at the other warehouses, from the
    per-warehouse files the network Sales Tracker commits to this repo
    (dashboards/sales-tracker/data/), so no extra exports are read.
  - Stock & fill — BOS1 days of cover, on hand / on order, past-due POs
    ("On Hand & ETA.csv") and out-of-stock events ("Trailing 14 FEOOS.csv").

FEOOS events accumulate across runs (that export only reaches back 14 days),
keyed on content with the stored count being the most times a key appeared
in any single snapshot, so re-reads never double-count. If a Looker export
is missing or empty on a run, the last good parse is carried forward and the
page says how old it is, rather than the run failing.

SKUs join across sources by normalized item name (lower-cased, leading
"(DUPLICATE) " stripped), the convention across these dashboards.
"""

from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

HERE = os.path.dirname(os.path.abspath(__file__))
SALES_TRACKER_DATA = os.path.join(HERE, "..", "..", "sales-tracker", "data")

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
PAIRS_FILE_NAME = "BOS1 SKU Customer Fulfillment.csv"   # Look 2405
WEEKLY_FILE_NAME = "BOS1 SKU Weekly Fulfillment.csv"    # Look 2406
PAIRS_LOOK_ID, WEEKLY_LOOK_ID = 2405, 2406
FEOOS_FILE_NAME = "Trailing 14 FEOOS.csv"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:N"

WAREHOUSE = "BOS1"
NEW_WINDOW_DAYS = 60       # launched within this many days counts as new
ACTIVE_CUSTOMER_DAYS = 90  # penetration base; matches the network tracker window
NEW_TO_ODEKO_DAYS = 180    # item record created this recently and sold nowhere else

# Verdict thresholds.
TOO_EARLY_DAYS = 21        # younger than this is too early to judge
STALLED_DAYS = 21          # no BOS1 fulfillment in this long → stalled
WIN_INDEX = 1.0            # penetration at or above the network median
SLOW_INDEX = 0.5           # under half the network median
WIN_REPEAT = 0.4           # 40%+ of buyers ordered it again
MIN_WIN_CUSTOMERS = 5      # reach needed before anything can be "Winning"
# One customer, at most this many orders: a special order rather than a
# launch. Kept apart so one-offs don't swamp the launch verdicts.
ONE_OFF_MAX_ORDERS = 2
# The carried-forward Looker cache keeps SKUs launched within the new window
# plus this margin, rather than every SKU BOS1 has ever shipped.
CACHE_MARGIN_DAYS = 30
TREND_BAND = 0.25          # ±25% week-on-week average counts as moving
DOC_CRITICAL = 7

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)
# Fee lines ride the fulfillment feed like items but aren't products.
_NON_PRODUCT = re.compile(r"\b(fee|surcharge)\b", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


def repair_keys(items, known, rename=False):
    """Looker's Sheets export sometimes destroys non-ASCII characters
    ("Reál" arrives as "Re\ufffd\ufffdl"), which breaks the by-name join to
    stock and the network benchmark. Treat each run of U+FFFD as a 1–2
    character wildcard and re-key the entry under the one known name it
    matches, if exactly one does. known: norm_name -> display name."""
    for key in [k for k in items if "\ufffd" in k]:
        pat = re.compile("^" + ".{1,2}".join(re.escape(x) for x in re.split("\ufffd+", key)) + "$")
        hits = [n for n in known if pat.match(n)]
        if len(hits) == 1 and hits[0] not in items:
            entry = items.pop(key)
            if rename:
                entry["name"] = known[hits[0]]
            items[hits[0]] = entry


def is_product(name):
    return bool(str(name).strip()) and not _NON_PRODUCT.search(str(name))


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def parse_date(s):
    s = str(s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.split(" ")[0], fmt).date()
        except ValueError:
            continue
    return None


def week_of(d):
    return d - timedelta(days=d.weekday())  # Monday, as Looker's DATE_TRUNC('WEEK')


def get_values(svc, sid, rng):
    return (svc.spreadsheets().values().get(spreadsheetId=sid, range=rng)
            .execute().get("values", []))


def find_file(drive, name, fuzzy=False):
    """Newest file with this exact name in the Looker folder. With fuzzy, fall
    back to one whose name contains the title without ".csv": a Looker
    schedule names its Sheet however it was set up, not necessarily title +
    ".csv". Only our own Look exports use it — for shared exports a contains
    match could pick up a sibling ("BH Trailing 14 FEOOS - Enterprise.csv")."""
    clauses = [f"name = '{name}'"]
    if fuzzy:
        clauses.append(f"name contains '{name.removesuffix('.csv')}'")
    for clause in clauses:
        q = f"'{LOOKER_FOLDER_ID}' in parents and {clause} and trashed = false"
        files = (drive.files().list(q=q, orderBy="modifiedTime desc", pageSize=5,
                                    fields="files(id,modifiedTime)")
                 .execute().get("files", []))
        if files:
            return files[0]
    return None


def cell(r, i):
    return r[i] if i is not None and i < len(r) else ""


def _hnorm(h):
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def locate(header, spec, label):
    """Map logical names to column indexes by header label.

    Looker writes headers with or without the view label depending on the
    schedule's formatting options ("Min Date" vs "Fulfillment Date Min
    Date"), so each column is matched on how its normalized label *ends*.
    spec: {key: [suffix, ...]}; a key whose first suffix is prefixed with
    '?' is optional.
    """
    hs = [_hnorm(h) for h in header]
    out = {}
    for key, suffixes in spec.items():
        optional = suffixes[0].startswith("?")
        sfx = [s.lstrip("?") for s in suffixes]
        idx = next((i for s in sfx for i, h in enumerate(hs)
                    if h == s or h.endswith(" " + s)), None)
        if idx is None and not optional:
            sys.exit(f"{label}: no column matching {sfx} in header {header}")
        out[key] = idx
    return out


# --- Looker exports -------------------------------------------------------

PAIRS_SPEC = {
    "item": ["item name"], "itemId": ["?item id"], "brand": ["?catalog brand", "brand"],
    "category": ["?category"], "subCategory": ["?sub category"], "vendor": ["?vendor name"],
    "created": ["?created"], "customer": ["customer id"],
    "first": ["min date"], "last": ["max date"],
    "orders": ["distinct order count", "unique orders"],
    "units": ["picked sales units", "sales units"],
}
WEEKLY_SPEC = {
    "item": ["item name"], "week": ["date week", "week"],
    "units": ["picked sales units", "sales units"],
}


def read_export(svc, drive, name, rng):
    f = find_file(drive, name, fuzzy=True)
    if not f:
        return None, None
    rows = get_values(svc, f["id"], rng)
    return (rows if len(rows) > 1 else None), f["modifiedTime"]


def parse_pairs(rows):
    """Item-level aggregates from the SKU × customer export."""
    col = locate(rows[0], PAIRS_SPEC, PAIRS_FILE_NAME)
    items = {}
    cust_last = {}
    through = None
    for r in rows[1:]:
        name = cell(r, col["item"]).strip()
        first, last = parse_date(cell(r, col["first"])), parse_date(cell(r, col["last"]))
        cust = cell(r, col["customer"]).strip()
        if not (name and first and last and cust):
            continue
        through = max(through or last, last)
        cust_last[cust] = max(cust_last.get(cust, last), last)
        if not is_product(name):
            continue
        key = norm_name(name)
        it = items.get(key)
        if it is None:
            it = items[key] = {
                "name": name, "itemId": cell(r, col["itemId"]),
                "brand": cell(r, col["brand"]), "category": cell(r, col["category"]),
                "subCategory": cell(r, col["subCategory"]), "vendor": cell(r, col["vendor"]),
                "created": (lambda d: d.isoformat() if d else "")(parse_date(cell(r, col["created"]))),
                "launch": first.isoformat(), "lastOrder": last.isoformat(),
                "units": 0.0, "orders": 0, "customers": 0, "repeatCustomers": 0,
                "adoption": {},   # week -> customers whose first order fell in it
            }
        it["launch"] = min(it["launch"], first.isoformat())
        it["lastOrder"] = max(it["lastOrder"], last.isoformat())
        orders = int(parse_num(cell(r, col["orders"])) or 0)
        it["units"] += parse_num(cell(r, col["units"])) or 0.0
        it["orders"] += orders
        it["customers"] += 1
        it["repeatCustomers"] += 1 if orders >= 2 else 0
        wk = week_of(first).isoformat()
        it["adoption"][wk] = it["adoption"].get(wk, 0) + 1
    if through is None:
        return None
    active = sum(1 for d in cust_last.values()
                 if (through - d).days < ACTIVE_CUSTOMER_DAYS)
    return {"through": through.isoformat(), "activeCustomers": active,
            "totalCustomers": len(cust_last), "items": items}


def parse_weekly(rows):
    """Weekly units per SKU, plus BOS1's weekly total across all SKUs.

    Every SKU is kept, not just today's new ones, so a carried-forward copy
    still serves whichever SKUs count as new on a later run.
    """
    col = locate(rows[0], WEEKLY_SPEC, WEEKLY_FILE_NAME)
    per = defaultdict(lambda: defaultdict(float))
    total = Counter()
    for r in rows[1:]:
        name, wk = cell(r, col["item"]), parse_date(cell(r, col["week"]))
        units = parse_num(cell(r, col["units"])) or 0.0
        if not (name and wk) or not is_product(name):
            continue
        w = week_of(wk).isoformat()
        total[w] += units
        per[norm_name(name)][w] += units
    return {"items": {k: {w: round(u, 2) for w, u in sorted(v.items())} for k, v in per.items()},
            "total": {w: round(u, 2) for w, u in sorted(total.items())}}


# --- Other inputs ---------------------------------------------------------

def load_stock(svc):
    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    if len(rows) <= 1:
        return {}
    col = {str(n).strip(): i for i, n in enumerate(rows[0])}
    need = ["Warehouse Name", "Procurement Vendor", "Item Name", "Consumption 60 Days",
            "Days of Cover 60 Days Eaches", "On Hand Purchase Units",
            "Quantity on Order Purchase Units", "Past Due Pos Tos"]
    missing = [c for c in need if c not in col]
    if missing:
        sys.exit(f"On Hand & ETA missing column(s): {', '.join(missing)}")
    stock = {}
    for r in rows[1:]:
        if cell(r, col["Warehouse Name"]) != WAREHOUSE:
            continue
        key = norm_name(cell(r, col["Item Name"]))
        # The sheet repeats each item once per stock line with identical
        # values, so the first non-empty value wins rather than summing.
        s = stock.setdefault(key, {"name": cell(r, col["Item Name"]).strip(), "vendor": "",
                                   "doc": None, "consumption60": None,
                                   "onHand": None, "onOrder": None, "pastDue": set()})
        if not s["vendor"]:
            s["vendor"] = cell(r, col["Procurement Vendor"]).strip()
        for src, dst in [("Days of Cover 60 Days Eaches", "doc"),
                         ("Consumption 60 Days", "consumption60"),
                         ("On Hand Purchase Units", "onHand"),
                         ("Quantity on Order Purchase Units", "onOrder")]:
            if s[dst] is None:
                s[dst] = parse_num(cell(r, col[src]))
        s["pastDue"].update(p for p in re.split(r"[,\s]+", cell(r, col["Past Due Pos Tos"])) if p)
    return stock


def merge_feoos(svc, drive, previous):
    prev = previous.get("feoosHistory") or {}
    hist = {tuple(e["k"]): e["n"] for e in prev.get("events", [])}
    since = prev.get("trackingSince")
    f = find_file(drive, FEOOS_FILE_NAME)
    rows = get_values(svc, f["id"], "A1:J") if f else []
    if len(rows) > 1:
        col = {str(n).strip(): i for i, n in enumerate(rows[0])}
        snap = Counter()
        for r in rows[1:]:
            if cell(r, col.get("Warehouse")) != WAREHOUSE:
                continue
            ev = cell(r, col.get("FEOOS Event Date"))
            if ev:
                since = min(since or ev, ev)
            snap[(ev, cell(r, col.get("Item Name")), cell(r, col.get("Location Name")),
                  cell(r, col.get("Quantity of FEOOS Items Requested in Sales Units")))] += 1
        for k, n in snap.items():
            hist[k] = max(hist.get(k, 0), n)
    return {"trackingSince": since,
            "events": [{"k": list(k), "n": n} for k, n in sorted(hist.items())]}


def load_network_benchmark():
    """Per-SKU penetration and per-buyer velocity at every other warehouse,
    from the network Sales Tracker's committed per-warehouse files."""
    try:
        with open(os.path.join(SALES_TRACKER_DATA, "manifest.json")) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return {}, [], {}
    per_item = defaultdict(list)
    used = []
    names = {}  # norm_name -> display name, for repairing Looker's names
    for w in manifest.get("warehouses", []):
        code = w.get("code")
        if code == WAREHOUSE or w.get("status") != "ok":
            continue
        try:
            with open(os.path.join(SALES_TRACKER_DATA, f"{code}.json")) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        base = (d.get("summary") or {}).get("customerCount") or 0
        dr = d.get("dateRange") or {}
        start, end = parse_date(dr.get("start")), parse_date(dr.get("end"))
        if not (base and start and end):
            continue
        weeks = ((end - start).days + 1) / 7.0
        used.append(code)
        for it in d.get("items", []):
            c = it.get("customers") or 0
            if not c:
                continue
            names.setdefault(norm_name(it.get("name", "")), it.get("name", ""))
            per_item[norm_name(it.get("name", ""))].append({
                "wh": code, "penetration": c / base,
                "perBuyerWeekly": (it.get("units") or 0) / c / weeks,
            })
    return per_item, used, names


# --- Build ----------------------------------------------------------------

def trend_of(weekly, launch, through):
    """Last 3 complete weeks since launch vs the up-to-3 before them.

    Only full weeks count: a launch mid-week would otherwise make every
    SKU's first week look small and bias the read toward "ramping".
    """
    cur_week = week_of(through)
    complete_end = cur_week if through.weekday() < 6 else cur_week + timedelta(days=7)
    weeks = []
    w = week_of(launch) if launch.weekday() == 0 else week_of(launch) + timedelta(days=7)
    while w < complete_end:
        weeks.append(w.isoformat())
        w += timedelta(days=7)
    series = [weekly.get(x, 0.0) for x in weeks]
    if len(series) < 4:
        return None, "new", series
    recent = series[-3:]
    prior = series[-6:-3] if len(series) >= 6 else series[:-3]
    a, b = sum(recent) / len(recent), sum(prior) / len(prior)
    if b == 0:
        return None, ("ramping" if a > 0 else "flat"), series
    ch = a / b - 1
    return round(ch, 3), ("ramping" if ch > TREND_BAND else "fading" if ch < -TREND_BAND else "steady"), series


def _cache_cutoff(pairs):
    through = parse_date(pairs["through"])
    return (through - timedelta(days=NEW_WINDOW_DAYS + CACHE_MARGIN_DAYS)).isoformat()


def trim_pairs(pairs):
    """Only SKUs near the new window are worth carrying forward: a carried
    copy is re-read against its own `through` date, so older SKUs can't
    become new from it."""
    cut = _cache_cutoff(pairs)
    return {**pairs, "items": {k: v for k, v in pairs["items"].items() if v["launch"] >= cut}}


def trim_weekly(weekly, pairs):
    if not pairs:
        return weekly
    cut = _cache_cutoff(pairs)
    keep = {k for k, v in pairs["items"].items() if v["launch"] >= cut}
    return {**weekly, "items": {k: v for k, v in weekly["items"].items() if k in keep}}


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        with open(out_path) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = {}
    cache = previous.get("lookerCache") or {}

    # Stock and the benchmark come first: their names repair the Looker ones.
    stock = load_stock(svc)
    bench, bench_whs, bench_names = load_network_benchmark()
    known = {**{k: v["name"] for k, v in stock.items() if v.get("name")}, **bench_names}

    # Looker exports, carrying the last good parse forward when one is empty.
    rows, mod = read_export(svc, drive, PAIRS_FILE_NAME, "A1:P")
    pairs = parse_pairs(rows) if rows else None
    if pairs:
        pairs_status, pairs["exportModified"] = "fresh", mod
    elif cache.get("pairs"):
        pairs, pairs_status = cache["pairs"], "carried"
    else:
        pairs, pairs_status = None, "missing"

    if pairs:
        repair_keys(pairs["items"], known, rename=True)

    new_items = {}
    if pairs:
        through = parse_date(pairs["through"])
        cutoff = through - timedelta(days=NEW_WINDOW_DAYS - 1)
        new_items = {k: v for k, v in pairs["items"].items()
                     if parse_date(v["launch"]) >= cutoff}

    rows, mod = read_export(svc, drive, WEEKLY_FILE_NAME, "A1:H")
    weekly = parse_weekly(rows) if rows else None
    if weekly:
        weekly_status, weekly["exportModified"] = "fresh", mod
    elif cache.get("weekly"):
        weekly, weekly_status = cache["weekly"], "carried"
    else:
        weekly, weekly_status = {"items": {}, "total": {}}, "missing"

    repair_keys(weekly["items"], known)

    feoos = merge_feoos(svc, drive, previous)
    feoos_by_item = Counter()
    for e in feoos["events"]:
        feoos_by_item[norm_name(e["k"][1])] += e["n"]

    skus = []
    if pairs:
        through = parse_date(pairs["through"])
        active = pairs["activeCustomers"] or 1
        for key, it in new_items.items():
            launch = parse_date(it["launch"])
            days_live = (through - launch).days + 1
            weeks_live = days_live / 7.0
            wk = weekly["items"].get(key, {})
            trend_pct, trend_dir, series = trend_of(wk, launch, through)
            pen = it["customers"] / active
            repeat = it["repeatCustomers"] / it["customers"] if it["customers"] else None
            per_buyer = it["units"] / it["customers"] / weeks_live if it["customers"] else 0.0

            b = bench.get(key, [])
            net_pen = statistics.median(x["penetration"] for x in b) if b else None
            net_vel = statistics.median(x["perBuyerWeekly"] for x in b) if b else None
            index = (pen / net_pen) if net_pen else None
            created = parse_date(it.get("created"))
            new_to_odeko = not b and bool(created and (through - created).days <= NEW_TO_ODEKO_DAYS)

            st = stock.get(key) or {}
            doc = st.get("doc")
            if doc == 0 and not st.get("consumption60"):
                doc = None  # no demand signal, not "zero days of cover"
            events = feoos_by_item.get(key, 0)
            supply_risk = bool(events) or (doc is not None and doc < DOC_CRITICAL and not st.get("onOrder"))
            days_since = (through - parse_date(it["lastOrder"])).days

            if days_live < TOO_EARLY_DAYS:
                verdict = "Too early"
            elif it["customers"] <= 1 and it["orders"] <= ONE_OFF_MAX_ORDERS:
                verdict = "One-off"
            elif days_since > STALLED_DAYS:
                verdict = "Stalled"
            elif index is not None:
                if index >= WIN_INDEX and (repeat or 0) >= WIN_REPEAT \
                        and it["customers"] >= MIN_WIN_CUSTOMERS:
                    verdict = "Winning"
                elif index < SLOW_INDEX and (repeat or 0) < WIN_REPEAT:
                    verdict = "Slow"
                else:
                    verdict = "Building"
            else:
                # Nothing to benchmark against (new to Odeko, or not sold
                # elsewhere): judge on stickiness and reach alone.
                if (repeat or 0) >= WIN_REPEAT and it["customers"] >= MIN_WIN_CUSTOMERS:
                    verdict = "Winning"
                elif it["customers"] <= 2 and (repeat or 0) < WIN_REPEAT:
                    verdict = "Slow"
                else:
                    verdict = "Building"

            skus.append({
                "key": key, "name": it["name"], "itemId": it["itemId"],
                "brand": it["brand"] or "", "category": it["category"] or "",
                "subCategory": it["subCategory"] or "",
                # On Hand & ETA's procurement vendor only: Looker's
                # dim_item.vendor_name is the vendor's name *for the item*
                # ("Mozzarella House Burratina 8oz"), not the vendor.
                "vendor": st.get("vendor") or "",
                "launch": it["launch"], "daysLive": days_live, "lastOrder": it["lastOrder"],
                "daysSinceLastOrder": days_since,
                "newToOdeko": new_to_odeko,
                "units": round(it["units"], 1), "orders": it["orders"],
                "unitsPerWeek": round(it["units"] / weeks_live, 1),
                "weekly": [round(x, 1) for x in series],
                "byWeek": weekly["items"].get(key, {}),
                "trend": trend_dir, "trendPct": trend_pct,
                "customers": it["customers"], "repeatCustomers": it["repeatCustomers"],
                "repeatRate": round(repeat, 3) if repeat is not None else None,
                "penetration": round(pen, 4), "perBuyerWeekly": round(per_buyer, 2),
                "adoptionByWeek": dict(sorted(it["adoption"].items())),
                "network": {"warehouses": len(b),
                            "penetration": round(net_pen, 4) if net_pen is not None else None,
                            "perBuyerWeekly": round(net_vel, 2) if net_vel is not None else None,
                            "index": round(index, 2) if index is not None else None},
                "doc": round(doc, 1) if doc is not None else None,
                "onHand": st.get("onHand"), "onOrder": st.get("onOrder"),
                "pastDuePos": sorted(st.get("pastDue") or []),
                "feoosEvents": events, "supplyRisk": supply_risk,
                "verdict": verdict,
            })
    skus.sort(key=lambda s: (-s["unitsPerWeek"], s["name"]))

    # BOS1's total weekly units, the denominator for new SKUs' share of
    # volume (the page sums the new SKUs' own byWeek under its filters).
    weekly_out = [{"week": w, "total": round(u, 1)} for w, u in weekly["total"].items()]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouse": WAREHOUSE,
        "thresholds": {"newWindowDays": NEW_WINDOW_DAYS, "tooEarlyDays": TOO_EARLY_DAYS,
                       "stalledDays": STALLED_DAYS, "winIndex": WIN_INDEX,
                       "slowIndex": SLOW_INDEX, "winRepeat": WIN_REPEAT,
                       "trendBand": TREND_BAND, "docCritical": DOC_CRITICAL,
                       "minWinCustomers": MIN_WIN_CUSTOMERS, "oneOffMaxOrders": ONE_OFF_MAX_ORDERS,
                       "activeCustomerDays": ACTIVE_CUSTOMER_DAYS},
        "sources": {
            "pairs": {"status": pairs_status, "file": PAIRS_FILE_NAME, "look": PAIRS_LOOK_ID,
                      "through": pairs and pairs["through"],
                      "exportModified": pairs and pairs.get("exportModified")},
            "weekly": {"status": weekly_status, "file": WEEKLY_FILE_NAME, "look": WEEKLY_LOOK_ID,
                       "exportModified": weekly.get("exportModified")},
            "feoos": {"trackingSince": feoos["trackingSince"]},
            "benchmark": {"warehouses": bench_whs},
        },
        "bos1": {"activeCustomers": pairs and pairs["activeCustomers"],
                 "skusEverFulfilled": len(pairs["items"]) if pairs else None},
        "skus": skus,
        "weekly": weekly_out,
        # Persisted between runs.
        "lookerCache": {"pairs": trim_pairs(pairs) if pairs_status == "fresh" else cache.get("pairs"),
                        "weekly": trim_weekly(weekly, pairs) if weekly_status == "fresh" else cache.get("weekly")},
        "feoosHistory": feoos,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)

    print(f"Looker exports: pairs {pairs_status}, weekly {weekly_status} | "
          f"new SKUs: {len(skus)} | verdicts: {dict(Counter(s['verdict'] for s in skus))} | "
          f"benchmark warehouses: {len(bench_whs)}")
    if pairs_status == "missing":
        print(f"WARNING: '{PAIRS_FILE_NAME}' isn't in the Looker Data Dumps folder yet — "
              f"schedule Look {PAIRS_LOOK_ID} there; the page shows a waiting state until then.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else os.path.join(HERE, "..", "data.json"))
