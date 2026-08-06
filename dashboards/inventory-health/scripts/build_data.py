#!/usr/bin/env python3
"""Build per-warehouse data files for the Inventory Health Scorecard.

Reads nine Looker/NetSuite exports via the Sheets API (service-account auth),
joins them by (warehouse, item id), scores every SKU, and writes one
data/<WAREHOUSE>.json per market plus a data/manifest.json the front-end uses
to render the warehouse switcher.

The join and the scoring both run here rather than in the browser: the raw
sources are ~180k rows across nine sheets, and shipping them to every viewer
meant a multi-megabyte download and a fragile client-side pipeline. Each
warehouse file is a few thousand already-scored rows instead.

Metric definitions (these mirror the reference workbook; the periods are NOT
interchangeable, so read the comment on each before changing one):

  avg inv  = (Beginning Inv On-hand Value + Ending Inv On-hand Value) / 2,
             from the stock ledger. Covers ONE month.
  DIO      = avg inv * 182 / 6-month COGS. The 6-month margin COGS is the
             stable denominator; a single month of ledger output value swings
             wildly for seasonal items (one slow month read as 777 days for an
             item whose real cover was 342). Ledger output value is only the
             fallback, over its own 30-day window. Capped at 9999 = dead stock,
             because a near-zero trailing COGS otherwise yields 20k+ days.
  ITO      = 6-month COGS * 2 / avg inv (annualised), ledger fallback * 12.
  GMROI    = (net revenue - COGS) / avg inv. Only scored when BOTH a ledger
             value and a COGS figure exist -- absent data is not evidence of
             poor return.
  velocity = recent 3-month monthly average / prior 9-month monthly average.
             A ratio, not a percentile rank.

Score = DIO(0-30) + ABC(0-25) + velocity(0-20) + customers(0-15) + GMROI(0-10),
normalised to the points actually achievable for that item so that a SKU
missing inventory data isn't handed a free pass toward KEEP.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# (spreadsheet id, A1 range, header row index within the returned rows).
# header row index None => detect the densest of the first 10 rows.
SOURCES = {
    "oh": ("1D4WMx0zvSlVH2ROdqJb8di0bQYoxPJvizMkSL8HLufQ",
           "'On Hand Automation.csv'!A1:O", 0),
    # ABC/XYZ carry a month-label row above the real header, and their monthly
    # columns repeat the same name, so the header lives on row index 1.
    "abc": ("1_FIMGtcHlIG36ybWGsWtWUG9-ATxkUFzwBlZy1Of3i8", "'ABC'!A1:AE", 1),
    "xyz": ("1BSbgPqVX8YI_X0E45fA8_-4pL_0YNMctbUPxmsCcon4", "'XYZ'!A1:AI", 1),
    "custSku": ("1DlVvTpy1z1Gdv6VATAQtbeP0aUQK-EH0z1GRLfpks80",
                "'Active SKU/WH Customers Automation.csv'!A1:F", 0),
    "custMkt": ("1M4MGluu2hvG_0HDB4KO-QoGwkQy9_FOKyPV00CVO6dY",
                "'Active Warehouse Customers.csv'!A1:D", 0),
    "margin": ("12QS8Kva_512I_oDS-gNle7dCpgCJzntiOIcpOttWaOo", "'Sheet1'!A1:G", 0),
    # Six rows of NetSuite report chrome sit above the header.
    "ledger": ("13Yp1zREpOFCAkEIajli5lgWdwZAZLsi7e25yod6X_tc",
               "'Stock Ledger'!A1:V", 6),
    "proc": ("1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ",
             "'Warehouse Raw'!A1:H", 0),
    # Read the First Fulfillment Ledger directly. The "First Fullfillment
    # Automation" sheet is only an IMPORTRANGE of this file plus a TODAY()-F
    # column; when the import is empty that column computes against a blank
    # cell and yields a date, not a day count.
    "ffl": ("1xQ4up0z56zvCKZH1g5fLpbgv2R1rFRFt6GL-6kUFUlE",
            "'First Fulfillment Ledger.csv'!A1:H", 0),
}

# Append-only decision log written by the Sunset Review Web App. Optional:
# until scripts/sunset_apps_script.gs is deployed the sheet won't exist and
# the build carries on with no team decisions applied.
REVIEW_LOG_ID = os.environ.get("SUNSET_REVIEW_LOG_ID", "").strip()
REVIEW_LOG_RANGE = "'Sunset Review Log'!A1:G"

NEW_TO_MARKET_DAYS = 90
DIO_CAP = 9999
MARGIN_PERIOD_DAYS = 182   # the margin export is trailing 6 months
LEDGER_PERIOD_DAYS = 30    # the stock ledger covers one month

WAREHOUSE_KEYS = ("Warehouse Name", "warehouse_name", "Warehouse",
                  "Warehouse (Picked) Warehouse Name", "Location")


# ---------------------------------------------------------------- utilities

_NUM_STRIP = re.compile(r"[$,\s]")


def num(value):
    """Parse '$2,357,489.76' / '1,234' / '' into a float. 0.0 when unparseable."""
    if value is None:
        return 0.0
    text = _NUM_STRIP.sub("", str(value))
    if not text or text in {"-", "."}:
        return 0.0
    neg = text.startswith("(") and text.endswith(")")
    if neg:
        text = text[1:-1]
    try:
        out = float(text)
    except ValueError:
        return 0.0
    return -out if neg else out


def pct(value):
    """Parse '16.08%' into 0.1608; bare numbers pass through as fractions."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if text.endswith("%"):
        return num(text[:-1]) / 100.0
    return num(text)


