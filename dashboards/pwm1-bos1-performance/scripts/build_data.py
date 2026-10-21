#!/usr/bin/env python3
"""Build data.json for the PWM1 → BOS1 transfer performance dashboard.

The 22 Blank Street and Van Leeuwen Boston-area stores moved from PWM1 to
BOS1 at the start of October. This tracks how that move is performing, for a
leadership readout, across four views:

  1. Store run-rate     — weekly units each store orders from BOS1 since
                          cutover, against its pre-cutover PWM1 baseline.
  2. Gap SKU onboarding — the 43 SKUs BOS1 didn't carry when the transfer was
                          scoped, tracked Not started → On PO/TO → Received →
                          Live at BOS1.
  3. Fill & stock       — out-of-stock (FEOOS) events at BOS1 for these stores
                          and on the SKUs they buy, plus BOS1 days of cover.
  4. Basket shift       — SKUs they bought at PWM1 but haven't reordered at
                          BOS1 (lost lines), and SKUs new to them at BOS1.

Frozen inputs (scripts/, committed — these must not move with the feeds):
  - baseline.json  PWM1 orders for the roster over the BASELINE_WEEKS before
                   CUTOVER. The PWM1 export is a rolling ~90-day window, so
                   this history ages out of the feed by late December; it is
                   snapshotted once with `--freeze-baseline` and read from
                   here thereafter.
  - gap_skus.csv   The 43 bring-in gaps from the 2026-09-14 conversion
                   analysis, i.e. the list the onboarding view is judged on.
  The roster is read from the sibling pwm1-bos1-conversion dashboard so the
  two can't drift apart.

Accumulated history (carried in data.json, merged forward each run):
  - BOS1 order lines. "BOS1 Sales Tracker.csv" is the only source of BOS1
    orders by customer, and Looker leaves it empty for much of the day (it was
    populated on about half the days sampled in October). When it is empty
    the last captured lines are kept and the page says how old they are, so
    the rest of the dashboard still refreshes instead of the run failing.
    When it is populated, its dates replace the stored ones and anything
    older is kept, so history survives the export's own rolling window.
  - FEOOS events. "Trailing 14 FEOOS.csv" only reaches back 14 days, so
    events are accumulated to count everything since tracking began. Rows
    carry no stable id; an event is keyed on its contents and the stored
    count is the most times that key appeared in any single snapshot, so a
    re-read never double-counts and genuine same-day repeats still count.

Live inputs (Looker Data Dumps folder / Combined models dump):
  - "BOS1 Sales Tracker.csv"            BOS1 orders by customer
  - "Network Sales Tracker - PWM1.csv"  post-cutover leakage back to PWM1
  - "Trailing 14 FEOOS.csv"             network FEOOS with Location Name
  - "On Hand & ETA.csv"                 BOS1 days of cover, on hand, on order
  - "PO Data for Automating.csv"        BOS1 PO/TO lines for onboarding stage
  - Combined Models Dump "Warehouse Raw" BOS1 in_catalog flag

SKUs join across sources by normalized item name (lower-cased, leading
"(DUPLICATE) " stripped), as across the other conversion dashboards. Actual
sold units = SO Item Qty / Conversion Rate, the network tracker convention.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

HERE = os.path.dirname(os.path.abspath(__file__))
ROSTER_CSV = os.path.join(HERE, "..", "..", "pwm1-bos1-conversion", "scripts", "roster.csv")
BASELINE_JSON = os.path.join(HERE, "baseline.json")
GAP_SKUS_CSV = os.path.join(HERE, "gap_skus.csv")

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
PWM1_FILE_NAME = "Network Sales Tracker - PWM1.csv"
BOS1_FILE_NAME = "BOS1 Sales Tracker.csv"
FEOOS_FILE_NAME = "Trailing 14 FEOOS.csv"
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:N"
PO_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
PO_RANGE = "'PO Data for Automating.csv'!A1:N"

ORIGIN, DEST = "PWM1", "BOS1"
# First day the roster was served from BOS1. Every Blank Street store placed
# its last PWM1 order on Sep 30 or Oct 1; Van Leeuwen had tapered off through
# late September.
CUTOVER = date(2026, 10, 2)
BASELINE_WEEKS = 8

# Store run-rate bands, as BOS1 weekly units / PWM1 baseline weekly units.
ON_TRACK_RATIO = 0.8
WELL_BELOW_RATIO = 0.5
QUIET_DAYS = 14            # no BOS1 order in this long → "gone quiet"
LOW_VOLUME_WEEKLY = 5.0    # baselines this small make the ratio noisy
# A SKU only counts as a lost line once the store would normally have
# reordered it this many times since cutover; before that it's just early.
LOST_LINE_EXPECTED_ORDERS = 2.0
# A BOS1 read with fewer than this share of the order lines we already hold
# for the same dates is a partially written export, not real data — the
# network sales tracker uses the same ratio.
PARTIAL_READ_RATIO = 0.8
# Days-of-cover bands for SKUs these stores buy.
DOC_CRITICAL = 7
DOC_LOW = 14

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)
# Order lines that aren't products — "Delivery Fee", "Fuel Surcharge". They
# ride the sales feeds like any item, but can't be stocked or brought in, so
# they're dropped everywhere (the Sep 14 gap list included "Delivery Fee").
# Whole words only: "Coffee" must not match.
_NON_PRODUCT = re.compile(r"\b(fee|surcharge)\b", re.I)


def is_product(name):
    return not _NON_PRODUCT.search(str(name))


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


def norm_loc(name):
    """Store names appear as 'Blank Street - 97 Charles Street' (roster,
    FEOOS) and 'Blank Street : 97 Charles Street' (sales feeds)."""
    s = str(name).lower().replace(" : ", " - ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_date(s):
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return None


def week_of(d):
    return d - timedelta(days=d.weekday())  # Monday


def get_values(svc, sid, rng):
    return (svc.spreadsheets().values().get(spreadsheetId=sid, range=rng)
            .execute().get("values", []))


def find_file(drive, name):
    q = (f"'{LOOKER_FOLDER_ID}' in parents and name = '{name}' "
         "and trashed = false")
    files = (drive.files().list(q=q, orderBy="modifiedTime desc", pageSize=5,
                                fields="files(id,modifiedTime)")
             .execute().get("files", []))
    return files[0] if files else None


def header_index(rows, required, label):
    if not rows:
        return None
    col = {str(n).strip(): i for i, n in enumerate(rows[0])}
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"{label} missing expected column(s): {', '.join(missing)}")
    return col


def cell(r, i):
    return r[i] if i is not None and i < len(r) else ""


def feed_dates(rows):
    """Every order date in a network-tracker export, located by header."""
    col = {str(n).strip(): i for i, n in enumerate(rows[0])} if rows else {}
    i = col.get("Date Date")
    return [d for d in (parse_date(cell(r, i)) for r in rows[1:]) if d]


# --- Inputs ---------------------------------------------------------------

def load_roster():
    roster = []
    with open(ROSTER_CSV, newline="") as f:
        for row in csv.DictReader(f):
            uuid = (row.get("UUID") or "").strip()
            if uuid:
                roster.append({
                    "uuid": uuid,
                    "name": (row.get("Account Name") or "").strip(),
                    "group": (row.get("Group") or "").strip(),
                    "location": (row.get("Location") or "").strip(),
                    "zip": (row.get("Zip") or "").strip(),
                })
    if not roster:
        sys.exit(f"Roster {ROSTER_CSV} is empty")
    return roster


def load_gap_skus():
    with open(GAP_SKUS_CSV, newline="") as f:
        return [{"key": norm_name(r["Item Name"]), "name": r["Item Name"],
                 "brand": r.get("Brand") or "",
                 "units": float(r.get("PWM1 Units") or 0),
                 "stores": int(r.get("Stores") or 0),
                 "chains": [c.strip() for c in (r.get("Chains") or "").split(";") if c.strip()]}
                for r in csv.DictReader(f)
                if r.get("Item Name") and is_product(r["Item Name"])]


def sales_lines(rows, wanted, label):
    """Order lines for the wanted store UUIDs from a network-tracker export:
    yields (uuid, key, name, brand, customer_name, date, units)."""
    col = header_index(rows, ["Item Name", "Odeko Account Uuid", "Date Date",
                              "SO Item Qty", "Conversion Rate"], label)
    if col is None:
        return
    ci, cb, cc = col["Item Name"], col.get("Brand Name"), col.get("Customer Name")
    cu, cd = col["Odeko Account Uuid"], col["Date Date"]
    cq, cv = col["SO Item Qty"], col["Conversion Rate"]
    for r in rows[1:]:
        uuid = cell(r, cu)
        if uuid not in wanted:
            continue
        name, d = cell(r, ci), parse_date(cell(r, cd))
        if not name or d is None or not is_product(name):
            continue
        qty, conv = parse_num(cell(r, cq)), parse_num(cell(r, cv))
        units = qty / conv if (qty is not None and conv and conv > 0) else 0.0
        yield (uuid, norm_name(name), name, cell(r, cb), cell(r, cc), d, units)


def freeze_baseline(svc, drive):
    """Snapshot the roster's pre-cutover PWM1 orders into baseline.json."""
    roster = load_roster()
    wanted = {r["uuid"] for r in roster}
    f = find_file(drive, PWM1_FILE_NAME)
    if not f:
        sys.exit(f"Could not find '{PWM1_FILE_NAME}'")
    rows = get_values(svc, f["id"], "A1:N")
    start = CUTOVER - timedelta(weeks=BASELINE_WEEKS)
    feed_min = min(feed_dates(rows), default=None)
    if feed_min is None or feed_min > start:
        sys.exit(f"PWM1 feed starts {feed_min}, after the baseline start {start} "
                 "— can't freeze a complete baseline from it.")
    agg = defaultdict(lambda: {"units": 0.0, "lines": 0, "last": ""})
    skus, names = {}, {}
    for uuid, key, name, brand, cust, d, units in sales_lines(rows, wanted, "PWM1 sheet"):
        if not (start <= d < CUTOVER):
            continue
        a = agg[(uuid, key, week_of(d).isoformat())]
        a["units"] += units
        a["lines"] += 1
        skus.setdefault(key, {"name": name, "brand": brand or ""})
        if cust:
            names.setdefault(uuid, cust)
    out = {
        "frozenAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": PWM1_FILE_NAME,
        "start": start.isoformat(),
        "end": (CUTOVER - timedelta(days=1)).isoformat(),
        "weeks": BASELINE_WEEKS,
        "customerNames": names,
        "skus": skus,
        # [store uuid, sku key, week (Monday), units, order lines]
        "lines": sorted([u, k, w, round(v["units"], 3), v["lines"]]
                        for (u, k, w), v in agg.items()),
    }
    with open(BASELINE_JSON, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"Froze {len(out['lines'])} baseline rows ({start} → {out['end']}) "
          f"for {len({l[0] for l in out['lines']})} stores to {BASELINE_JSON}")


