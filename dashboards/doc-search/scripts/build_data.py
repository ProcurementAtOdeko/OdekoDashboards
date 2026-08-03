#!/usr/bin/env python3
"""Build data.json for the DOC Search dashboard.

Joins two Looker exports on (Item Uuid, Warehouse Name):

  * "On Hand & ETA.csv"       -> on hand, available eaches, 60-day consumption,
                                 current days of cover, quantity on order
  * "PO Data for Automating.csv" -> open purchase orders with expected receipt
                                 dates, so each item can show incoming ETAs

Current DOC comes straight from the sheet's "Days of Cover 60 Days Eaches".
Pipeline DOC adds everything on order (authoritative "Quantity on Order
Purchase Units", converted to eaches) on top of what is available today.
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

ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:R"

PO_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
PO_RANGE = "'PO Data for Automating.csv'!A1:N"

# A PO still counts as incoming only while it has not been received.
OPEN_PO_STATUSES = {"pending receipt", "partially received", "approved", "open"}

DOC_RED = 7
DOC_AMBER = 14

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def parse_date(s):
    """Return an ISO yyyy-mm-dd string, or None."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def po_numbers(cell):
    """Extract PO ids from a Looker list cell like ["PO112556","PO110164"]."""
    if not cell:
        return []
    return re.findall(r"PO\d+", str(cell))


def doc_bucket(days):
    if days is None:
        return "unknown"
    if days < DOC_RED:
        return "<7"
    if days < DOC_AMBER:
        return "7-14"
    if days < 30:
        return "14-30"
    if days < 60:
        return "30-60"
    return "60+"


BUCKETS = ["<7", "7-14", "14-30", "30-60", "60+", "unknown"]


def fetch(svc, spreadsheet_id, rng):
    res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
    )
    rows = res.get("values", [])
    if not rows:
        sys.exit(f"Sheet {spreadsheet_id} returned no rows for {rng}")
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    width = len(header)
    body = [r + [""] * (width - len(r)) for r in rows[1:]]
    return col, body


def require(col, names, label):
    missing = [c for c in names if c not in col]
    if missing:
        sys.exit(f"{label}: missing expected columns: {missing}")


def load_pos(svc):
    """Return {(warehouse, uuid): [po dicts]} for open POs only."""
    col, body = fetch(svc, PO_SPREADSHEET_ID, PO_RANGE)
    require(
        col,
        [
            "Purchase Order Number",
            "Item Uuid",
            "Warehouse Name",
            "Expected Receipt Date Date",
            "Purchase Order Units",
            "Purchase Unit Name",
            "Purchase Order Status",
        ],
        "PO sheet",
    )

    pos = defaultdict(dict)
    for r in body:
        status = r[col["Purchase Order Status"]].strip()
        if status.lower() not in OPEN_PO_STATUSES:
            continue
        uuid = r[col["Item Uuid"]].strip()
        wh = r[col["Warehouse Name"]].strip()
        po = r[col["Purchase Order Number"]].strip()
        if not (uuid and wh and po):
            continue
        units = parse_num(r[col["Purchase Order Units"]]) or 0.0
        received = 0.0
        if "Quantity Received PU" in col:
            received = parse_num(r[col["Quantity Received PU"]]) or 0.0
        outstanding = max(units - received, 0.0)
        # One PO can list the same item on multiple lines; keep a single entry
        # per (item, warehouse, PO) and accumulate the units.
        entry = pos[(wh, uuid)].setdefault(
            po,
            {
                "po": po,
                "eta": parse_date(r[col["Expected Receipt Date Date"]]),
                "units": 0.0,
                "unit": r[col["Purchase Unit Name"]].strip(),
                "status": status,
            },
        )
        entry["units"] += outstanding
        if entry["eta"] is None:
            entry["eta"] = parse_date(r[col["Expected Receipt Date Date"]])

    # A PO whose lines are all received but which has not been closed yet adds
    # nothing to the pipeline, so drop it rather than show a phantom ETA.
    return {
        key: ordered
        for key, v in pos.items()
        if (
            ordered := sorted(
                (p for p in v.values() if p["units"] > 0),
                key=lambda p: (p["eta"] is None, p["eta"] or ""),
            )
        )
    }