def get(row, *names):
    """First non-empty value among the named columns."""
    for name in names:
        val = row.get(name)
        if val not in (None, ""):
            return val
    return ""


def warehouse_of(row):
    return str(get(row, *WAREHOUSE_KEYS)).strip()


def to_records(rows, header_idx):
    """Turn raw sheet rows into dicts keyed by (stripped) header name.

    Duplicate header names keep their FIRST occurrence, which matters for the
    ABC/XYZ exports where every month column repeats the same label -- the
    aggregate columns we actually want sit past them under unique names.
    """
    if not rows:
        return []
    if header_idx is None:
        counts = [sum(1 for c in r if str(c).strip())
                  for r in rows[:10]] or [0]
        # Prefer the earliest row within 2 cells of the densest: an export
        # whose header has a blank leading cell is otherwise beaten by its
        # own data rows.
        threshold = max(counts) - 2
        header_idx = next(i for i, c in enumerate(counts) if c >= threshold)

    header = [str(h).strip() for h in rows[header_idx]]
    seen, cols = set(), []
    for i, name in enumerate(header):
        cols.append(None if (not name or name in seen) else name)
        seen.add(name)

    out = []
    for raw in rows[header_idx + 1:]:
        rec = {}
        for i, name in enumerate(cols):
            if name is not None:
                rec[name] = str(raw[i]).strip() if i < len(raw) else ""
        if any(rec.values()):
            out.append(rec)
    return out


def fetch(svc, key):
    """Raw sheet rows. Callers decide how to interpret the header."""
    sid, rng, _ = SOURCES[key]
    rows = (svc.spreadsheets().values()
            .get(spreadsheetId=sid, range=rng).execute().get("values", []))
    print(f"  {key:8s} {max(len(rows) - 1, 0):>7,} rows", file=sys.stderr)
    return rows


def records(key, rows):
    return to_records(rows, SOURCES[key][2])


# ------------------------------------------------------------------ scoring

def score_dio(dio):
    if dio > 365:
        return 30
    if dio > 180:
        return 25
    if dio > 90:
        return 18
    if dio > 60:
        return 10
    if dio > 30:
        return 5
    return 0


def score_abc(cat_abc):
    # Category class only. Averaging in the sub-category class is not what the
    # reference workbook does and halves the penalty on genuine C items.
    return {"C": 25, "B": 10}.get(cat_abc, 0)