def load_previous(out_path):
    try:
        with open(out_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def merge_bos1(svc, drive, roster, previous):
    """BOS1 post-cutover order lines for the roster, merged with history.

    Returns (history, status) where history is the dict persisted in
    data.json and status says whether this run saw a populated export.
    """
    hist = previous.get("bos1History") or {
        "lines": [], "skus": {}, "customerNames": {},
        "capturedAt": None, "dataThrough": None,
    }
    wanted = {r["uuid"] for r in roster}
    f = find_file(drive, BOS1_FILE_NAME)
    rows = get_values(svc, f["id"], "A1:N") if f else []
    if len(rows) <= 1:
        return hist, "carried" if hist["lines"] else "missing"

    agg = defaultdict(lambda: [0.0, 0])
    fresh_dates = set()
    for uuid, key, name, brand, cust, d, units in sales_lines(rows, wanted, "BOS1 sheet"):
        fresh_dates.add(d)
        if d < CUTOVER:
            continue
        a = agg[(uuid, key, d.isoformat())]
        a[0] += units
        a[1] += 1
        hist["skus"].setdefault(key, {"name": name, "brand": brand or ""})
        if cust:
            hist["customerNames"][uuid] = cust
    all_dates = feed_dates(rows)
    if not all_dates:
        return hist, "carried" if hist["lines"] else "missing"
    feed_min, feed_max = min(all_dates), max(all_dates)
    new = [[u, k, d, round(v[0], 3), v[1]] for (u, k, d), v in agg.items()]
    # Caught mid-write, the export holds only part of its rows. Compare against
    # what we already hold for the dates both cover; keep history if it's short.
    overlap_end = hist["dataThrough"] or ""
    stored = sum(l[4] for l in hist["lines"] if feed_min.isoformat() <= l[2] <= overlap_end)
    fresh = sum(l[4] for l in new if l[2] <= overlap_end)
    if stored and fresh < PARTIAL_READ_RATIO * stored:
        print(f"BOS1 export looks partially written ({fresh} order lines vs {stored} "
              "held for the same dates) — keeping stored history.")
        return hist, "carried"
    # The export's own window replaces what we stored for those dates; older
    # stored dates are kept so history outlives the export's rolling window.
    kept = [l for l in hist["lines"] if parse_date(l[2]) < feed_min]
    hist["lines"] = sorted(kept + new)
    hist["capturedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hist["dataThrough"] = feed_max.isoformat()
    hist["feedRows"] = len(rows) - 1
    return hist, "fresh"


def pwm1_leakage(svc, drive, roster):
    """Roster orders still landing at PWM1 after cutover."""
    wanted = {r["uuid"] for r in roster}
    f = find_file(drive, PWM1_FILE_NAME)
    rows = get_values(svc, f["id"], "A1:N") if f else []
    if len(rows) <= 1:
        return None
    leak = defaultdict(lambda: {"units": 0.0, "lines": 0, "last": ""})
    for uuid, key, name, brand, cust, d, units in sales_lines(rows, wanted, "PWM1 sheet"):
        if d >= CUTOVER:
            l = leak[uuid]
            l["units"] += units
            l["lines"] += 1
            l["last"] = max(l["last"], d.isoformat())
    return leak


def merge_feoos(svc, drive, previous, roster_locs, cohort_keys):
    """Accumulated BOS1 FEOOS events touching the roster or its SKUs."""
    hist = {tuple(e["k"]): e["n"] for e in (previous.get("feoosHistory") or {}).get("events", [])}
    first = (previous.get("feoosHistory") or {}).get("trackingSince")
    f = find_file(drive, FEOOS_FILE_NAME)
    rows = get_values(svc, f["id"], "A1:J") if f else []
    market = None
    if len(rows) > 1:
        col = header_index(rows, ["FEOOS Event Date", "Delivery Date", "Item Name",
                                  "Warehouse", "Location Name",
                                  "Quantity of FEOOS Items Requested in Sales Units"],
                           "FEOOS sheet")
        snap = Counter()
        market = {"events": 0, "locations": set(), "from": None, "to": None}
        for r in rows[1:]:
            if cell(r, col["Warehouse"]) != DEST:
                continue
            ev = cell(r, col["FEOOS Event Date"])
            market["events"] += 1
            market["locations"].add(cell(r, col["Location Name"]))
            if ev:
                market["from"] = min(market["from"] or ev, ev)
                market["to"] = max(market["to"] or ev, ev)
            loc = cell(r, col["Location Name"])
            key = norm_name(cell(r, col["Item Name"]))
            if norm_loc(loc) not in roster_locs and key not in cohort_keys:
                continue
            snap[(ev, cell(r, col["Delivery Date"]), cell(r, col["Item Name"]),
                  cell(r, col["Brand Name"]) if "Brand Name" in col else "",
                  loc, cell(r, col["Quantity of FEOOS Items Requested in Sales Units"]))] += 1
        for k, n in snap.items():
            hist[k] = max(hist.get(k, 0), n)
        if market["from"]:
            first = min(first or market["from"], market["from"])
        market["locations"] = len(market["locations"])
    return {
        "trackingSince": first,
        "events": [{"k": list(k), "n": n} for k, n in sorted(hist.items())],
    }, market


def load_carried(svc):
    """BOS1 catalog flag and on-hand rows: norm_name -> {sources}, plus stock."""
    carried = defaultdict(set)
    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    col = header_index(rows, ["warehouse_name", "item_name", "in_catalog"], "Warehouse Raw")
    for r in rows[1:]:
        if cell(r, col["warehouse_name"]) == DEST and \
                str(cell(r, col["in_catalog"])).strip().upper() in ("TRUE", "1", "YES", "Y"):
            carried[norm_name(cell(r, col["item_name"]))].add("catalog")

    stock = {}
    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    col = header_index(rows, ["Warehouse Name", "Procurement Vendor", "Item Name",
                              "Consumption 60 Days", "Days of Cover 60 Days Eaches",
                              "On Hand Purchase Units", "Quantity on Order Purchase Units",
                              "Past Due Pos Tos"], "On Hand & ETA")
    for r in rows[1:]:
        if cell(r, col["Warehouse Name"]) != DEST:
            continue
        key = norm_name(cell(r, col["Item Name"]))
        if not key:
            continue
        carried[key].add("onhand")
        # The sheet repeats each item once per stock line with the same values,
        # so the first non-empty value wins rather than summing.
        s = stock.setdefault(key, {"vendor": "", "doc": None, "consumption60": None,
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
    return carried, stock


# Intracompany transfers ride the PO feed with another Odeko warehouse as the
# "vendor" — same convention as the MDT1 onboarding tracker.
_TRANSFER_VENDOR = re.compile(r"\bwarehouse\b|\bintracompany\b", re.I)
CLOSED_STATUSES = {"closed", "fully billed"}


def load_pos(svc, wanted):
    rows = get_values(svc, PO_SPREADSHEET_ID, PO_RANGE)
    col = header_index(rows, ["Item Name", "Warehouse Name", "Purchase Order Number",
                              "Full Vendor Name", "Purchase Order Status",
                              "Purchase Order Units", "Quantity Received",
                              "Shipment Received Utc Date", "Expected Receipt Date Date"],
                       "PO sheet")
    pos = defaultdict(list)
    for r in rows[1:]:
        key = norm_name(cell(r, col["Item Name"]))
        if cell(r, col["Warehouse Name"]) != DEST or key not in wanted:
            continue
        vendor = cell(r, col["Full Vendor Name"])
        status = cell(r, col["Purchase Order Status"])
        pos[key].append({
            "po": cell(r, col["Purchase Order Number"]),
            "vendor": re.sub(r"^VEN\d+\s+", "", vendor),
            "status": status,
            "type": "TO" if _TRANSFER_VENDOR.search(vendor) else "PO",
            "open": status.strip().lower() not in CLOSED_STATUSES,
            "expected": cell(r, col["Expected Receipt Date Date"]),
            "received": (parse_num(cell(r, col["Quantity Received"])) or 0) > 0
                        or bool(cell(r, col["Shipment Received Utc Date"])),
        })
    return pos


# --- Build ----------------------------------------------------------------

def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    if "--freeze-baseline" in sys.argv:
        freeze_baseline(svc, drive)
        return

    roster = load_roster()
    by_uuid = {r["uuid"]: r for r in roster}
    with open(BASELINE_JSON) as f:
        baseline = json.load(f)
    gap_skus = load_gap_skus()
    previous = load_previous(out_path)
    groups = sorted({r["group"] for r in roster})

    bos1, bos1_status = merge_bos1(svc, drive, roster, previous)
    leak = pwm1_leakage(svc, drive, roster)
    carried, stock = load_carried(svc)

    # SKU metadata: baseline first, BOS1 names win where both exist.
    meta = {**baseline["skus"], **bos1["skus"]}
    for g in gap_skus:
        meta.setdefault(g["key"], {"name": g["name"], "brand": g["brand"]})

    # Per-store, per-SKU totals for both periods.
    base_ss = defaultdict(lambda: [0.0, 0])   # (uuid, key) -> [units, lines]
    base_week = defaultdict(float)             # (group, week) -> units
    for uuid, key, wk, units, lines in baseline["lines"]:
        if not is_product(baseline["skus"].get(key, {}).get("name", key)):
            continue
        base_ss[(uuid, key)][0] += units
        base_ss[(uuid, key)][1] += lines
        base_week[(by_uuid[uuid]["group"], wk)] += units
    post_ss = defaultdict(lambda: [0.0, 0, ""])  # (uuid, key) -> [units, lines, last date]
    post_week = defaultdict(float)
    store_dates = defaultdict(list)
    for uuid, key, d, units, lines in bos1["lines"]:
        p = post_ss[(uuid, key)]
        p[0] += units
        p[1] += lines
        p[2] = max(p[2], d)
        post_week[(by_uuid[uuid]["group"], week_of(parse_date(d)).isoformat())] += units
        store_dates[uuid].append(d)

    data_through = parse_date(bos1["dataThrough"]) if bos1["dataThrough"] else None
    post_days = max((data_through - CUTOVER).days + 1, 0) if data_through else 0
    post_weeks = max(post_days / 7.0, 0.0)
    base_weeks = float(baseline["weeks"])
    bos1_buys = {k for (u, k) in post_ss}

    # Live at BOS1 = in its catalog, on its on-hand report, or already being
    # ordered from it by these stores. Deliberately not the BOS1 sales export's
    # whole-market sold list: that export is empty much of the day, and a gap
    # shouldn't flicker open and shut with it.
    def bos1_sources(key):
        s = set(carried.get(key, ()))
        if key in bos1_buys:
            s.add("roster orders")
        return sorted(s)

    # --- Stores
    stores = []
    for r in roster:
        u = r["uuid"]
        b_units = sum(v[0] for (uu, _), v in base_ss.items() if uu == u)
        b_skus = {k for (uu, k) in base_ss if uu == u}
        p_units = sum(v[0] for (uu, _), v in post_ss.items() if uu == u)
        p_skus = {k for (uu, k) in post_ss if uu == u}
        b_wk = b_units / base_weeks
        p_wk = p_units / post_weeks if post_weeks else 0.0
        ratio = (p_wk / b_wk) if b_wk else None
        dates = sorted(store_dates.get(u, []))
        last = dates[-1] if dates else None
        quiet = bool(last and data_through and (data_through - parse_date(last)).days > QUIET_DAYS)

        lost = []
        for k in b_skus - p_skus:
            units, lines = base_ss[(u, k)]
            expected = lines / base_weeks * post_weeks
            if expected >= LOST_LINE_EXPECTED_ORDERS:
                lost.append({"key": k, "baselineUnits": round(units, 1),
                             "expectedOrders": round(expected, 1)})
        new = [k for k in p_skus - b_skus]

        if bos1_status == "missing":
            # Never captured a BOS1 order: unknown, which is not the same as zero.
            status = "Awaiting BOS1 data" if b_units else "No PWM1 baseline"
        elif not b_units and not p_units:
            status = "No activity"
        elif not b_units:
            status = "No PWM1 baseline"
        elif not p_units:
            status = "Not ordering at BOS1"
        elif quiet:
            status = "Gone quiet"
        elif ratio >= ON_TRACK_RATIO:
            status = "On track"
        elif ratio >= WELL_BELOW_RATIO:
            status = "Below baseline"
        else:
            status = "Well below baseline"
        lk = (leak or {}).get(u)
        stores.append({
            "uuid": u,
            "name": bos1["customerNames"].get(u) or baseline["customerNames"].get(u) or r["name"],
            "group": r["group"], "location": r["location"], "zip": r["zip"],
            "baselineUnits": round(b_units, 1), "baselineWeekly": round(b_wk, 1),
            "baselineSkus": len(b_skus),
            "bos1Units": round(p_units, 1), "bos1Weekly": round(p_wk, 1),
            "bos1Skus": len(p_skus),
            "ratio": round(ratio, 3) if ratio is not None else None,
            "firstBos1Order": dates[0] if dates else None,
            "lastBos1Order": last,
            "status": status,
            "lowVolume": 0 < b_wk < LOW_VOLUME_WEEKLY,
            "lostLines": sorted(lost, key=lambda x: -x["baselineUnits"]),
            "newSkus": len(new),
            "pwm1AfterCutover": ({"units": round(lk["units"], 1), "lines": lk["lines"],
                                  "last": lk["last"]} if lk else None),
        })

    # --- Weekly series per chain (PWM1 baseline weeks, then BOS1 weeks)
    weeks = sorted({w for (_, w) in base_week} | {w for (_, w) in post_week})
    weekly = [{"week": w,
               "byGroup": {g: {"pwm1": round(base_week.get((g, w), 0.0), 1),
                               "bos1": round(post_week.get((g, w), 0.0), 1)} for g in groups}}
              for w in weeks]

    # --- SKUs: everything the roster bought in either period, plus the gaps
    gap_keys = {g["key"] for g in gap_skus}
    pos = load_pos(svc, set(meta) | gap_keys)
    feoos_hist, feoos_market = merge_feoos(
        svc, drive, previous, {norm_loc(r["name"]) for r in roster}
        | {norm_loc(n) for n in list(bos1["customerNames"].values())
           + list(baseline["customerNames"].values())},
        set(meta))
    feoos_by_sku = Counter()
    for e in feoos_hist["events"]:
        feoos_by_sku[norm_name(e["k"][2])] += e["n"]

    skus = []
    for key in sorted(set(k for (_, k) in base_ss) | bos1_buys | gap_keys):
        m = meta.get(key, {"name": key, "brand": ""})
        per = {g: {"baseUnits": 0.0, "baseLines": 0, "baseStores": 0,
                   "bos1Units": 0.0, "bos1Stores": 0} for g in groups}
        for (u, k), (units, lines) in base_ss.items():
            if k == key:
                p = per[by_uuid[u]["group"]]
                p["baseUnits"] += units
                p["baseLines"] += lines
                p["baseStores"] += 1
        for (u, k), (units, lines, _) in post_ss.items():
            if k == key:
                p = per[by_uuid[u]["group"]]
                p["bos1Units"] += units
                p["bos1Stores"] += 1
        for p in per.values():
            p["baseUnits"] = round(p["baseUnits"], 1)
            p["bos1Units"] = round(p["bos1Units"], 1)

        srcs = bos1_sources(key)
        st = stock.get(key) or {}
        doc = st.get("doc")
        if doc == 0 and not st.get("consumption60"):
            doc = None  # no demand signal, not "zero days of cover"
        item = {
            "key": key, "name": m["name"], "brand": m.get("brand") or "",
            "vendor": st.get("vendor") or "",
            "byGroup": per,
            "live": bool(srcs), "bos1Sources": srcs,
            "doc": round(doc, 1) if doc is not None else None,
            "onHand": st.get("onHand"), "onOrder": st.get("onOrder"),
            "pastDuePos": sorted(st.get("pastDue") or []),
            "feoosEvents": feoos_by_sku.get(key, 0),
            "originalGap": key in gap_keys,
        }
        if key in gap_keys:
            lines = pos.get(key, [])
            open_lines = [l for l in lines if l["open"]]
            if srcs:
                stage, si = "Live at BOS1", 3
            elif any(l["received"] for l in lines):
                stage, si = "Received", 2
            elif open_lines:
                stage, si = "On PO/TO", 1
            else:
                stage, si = "Not started", 0
            g = next(x for x in gap_skus if x["key"] == key)
            item.update({
                "stage": stage, "stageIndex": si,
                "scopedUnits": g["units"], "scopedStores": g["stores"],
                "scopedChains": g["chains"],
                "inbound": [{"po": l["po"], "type": l["type"], "vendor": l["vendor"],
                             "status": l["status"], "expected": l["expected"]}
                            for l in open_lines],
                "reorderedByRoster": key in bos1_buys,
            })
        skus.append(item)

    feoos_events = []
    loc_to_store = {}
    for r in roster:
        loc_to_store[norm_loc(r["name"])] = r["uuid"]
    for src in (baseline["customerNames"], bos1["customerNames"]):
        for u, n in src.items():
            loc_to_store[norm_loc(n)] = u
    for e in feoos_hist["events"]:
        ev, dl, item, brand, loc, qty = e["k"]
        u = loc_to_store.get(norm_loc(loc))
        feoos_events.append({
            "eventDate": ev, "deliveryDate": dl, "item": item, "brand": brand,
            "location": loc, "qty": parse_num(qty) or 0, "count": e["n"],
            "storeUuid": u, "group": by_uuid[u]["group"] if u else None,
            "cohortSku": norm_name(item) in meta,
        })
    feoos_events.sort(key=lambda x: (x["eventDate"], x["item"]), reverse=True)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": ORIGIN, "destination": DEST,
        "cutover": CUTOVER.isoformat(),
        "groups": groups,
        "thresholds": {"onTrack": ON_TRACK_RATIO, "wellBelow": WELL_BELOW_RATIO,
                       "quietDays": QUIET_DAYS, "docCritical": DOC_CRITICAL,
                       "docLow": DOC_LOW, "lostLineExpectedOrders": LOST_LINE_EXPECTED_ORDERS},
        "sources": {
            "bos1Sales": {"status": bos1_status, "capturedAt": bos1["capturedAt"],
                          "dataThrough": bos1["dataThrough"],
                          "postCutoverDays": post_days},
            "baseline": {"start": baseline["start"], "end": baseline["end"],
                         "weeks": baseline["weeks"], "frozenAt": baseline["frozenAt"]},
            "feoos": {"trackingSince": feoos_hist["trackingSince"],
                      "market14d": feoos_market},
            "pwm1Leakage": {"checked": leak is not None},
        },
        "originalGapCount": len(gap_skus),
        "stores": stores,
        "weekly": weekly,
        "skus": skus,
        "feoosEvents": feoos_events,
        # Persisted history, merged forward on the next run.
        "bos1History": bos1,
        "feoosHistory": feoos_hist,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)

    live_gaps = sum(1 for s in skus if s.get("stageIndex") == 3)
    print(f"BOS1 orders: {bos1_status} (through {bos1['dataThrough']}, "
          f"{post_days} days post-cutover) | stores ordering at BOS1: "
          f"{sum(1 for s in stores if s['bos1Units'])}/{len(stores)} | "
          f"gaps live: {live_gaps}/{len(gap_skus)} | FEOOS events tracked: "
          f"{sum(e['count'] for e in feoos_events)}")
    if bos1_status == "missing":
        print("WARNING: BOS1 Sales Tracker has been empty on every run so far — "
              "store run-rate and basket views stay empty until it populates.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else os.path.join(HERE, "..", "data.json"))
