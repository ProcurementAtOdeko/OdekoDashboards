#!/usr/bin/env python3
"""Build data.json for the MDT1 SKU onboarding tracker.

Tracks a fixed cohort of SKUs (the MDT1 → DCA1 transition list, committed
alongside this script as cohort.csv) through the onboarding pipeline into
DCA1:

    Not started  →  On PO  →  Received  →  Live in catalog

Stage signals:
  - On PO           — a DCA1 purchase order exists for the item
                      ("PO Data for Automating.csv", Warehouse Name == DCA1)
  - Received        — that PO shows Quantity Received > 0 or a shipment
                      received date
  - Live in catalog — Combined Models Dump "Warehouse Raw" row for DCA1 with
                      in_catalog truthy

Transfer-order recommendation:
  MDT1 is being wound down, so where MDT1 still holds deep stock we would
  rather transfer it than buy new. Any cohort SKU whose MDT1 "Days of Cover
  60 Days Eaches" exceeds TO_DOC_THRESHOLD and which is not already received
  or live at DCA1 is flagged as a transfer-order candidate.

Sources (all in the Looker Data Dumps folder / Combined models dump):
  - PO Data for Automating.csv   — per-PO-line: item, warehouse, status,
                                   ordered/received qty, receipt date
  - Combined Models Dump "Warehouse Raw" — DCA1 in_catalog flag
  - On Hand & ETA.csv            — MDT1 on hand + days of cover

SKUs join across sources by normalized item name (lower-cased, with a
leading "(DUPLICATE) " stripped) because the numeric/uuid item keys are not
populated consistently across these exports.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

PO_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
PO_RANGE = "'PO Data for Automating.csv'!A1:N"
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:J"

TARGET_WAREHOUSE = "DCA1"   # where the SKUs are being onboarded to
SOURCE_WAREHOUSE = "MDT1"   # the warehouse being wound down
TO_DOC_THRESHOLD = 50       # MDT1 days-of-cover above which we suggest a TO

COHORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cohort.csv")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def get_values(svc, sid, rng):
    return (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sid, range=rng)
        .execute()
        .get("values", [])
    )


def row_getter(header, row):
    """Return a g(colname) accessor tolerant of short/ragged rows."""
    col = {n: i for i, n in enumerate(header)}

    def g(name):
        i = col.get(name)
        if i is None or len(row) <= i:
            return ""
        return row[i]

    return g


def load_cohort():
    """The SKUs being onboarded, in priority (units) order."""
    with open(COHORT_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    cohort = []
    for r in rows:
        name = (r.get("Item") or "").strip()
        if not name:
            continue
        cohort.append({
            "name": name,
            "brand": (r.get("Brand") or "").strip(),
            "units": parse_num(r.get("Units")) or 0.0,
            "customers": int(parse_num(r.get("Customers")) or 0),
            "lines": int(parse_num(r.get("Order Lines")) or 0),
        })
    if not cohort:
        sys.exit(f"Cohort file {COHORT_FILE} has no rows")
    return cohort


def load_pos(svc, wanted):
    """DCA1 PO lines for the cohort, keyed by normalized item name."""
    rows = get_values(svc, PO_SPREADSHEET_ID, PO_RANGE)
    if not rows:
        sys.exit("PO sheet returned no rows")
    header = rows[0]
    for c in ["Item Name", "Warehouse Name", "Purchase Order Number",
              "Purchase Order Status", "Purchase Order Units",
              "Quantity Received", "Shipment Received Utc Date"]:
        if c not in header:
            sys.exit(f"PO sheet missing expected column: {c}")

    pos = defaultdict(list)
    for r in rows[1:]:
        g = row_getter(header, r)
        if g("Warehouse Name") != TARGET_WAREHOUSE:
            continue
        key = norm_name(g("Item Name"))
        if key not in wanted:
            continue
        received = parse_num(g("Quantity Received")) or 0.0
        pos[key].append({
            "po": g("Purchase Order Number"),
            "vendor": g("Full Vendor Name"),
            "status": g("Purchase Order Status"),
            "created": g("PO Created Date Date"),
            "expected": g("Expected Receipt Date Date"),
            "receivedDate": g("Shipment Received Utc Date"),
            "ordered": parse_num(g("Purchase Order Units")) or 0.0,
            "received": received,
            "unit": g("Purchase Unit Name"),
        })
    return pos


def load_catalog(svc, wanted):
    """Cohort SKUs flagged in_catalog at DCA1."""
    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    live = set()
    if not rows:
        return live
    header = rows[0]
    for r in rows[1:]:
        g = row_getter(header, r)
        if g("warehouse_name") != TARGET_WAREHOUSE:
            continue
        if str(g("in_catalog")).strip().upper() not in ("TRUE", "1", "YES", "Y"):
            continue
        key = norm_name(g("item_name"))
        if key in wanted:
            live.add(key)
    return live


def load_mdt1_stock(svc, wanted):
    """MDT1 on-hand + days of cover for cohort SKUs (best/deepest row wins)."""
    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    stock = {}
    if not rows:
        return stock
    header = rows[0]
    for r in rows[1:]:
        g = row_getter(header, r)
        if g("Warehouse Name") != SOURCE_WAREHOUSE:
            continue
        key = norm_name(g("Item Name"))
        if key not in wanted:
            continue
        doc = parse_num(g("Days of Cover 60 Days Eaches"))
        onhand = parse_num(g("On Hand Purchase Units"))
        prev = stock.get(key)
        # Keep the row with the deepest cover — that's the transfer candidate.
        if prev is None or (doc or 0) > (prev["doc"] or 0):
            stock[key] = {
                "doc": doc,
                "onHand": onhand,
                "consumption60": parse_num(g("Consumption 60 Days")),
                "vendor": g("Procurement Vendor"),
            }
    return stock


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    cohort = load_cohort()
    wanted = {norm_name(c["name"]) for c in cohort}

    pos = load_pos(svc, wanted)
    live = load_catalog(svc, wanted)
    stock = load_mdt1_stock(svc, wanted)

    # Guard: if the PO export is mid-refresh we'd wrongly reset every SKU to
    # "not started". Only trust an empty PO map when the sheet genuinely has
    # DCA1 rows for other items.
    if not pos and not live:
        sys.exit(
            "No DCA1 PO or catalog rows matched the cohort — sources likely "
            "mid-refresh; leaving existing data.json untouched."
        )

    items = []
    for c in cohort:
        key = norm_name(c["name"])
        lines = pos.get(key, [])
        st = stock.get(key, {})

        ordered = sum(l["ordered"] for l in lines)
        received = sum(l["received"] for l in lines)
        is_live = key in live
        has_po = bool(lines)
        has_receipt = received > 0 or any(l["receivedDate"] for l in lines)

        if is_live:
            stage, stage_i = "Live in catalog", 3
        elif has_receipt:
            stage, stage_i = "Received", 2
        elif has_po:
            stage, stage_i = "On PO", 1
        else:
            stage, stage_i = "Not started", 0

        doc = st.get("doc")
        # Recommend pulling from MDT1 rather than buying when cover is deep
        # and the SKU hasn't already landed at DCA1.
        to_rec = (
            doc is not None and doc > TO_DOC_THRESHOLD and stage_i < 2
        )

        lines_sorted = sorted(lines, key=lambda l: l["created"] or "", reverse=True)
        items.append({
            "name": c["name"],
            "brand": c["brand"],
            "units": round(c["units"], 1),
            "customers": c["customers"],
            "stage": stage,
            "stageIndex": stage_i,
            "hasPo": has_po,
            "poCount": len(lines),
            "ordered": round(ordered, 1),
            "received": round(received, 1),
            "inCatalog": is_live,
            "mdt1Doc": doc,
            "mdt1OnHand": st.get("onHand"),
            "mdt1Consumption60": st.get("consumption60"),
            "toRecommended": to_rec,
            "nextExpected": min(
                (l["expected"] for l in lines if l["expected"]), default=""
            ),
            "poLines": lines_sorted,
        })

    order = {"Not started": 0, "On PO": 1, "Received": 2, "Live in catalog": 3}
    items.sort(key=lambda x: (order[x["stage"]], -x["units"]))

    counts = defaultdict(int)
    for it in items:
        counts[it["stage"]] += 1
    to_items = [i for i in items if i["toRecommended"]]
    total = len(items)
    done = counts["Live in catalog"]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targetWarehouse": TARGET_WAREHOUSE,
        "sourceWarehouse": SOURCE_WAREHOUSE,
        "docThreshold": TO_DOC_THRESHOLD,
        "summary": {
            "skusTracked": total,
            "notStarted": counts["Not started"],
            "onPo": counts["On PO"],
            "received": counts["Received"],
            "liveInCatalog": done,
            "completePct": round(100 * done / total) if total else 0,
            "toRecommended": len(to_items),
            "unitsTracked": round(sum(i["units"] for i in items), 1),
        },
        "pipeline": [
            {"stage": "Not started", "count": counts["Not started"]},
            {"stage": "On PO", "count": counts["On PO"]},
            {"stage": "Received", "count": counts["Received"]},
            {"stage": "Live in catalog", "count": counts["Live in catalog"]},
        ],
        "transferCandidates": sorted(
            (
                {
                    "name": i["name"],
                    "brand": i["brand"],
                    "units": i["units"],
                    "stage": i["stage"],
                    "mdt1Doc": i["mdt1Doc"],
                    "mdt1OnHand": i["mdt1OnHand"],
                }
                for i in to_items
            ),
            key=lambda x: -(x["mdt1Doc"] or 0),
        ),
        "items": items,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {total} SKUs "
        f"(not started {counts['Not started']}, on PO {counts['On PO']}, "
        f"received {counts['Received']}, live {done}; "
        f"{len(to_items)} transfer candidates) to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