def score_velocity(vel):
    # vel is None when the item has no XYZ history at all, and 0.0 when it has
    # history but no recent sales -- both are the maximum-penalty case.
    if vel is None or vel == 0:
        return 20
    if vel <= 2:
        return 12
    if vel <= 5:
        return 9
    if vel <= 10:
        return 5
    if vel <= 20:
        return 2
    return 0


def score_customers(count):
    if count == 0:
        return 15
    if count <= 2:
        return 12
    if count <= 5:
        return 9
    if count <= 10:
        return 5
    if count <= 20:
        return 2
    return 0


def score_gmroi(gmroi):
    if gmroi < 0.5:
        return 10
    if gmroi < 1:
        return 8
    if gmroi < 1.5:
        return 5
    if gmroi < 2:
        return 3
    if gmroi < 3:
        return 1
    return 0


def recommendation(total):
    if total < 40:
        return "KEEP"
    if total <= 54:
        return "WATCH"
    return "SUNSET"


# ------------------------------------------------------------------ loaders

def enrich_abc(rows):
    """Fill blank inventory_classification per (warehouse, category).

    The upstream sheet only pre-classifies a couple of markets; everywhere
    else the column is blank. Same rule the sheet uses: rank by revenue,
    A through 80% of cumulative category revenue, B to 95%, C beyond.
    """
    groups = defaultdict(list)
    filled = 0
    for row in rows:
        if get(row, "inventory_classification"):
            continue
        key = (warehouse_of(row), get(row, "Picked Items Category"))
        groups[key].append(row)

    for items in groups.values():
        items.sort(key=lambda r: -num(r.get("_revenue")))
        total = sum(num(r.get("_revenue")) for r in items)
        if total <= 0:
            continue
        running = 0.0
        for row in items:
            running += num(row.get("_revenue"))
            share = running / total
            row["inventory_classification"] = (
                "A" if share <= 0.80 else "B" if share <= 0.95 else "C")
            filled += 1
    return filled


def load_abc(raw_rows):
    """Index ABC by (warehouse, item id), reading the aggregate revenue column.

    Every month column in this export repeats the label "Orders Deliveries and
    Invoices Invoiced Amount", and the item's 12-month total repeats it once
    more. Matching by name therefore lands on the oldest month -- which is
    mostly blank -- so find the total positionally instead: it is the column
    immediately left of running_category_revenue. That survives new months
    being appended, which a hardcoded column letter would not.
    """
    header_idx = SOURCES["abc"][2]
    header = [str(h).strip() for h in raw_rows[header_idx]]
    try:
        revenue_idx = header.index("running_category_revenue") - 1
    except ValueError:
        sys.exit("ABC export: running_category_revenue column not found; "
                 "cannot locate the aggregate revenue column")
    if revenue_idx < 0 or header[revenue_idx] != \
            "Orders Deliveries and Invoices Invoiced Amount":
        sys.exit(f"ABC export: expected the aggregate revenue column left of "
                 f"running_category_revenue, found {header[revenue_idx]!r}")

    # Build records straight from the raw rows: to_records drops blank lines,
    # so zipping its output back against raw_rows would silently misalign.
    def cell(raw, name):
        try:
            i = header.index(name)
        except ValueError:
            return ""
        return str(raw[i]).strip() if i < len(raw) else ""

    rows = []
    for raw in raw_rows[header_idx + 1:]:
        if not any(str(c).strip() for c in raw):
            continue
        rows.append({
            "Netsuite Items Item ID": cell(raw, "Netsuite Items Item ID"),
            "Warehouse (Picked) Warehouse Name":
                cell(raw, "Warehouse (Picked) Warehouse Name"),
            "Picked Items Category": cell(raw, "Picked Items Category"),
            "Picked Items Sub-Category": cell(raw, "Picked Items Sub-Category"),
            "inventory_classification": cell(raw, "inventory_classification"),
            "sub_category_classification": cell(raw, "sub_category_classification"),
            "_revenue": num(raw[revenue_idx]) if revenue_idx < len(raw) else 0.0,
        })

    filled = enrich_abc(rows)
    index = {}
    for row in rows:
        wh = warehouse_of(row)
        item = str(get(row, "Netsuite Items Item ID")).strip()
        if wh and item:
            index[(wh, item)] = row
    return index, filled