def load_onhand(svc):
    """Return deduped rows keyed by (warehouse, uuid).

    The source sheet repeats each warehouse/item combination once per stock
    line; every column we care about is constant across those repeats, so the
    first non-empty value wins rather than summing (which would inflate).
    """
    col, body = fetch(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    require(
        col,
        [
            "Warehouse Name",
            "Procurement Vendor",
            "Item Name",
            "Item Extid",
            "Consumption 60 Days",
            "Days of Cover 60 Days Eaches",
            "On Hand Purchase Units",
            "Quantity on Order Purchase Units",
            "Available Each",
            "Purchase Unit Conversion Rate",
            "Upcoming Pos Tos",
            "Past Due Pos Tos",
        ],
        "On Hand & ETA sheet",
    )

    items = {}
    for r in body:
        wh = r[col["Warehouse Name"]].strip()
        uuid = r[col["Item Extid"]].strip()
        if not (wh and uuid):
            continue
        key = (wh, uuid)
        it = items.get(key)
        if it is None:
            it = items[key] = {
                "warehouse": wh,
                "uuid": uuid,
                "name": r[col["Item Name"]].strip(),
                "vendor": r[col["Procurement Vendor"]].strip(),
                "onHandPU": None,
                "qtyOnOrderPU": None,
                "availableEach": None,
                "consumption60": None,
                "convRate": None,
                "docCurrent": None,
                "sheetPos": set(),
            }
        for src, dst in [
            ("On Hand Purchase Units", "onHandPU"),
            ("Quantity on Order Purchase Units", "qtyOnOrderPU"),
            ("Available Each", "availableEach"),
            ("Consumption 60 Days", "consumption60"),
            ("Purchase Unit Conversion Rate", "convRate"),
            ("Days of Cover 60 Days Eaches", "docCurrent"),
        ]:
            if it[dst] is None:
                v = parse_num(r[col[src]])
                if v is not None:
                    it[dst] = v
        for c in ("Upcoming Pos Tos", "Past Due Pos Tos"):
            it["sheetPos"].update(po_numbers(r[col[c]]))

    return items


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    items = load_onhand(svc)
    po_index = load_pos(svc)

    rows = []
    for (wh, uuid), it in items.items():
        consumption60 = it["consumption60"]
        daily = consumption60 / 60.0 if consumption60 else None

        doc_current = it["docCurrent"]
        # A sheet DOC of 0 with no consumption history means "no demand signal",
        # not "zero days of cover".
        if doc_current == 0 and not consumption60:
            doc_current = None
        if doc_current is not None:
            # Same precision as pipeline DOC, so the two columns never differ
            # by a rounding artefact alone.
            doc_current = round(doc_current, 1)

        conv = it["convRate"] or 1.0
        available = it["availableEach"]
        if available is None:
            available = (it["onHandPU"] or 0.0) * conv

        incoming_each = (it["qtyOnOrderPU"] or 0.0) * conv
        # Anchor pipeline DOC to the sheet's own current DOC and add only the
        # days the incoming order buys. Recomputing it from raw eaches instead
        # would drift from the current figure (Looker rounds and can use a
        # slightly different available quantity), which shows up as a pipeline
        # DOC *below* the current one even when nothing is on order.
        doc_pipeline = None
        if doc_current is not None and daily:
            doc_pipeline = round(doc_current + incoming_each / daily, 1)
        elif doc_current is not None:
            doc_pipeline = doc_current

        pos = po_index.get((wh, uuid), [])
        etas = [p["eta"] for p in pos if p["eta"]]

        rows.append(
            {
                "warehouse": wh,
                "uuid": uuid,
                "name": it["name"],
                "vendor": it["vendor"],
                "onHandPU": it["onHandPU"],
                "availableEach": round(available, 1),
                "qtyOnOrderPU": it["qtyOnOrderPU"] or 0.0,
                "incomingEach": round(incoming_each, 1),
                "consumption60": consumption60,
                "dailyUse": round(daily, 2) if daily else None,
                "docCurrent": doc_current,
                "docPipeline": doc_pipeline,
                "nextEta": min(etas) if etas else None,
                "pos": pos,
                # PO ids the on-hand sheet knows about but the PO export does
                # not, so nothing silently disappears from the item view.
                "posWithoutEta": sorted(
                    it["sheetPos"] - {p["po"] for p in pos}
                ),
            }
        )

    # Warehouse/item combinations with no stock, no demand and no orders are
    # catalogue noise; keeping them would triple the payload for no signal.
    def has_signal(r):
        return bool(
            r["consumption60"]
            or r["availableEach"]
            or r["onHandPU"]
            or r["qtyOnOrderPU"]
            or r["pos"]
            or r["posWithoutEta"]
        )

    dropped = len(rows) - sum(1 for r in rows if has_signal(r))
    rows = [r for r in rows if has_signal(r)]
    rows.sort(key=lambda r: (r["docCurrent"] is None, r["docCurrent"] or 0, r["name"]))

    # ---- aggregates -------------------------------------------------------
    dist = {b: {"current": 0, "pipeline": 0} for b in BUCKETS}
    for r in rows:
        dist[doc_bucket(r["docCurrent"])]["current"] += 1
        dist[doc_bucket(r["docPipeline"])]["pipeline"] += 1

    critical = [r for r in rows if r["docCurrent"] is not None and r["docCurrent"] < DOC_RED]
    warn = [
        r
        for r in rows
        if r["docCurrent"] is not None and DOC_RED <= r["docCurrent"] < DOC_AMBER
    ]
    # Critical today, but incoming POs lift them out of the red.
    rescued = [
        r
        for r in critical
        if r["docPipeline"] is not None and r["docPipeline"] >= DOC_RED
    ]
    rescued_keys = {(r["warehouse"], r["uuid"]) for r in rescued}
    exposed = [r for r in critical if (r["warehouse"], r["uuid"]) not in rescued_keys]

    wh_summary = defaultdict(
        lambda: {"rows": 0, "critical": 0, "warn": 0, "exposed": 0, "docs": []}
    )
    for r in rows:
        w = wh_summary[r["warehouse"]]
        w["rows"] += 1
        if r["docCurrent"] is not None:
            w["docs"].append(r["docCurrent"])
            if r["docCurrent"] < DOC_RED:
                w["critical"] += 1
                if r["docPipeline"] is None or r["docPipeline"] < DOC_RED:
                    w["exposed"] += 1
            elif r["docCurrent"] < DOC_AMBER:
                w["warn"] += 1

    warehouses = sorted(wh_summary)
    open_po_ids = {p["po"] for r in rows for p in r["pos"]}

    # ---- intern repeated strings -----------------------------------------
    # ~13k item names and ~500 vendors are shared across ~21k warehouse rows,
    # so the table ships as index-referenced arrays instead of fat objects.
    wh_idx = {w: i for i, w in enumerate(warehouses)}
    vendors, vendor_idx = [], {}
    units, unit_idx = [], {}
    items, item_idx = [], {}

    def intern(value, store, index):
        if value not in index:
            index[value] = len(store)
            store.append(value)
        return index[value]

    def num(v):
        """Emit ints where possible so the JSON stays compact."""
        if v is None:
            return None
        r = round(v, 2)
        return int(r) if r == int(r) else r

    # Item identity is the UUID alone. The procurement vendor belongs to the
    # warehouse row, not the item: the same SKU is bought direct at one site
    # and received as an intracompany transfer at another, and keying the item
    # on the vendor too would scatter one SKU across several search results.
    packed = []
    for r in rows:
        key = r["uuid"]
        if key not in item_idx:
            item_idx[key] = len(items)
            items.append([r["name"], r["uuid"]])
        packed.append(
            [
                wh_idx[r["warehouse"]],
                item_idx[key],
                intern(r["vendor"], vendors, vendor_idx),
                num(r["availableEach"]),
                num(r["qtyOnOrderPU"]),
                num(r["incomingEach"]),
                num(r["consumption60"]),
                num(r["dailyUse"]),
                num(r["docCurrent"]),
                num(r["docPipeline"]),
                r["nextEta"],
                [
                    [
                        p["po"],
                        p["eta"],
                        num(p["units"]),
                        intern(p["unit"], units, unit_idx),
                        1 if p["status"] == "Partially Received" else 0,
                    ]
                    for p in r["pos"]
                ],
                r["posWithoutEta"],
            ]
        )

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {"red": DOC_RED, "amber": DOC_AMBER},
        "warehouses": warehouses,
        # Column order of each entry in "rows" (see the packing step above).
        "rowFields": [
            "wh", "item", "vendor", "availableEach", "onOrderPU", "incomingEach",
            "consumption60", "dailyUse", "docCurrent", "docPipeline",
            "nextEta", "pos", "posWithoutEta",
        ],
        "poFields": ["po", "eta", "units", "unit", "partial"],
        "itemFields": ["name", "uuid"],
        "vendors": vendors,
        "units": units,
        "items": items,
        "summary": {
            "rowCount": len(rows),
            "droppedEmptyRows": dropped,
            "itemCount": len({r["uuid"] for r in rows}),
            "warehouseCount": len(warehouses),
            "criticalCount": len(critical),
            "warnCount": len(warn),
            "rescuedByPipeline": len(rescued),
            "exposedCount": len(exposed),
            "openPoCount": len(open_po_ids),
            "itemsAwaitingPo": sum(1 for r in rows if not r["pos"]),
        },
        "docDistribution": [
            {"bucket": b, "current": dist[b]["current"], "pipeline": dist[b]["pipeline"]}
            for b in BUCKETS
        ],
        "warehouseSummary": [
            {
                "warehouse": w,
                "rows": v["rows"],
                "critical": v["critical"],
                "warn": v["warn"],
                "exposed": v["exposed"],
                "avgDoc": round(sum(v["docs"]) / len(v["docs"]), 1) if v["docs"] else None,
            }
            for w, v in sorted(wh_summary.items(), key=lambda kv: -kv[1]["critical"])
        ],
        "rows": packed,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"Wrote {len(rows)} warehouse/item rows "
        f"({out['summary']['itemCount']} items, {len(warehouses)} warehouses, "
        f"{len(open_po_ids)} open POs) to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