def load_xyz(rows):
    index = {}
    for row in rows:
        wh = warehouse_of(row)
        item = str(get(row, "Netsuite Items Item ID")).strip()
        if wh and item:
            index[(wh, item)] = row
    return index


def load_simple(rows, id_keys):
    index = {}
    for row in rows:
        wh = warehouse_of(row)
        item = str(get(row, *id_keys)).strip()
        if wh and item:
            index[(wh, item)] = row
    return index


def load_ledger(rows):
    """Index the ledger by id and, separately, by lowercased item name.

    Most ledger rows arrive with a blank Internal ID -- joining on it alone
    matched under a tenth of the catalogue. The item name is the only other
    key present, and it is unique within a warehouse.
    """
    by_id, by_name = {}, {}
    blank_ids = 0
    for row in rows:
        wh = warehouse_of(row)
        if not wh:
            continue
        item = str(get(row, "Internal ID")).strip()
        name = str(get(row, "Item")).strip().lower()
        if item:
            by_id[(wh, item)] = row
        else:
            blank_ids += 1
        if name:
            by_name.setdefault((wh, name), row)
    return by_id, by_name, blank_ids


def load_proc(rows):
    """Item ids that are live in the procurement model, per warehouse.

    in_catalog=FALSE means the item is in the model but delisted, so it is
    not Active for scoring purposes.
    """
    active = defaultdict(set)
    for row in rows:
        wh = warehouse_of(row)
        item = str(get(row, "item_id", "Item ID")).strip()
        if not wh or not item:
            continue
        if str(row.get("in_catalog", "")).strip().upper() == "FALSE":
            continue
        active[wh].add(item)
    return active


def load_days_active(rows):
    """(warehouse, item) -> days since first fulfillment.

    Returns an empty index when the export is empty, which is its current
    state; callers must treat "no entry" as unknown rather than as new.
    """
    index = {}
    for row in rows:
        wh = warehouse_of(row)
        item = str(get(row, "Item ID")).strip()
        days = num(get(row, "days Active"))
        if wh and item and days > 0:
            index[(wh, item)] = int(days)
    return index


def load_market_customers(rows):
    """Latest month's total ordering customers per warehouse."""
    latest = {}
    for row in rows:
        wh = warehouse_of(row)
        month = str(row.get("Date Month", "")).strip()
        count = num(get(row, "# of Ordering Customers"))
        if not wh or count <= 0:
            continue
        if wh not in latest or month > latest[wh][0]:
            latest[wh] = (month, count)
    return {wh: count for wh, (_, count) in latest.items()}


def load_review_log(svc):
    """Latest team decision per (warehouse, item) from the append-only log."""
    if not REVIEW_LOG_ID:
        return {}
    try:
        rows = (svc.spreadsheets().values()
                .get(spreadsheetId=REVIEW_LOG_ID, range=REVIEW_LOG_RANGE)
                .execute().get("values", []))
    except Exception as exc:                                  # noqa: BLE001
        print(f"  review log unavailable: {str(exc)[:120]}", file=sys.stderr)
        return {}

    decisions = {}
    for rec in to_records(rows, 0):
        wh = str(rec.get("warehouse", "")).strip()
        item = str(rec.get("item_id", "")).strip()
        decision = str(rec.get("decision", "")).strip().upper()
        if not wh or not item or decision not in {"CONFIRM", "KEEP"}:
            continue
        stamp = str(rec.get("timestamp_utc", "")).strip()
        prior = decisions.get((wh, item))
        # The log is append-only, so the newest timestamp wins and older
        # entries stay put as an audit trail.
        if prior and prior["at"] > stamp:
            continue
        decisions[(wh, item)] = {
            "decision": decision,
            "by": str(rec.get("decided_by", "")).strip(),
            "note": str(rec.get("notes", "")).strip(),
            "at": stamp,
        }
    print(f"  review   {len(decisions):>7,} decisions", file=sys.stderr)
    return decisions


# --------------------------------------------------------------------- join

def build_items(wh, raw, idx, decisions):
    """Join, score and return every SKU on hand in one warehouse."""
    proc_active = idx["proc"].get(wh, set())
    total_customers = idx["custMkt"].get(wh, 0)
    items = []

    for row in raw:
        item_id = str(get(row, "Item ID")).strip()
        if not item_id:
            continue
        name = str(get(row, "Item Name")).strip()
        key = (wh, item_id)

        abc = idx["abc"].get(key, {})
        xyz = idx["xyz"].get(key, {})
        cust = idx["custSku"].get(key, {})
        margin = idx["margin"].get(key, {})
        ledger = (idx["ledgerById"].get(key)
                  or idx["ledgerByName"].get((wh, name.lower()))
                  or {})

        # --- status -----------------------------------------------------
        # With no procurement model for a warehouse we cannot tell live SKUs
        # from delisted ones, so we do not guess Inactive.
        base = "Active" if not proc_active else (
            "Active" if item_id in proc_active else "Inactive")
        days_active = idx["daysActive"].get(key)
        cat_abc = str(get(abc, "inventory_classification")).strip().upper()
        xyz_class = str(get(xyz, "xyz_classification")).strip().upper()
        net_revenue = num(get(margin, "net revenue"))
        begin_value = num(get(ledger, "Beginning Inv On-hand Value"))

        no_signal = (base == "Active" and not cat_abc and not xyz_class
                     and not net_revenue and not begin_value)
        if base == "Active" and days_active is not None and days_active < NEW_TO_MARKET_DAYS:
            status = "New To Market"
        elif no_signal:
            status = "No Data"
        else:
            status = base

        # --- on-hand, converted to purchase units -----------------------
        # Every quantity in this export is in eaches; the dashboard talks in
        # purchase units, so a case-of-12 reads as 12 not 144.
        conversion = max(num(get(row, "Sales Unit Conversion Rate")), 1)
        qoh_each = num(get(row, "quantity on hand eaches"))
        cost_each = num(get(row, "Average Cost Dollars Eaches"))
        qoh = qoh_each / conversion
        on_order = num(get(row, "Quantity on Order Each")) / conversion
        cost_unit = cost_each * conversion
        on_hand_cost = num(get(row, "Total On Hand Cost")) or qoh_each * cost_each
        # Neither of these is a column in the source; both are derived.
        inv_pipe = qoh + on_order
        total_value = inv_pipe * cost_unit

        # --- metrics ----------------------------------------------------
        cost_amount = num(get(margin, "Cost Amount"))
        end_value = num(get(ledger, "Ending Inv On-hand Value"))
        output_value = num(get(ledger, "Value of Outputs"))
        avg_inventory = (begin_value + end_value) / 2
        has_ledger = avg_inventory > 0
        has_gmroi = has_ledger and cost_amount > 0

        dio = 0.0
        if has_ledger:
            if cost_amount > 0:
                dio = min(avg_inventory * MARGIN_PERIOD_DAYS / cost_amount, DIO_CAP)
            elif output_value > 0:
                dio = min(avg_inventory * LEDGER_PERIOD_DAYS / output_value, DIO_CAP)
            else:
                dio = DIO_CAP          # stock on hand, no sales signal at all

        if has_ledger and cost_amount > 0:
            ito = cost_amount * 2 / avg_inventory
        elif has_ledger and output_value > 0:
            ito = output_value * 12 / avg_inventory
        else:
            ito = 0.0
        gmroi = (net_revenue - cost_amount) / avg_inventory if has_gmroi else 0.0

        recent = num(get(xyz, "recent_3_months_units"))
        prior = num(get(xyz, "prior_9_months_units"))
        has_xyz = bool(xyz_class or recent or prior)
        if not has_xyz:
            velocity = None
        elif prior > 0:
            velocity = (recent / 3) / (prior / 9)
        else:
            velocity = 2.0 if recent > 0 else 0.0

        customers = int(num(get(cust, "# of Ordering Customers")))

        # --- score ------------------------------------------------------
        if status == "Active":
            dio_pts = score_dio(dio) if has_ledger else 0
            abc_pts = score_abc(cat_abc)
            vel_pts = score_velocity(velocity)
            cust_pts = score_customers(customers)
            gmroi_pts = score_gmroi(gmroi) if has_gmroi else 0
            raw_score = dio_pts + abc_pts + vel_pts + cust_pts + gmroi_pts
            # Grade against the points this item could actually have earned.
            # Otherwise an item with no inventory data can never lose the 40
            # points behind DIO and GMROI, and coasts under the KEEP line.
            achievable = 100 - (0 if has_ledger else 30) - (0 if has_gmroi else 10)
            total = round(raw_score / achievable * 100)
            rec = recommendation(total)
        else:
            dio_pts = abc_pts = vel_pts = cust_pts = gmroi_pts = 0
            achievable, total, rec = 100, None, ""

        review = decisions.get(key)
        if review and status == "Active":
            # A SKU the team vetoed becomes WATCH; a confirmed one stays
            # SUNSET but is flagged so it can be pulled into the kill list.
            if review["decision"] == "KEEP":
                rec = "WATCH"

        items.append({
            "id": item_id,
            "name": name,
            "brand": str(get(row, "Catalog Brand")).strip(),
            "category": str(get(row, "Item Category Name")).strip(),
            "subCategory": str(get(row, "item subcategory name")).strip(),
            "saleUnit": str(get(row, "Sale Units Name")).strip(),
            "status": status,
            "daysActive": days_active,
            "qoh": round(qoh, 2),
            "onOrder": round(on_order, 2),
            "invPipe": round(inv_pipe, 2),
            "consumption30": round(num(get(row, "Consumption 30 Days")) / conversion, 2),
            "consumption60": round(num(get(row, "Consumption 60 Days")) / conversion, 2),
            "costPerUnit": round(cost_unit, 4),
            "onHandCost": round(on_hand_cost, 2),
            "totalValue": round(total_value, 2),
            "revenue12mo": round(num(abc.get("_revenue", 0)), 2),
            "units12mo": round(num(get(xyz, "total_units_dynamic")), 2),
            "catABC": cat_abc,
            "subCatABC": str(get(abc, "sub_category_classification")).strip().upper(),
            "xyzClass": xyz_class,
            "coefficientOfVariation": round(num(get(xyz, "coefficient_of_variation")), 4),
            "velocity": None if velocity is None else round(velocity, 4),
            "customers": customers,
            "customerPct": round(customers / total_customers, 6) if total_customers else None,
            "netRevenue": round(net_revenue, 2),
            "costAmount": round(cost_amount, 2),
            "marginPct": round(pct(get(margin, "Margin")), 6),
            "avgInventory": round(avg_inventory, 2),
            "outputValue": round(output_value, 2),
            "dio": round(dio, 1),
            "ito": round(ito, 3),
            "gmroi": round(gmroi, 3),
            "hasLedger": has_ledger,
            "hasGmroi": has_gmroi,
            "scoreDio": dio_pts,
            "scoreAbc": abc_pts,
            "scoreVelocity": vel_pts,
            "scoreCustomers": cust_pts,
            "scoreGmroi": gmroi_pts,
            "scoreMax": achievable,
            "score": total,
            "recommendation": rec,
            "review": review,
        })

    # Share of the category's customer relationships, once every item is known.
    per_category = defaultdict(int)
    for item in items:
        per_category[item["category"]] += item["customers"]
    for item in items:
        pool = per_category[item["category"]]
        item["customerCategoryPct"] = round(item["customers"] / pool, 6) if pool else None

    return items


def coverage(items):
    active = [i for i in items if i["status"] == "Active"]
    counts = defaultdict(int)
    for item in items:
        counts[item["status"]] += 1
    recs = defaultdict(int)
    for item in active:
        if item["recommendation"]:
            recs[item["recommendation"]] += 1
    return {
        "items": len(items),
        "status": dict(counts),
        "recommendations": dict(recs),
        "withAbc": sum(1 for i in items if i["catABC"]),
        "withXyz": sum(1 for i in items if i["xyzClass"]),
        "withMargin": sum(1 for i in items if i["costAmount"]),
        "withLedger": sum(1 for i in items if i["hasLedger"]),
        "withDaysActive": sum(1 for i in items if i["daysActive"] is not None),
        "reviewed": sum(1 for i in items if i["review"]),
        "onHandCost": round(sum(i["onHandCost"] for i in items), 2),
        "sunsetOnHandCost": round(
            sum(i["onHandCost"] for i in active if i["recommendation"] == "SUNSET"), 2),
    }


def main(out_dir):
    raw_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_key:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw_key), scopes=SCOPES)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    print("Fetching sources:", file=sys.stderr)
    raw = {key: fetch(svc, key) for key in SOURCES}
    on_hand = records("oh", raw["oh"])
    if not on_hand:
        sys.exit("On Hand export returned no rows; refusing to publish")

    abc_index, abc_filled = load_abc(raw["abc"])
    ledger_by_id, ledger_by_name, ledger_blank = load_ledger(
        records("ledger", raw["ledger"]))
    days_active = load_days_active(records("ffl", raw["ffl"]))
    decisions = load_review_log(svc)

    idx = {
        "abc": abc_index,
        "xyz": load_xyz(records("xyz", raw["xyz"])),
        "custSku": load_simple(records("custSku", raw["custSku"]), ("Item ID",)),
        "margin": load_simple(records("margin", raw["margin"]), ("Item ID",)),
        "ledgerById": ledger_by_id,
        "ledgerByName": ledger_by_name,
        "proc": load_proc(records("proc", raw["proc"])),
        "daysActive": days_active,
        "custMkt": load_market_customers(records("custMkt", raw["custMkt"])),
    }

    print(f"  ABC classified client-side: {abc_filled:,}", file=sys.stderr)
    print(f"  ledger rows with blank Internal ID: {ledger_blank:,}", file=sys.stderr)
    if not days_active:
        print("  WARNING: First Fulfillment Ledger is empty -- "
              "'New To Market' cannot be determined", file=sys.stderr)

    by_warehouse = defaultdict(list)
    for row in on_hand:
        wh = warehouse_of(row)
        if wh:
            by_warehouse[wh].append(row)

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(out_dir, exist_ok=True)
    warehouses = []

    for wh in sorted(by_warehouse):
        items = build_items(wh, by_warehouse[wh], idx, decisions)
        if not items:
            continue
        stats = coverage(items)
        # No timestamp in here on purpose: it lives in the manifest instead.
        # Stamping each warehouse file would make all 23 differ on every run
        # even when the underlying data is identical, so the refresh job would
        # commit the whole ~27MB hourly rather than only what actually moved.
        payload = {
            "warehouse": wh,
            "totalCustomers": idx["custMkt"].get(wh),
            "coverage": stats,
            "items": items,
        }
        path = os.path.join(out_dir, f"{wh}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        warehouses.append({"code": wh, "coverage": stats})
        print(f"  {wh:9s} {stats['items']:>5,} items  "
              f"{stats['recommendations'].get('SUNSET', 0):>4} sunset", file=sys.stderr)

    manifest = {
        "generatedAt": generated_at,
        "sources": {
            "daysActiveAvailable": bool(days_active),
            "reviewLogConnected": bool(REVIEW_LOG_ID and decisions is not None
                                       and REVIEW_LOG_ID != ""),
            "abcClassifiedInBuild": abc_filled,
        },
        "warehouses": warehouses,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)

    print(f"\nWrote {len(warehouses)} warehouses to {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         os.path.join(os.path.dirname(__file__), "..", "data"))
