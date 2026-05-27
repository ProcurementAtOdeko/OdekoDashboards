#!/usr/bin/env python3
"""Build data.json for the DCA1 Tracking dashboard.

Reads today's date-stamped tab in the DCA1 Tracking spreadsheet, the Mapping
tab (item_class -> min/max OH), and the MOQ Surfacing and Automating
spreadsheet. Produces per-vendor and per-item order-action data plus a
short historical trend pulled from prior dated tabs.

Reorder logic (v1, intentionally simple so it's easy to iterate on):
    inventory <= Min OH  ->  order = Max OH - inventory   (in purchase units)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httplib2
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

# Respect a custom CA bundle if the runtime sets one (e.g. SSL_CERT_FILE in a
# sandboxed env). httplib2 doesn't read that env var on its own.
_CA = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
if _CA:
    httplib2.CA_CERTS = _CA

SPREADSHEET_ID = "162M43zm7D65Z3JqHpPqh1pa5Xrd6NLLvPuDu9qmpM8M"
MOQ_SPREADSHEET_ID = "1zNDxmJETDp6IGFiYL04wZU_lak8CBMNHfoz5KmlFzTs"
WAREHOUSE = "DCA1"
WAREHOUSE_TZ = ZoneInfo("America/New_York")
TREND_TAB_LIMIT = 10
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DATED_TAB_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{2})-(\d{2})$")


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        f = float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def col_index(header, name):
    # MOQ sheet has trailing space in "MOQ Quantity " — be lenient
    for i, h in enumerate(header):
        if h.strip() == name.strip():
            return i
    return None


def get_values(svc, sheet_id, rng):
    return (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=rng)
        .execute()
        .get("values", [])
    )


def list_tabs(svc, sheet_id):
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return [s["properties"]["title"] for s in meta["sheets"]]


def dated_tabs_sorted(tab_names):
    """Return dated tabs (newest first) as list of (title, MM-DD)."""
    found = []
    for t in tab_names:
        m = DATED_TAB_RE.match(t)
        if m:
            found.append((t, f"{m.group(2)}-{m.group(3)}"))
    # Sort by MM-DD descending. Year-rollover handled implicitly: workflow
    # runs daily so the newest tab is the one the source most recently
    # wrote, and sheet creation order keeps newest first in the tab list.
    return sorted(found, key=lambda x: x[1], reverse=True)


def expected_today_tab():
    now = datetime.now(WAREHOUSE_TZ)
    return now.strftime("%a %m-%d")


def load_mapping(svc):
    rows = get_values(svc, SPREADSHEET_ID, "Mapping!A1:C")
    if not rows:
        return {}
    header = rows[0]
    ci_class = col_index(header, "item_class")
    ci_max = col_index(header, "Max OH")
    ci_min = col_index(header, "Min OH")
    out = {}
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        key = (r[ci_class] or "").strip()
        if not key:
            continue
        mx = parse_num(r[ci_max])
        mn = parse_num(r[ci_min])
        if mx is None or mn is None:
            continue
        out[key] = {"min": mn, "max": mx}
    return out


def parse_moq(constraint, measure):
    """Derive {type, quantity, unit} from MOQ constraint + measure text.

    Prefers the parenthetical canonical quantity (e.g. "1 Pallet (120 cases)"
    -> 120 cases). Quantity MOQs in cases are actionable against
    casesToOrder; everything else (dollars, weight, cadence) is
    informational only until we have cost / weight data.
    """
    constraint = (constraint or "").strip().lower()
    measure = (measure or "").strip()

    if not constraint and not measure:
        return {"type": "none", "quantity": None, "unit": None}
    if "no moq" in constraint:
        return {"type": "none", "quantity": None, "unit": None}
    if "cadence" in constraint or "delivery constraint" in constraint:
        return {"type": "cadence", "quantity": None, "unit": None}

    if "$ spend" in constraint or constraint.startswith("$"):
        m = re.search(r'\$\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)', measure)
        if m:
            amt = float(m.group(1).replace(",", ""))
            suffix = m.group(2).lower()
            if suffix == "k":
                amt *= 1000
            elif suffix == "m":
                amt *= 1_000_000
            return {"type": "dollars", "quantity": amt, "unit": "$"}
        return {"type": "dollars", "quantity": None, "unit": "$"}

    if "weight" in constraint:
        m = re.search(r'(\d{1,6}(?:,\d{3})*(?:\.\d+)?)\s*lbs?', measure, re.IGNORECASE)
        if m:
            return {"type": "weight", "quantity": float(m.group(1).replace(",", "")), "unit": "lbs"}
        return {"type": "weight", "quantity": None, "unit": "lbs"}

    if "quantity" in constraint:
        # Eaches first (parenthetical preferred)
        m = (re.search(r'\((\d{1,6}(?:,\d{3})*)\s+eaches?\)', measure, re.IGNORECASE)
             or re.search(r'(\d{1,6}(?:,\d{3})*)\s+eaches?', measure, re.IGNORECASE))
        if m:
            return {"type": "eaches", "quantity": float(m.group(1).replace(",", "")), "unit": "eaches"}
        # Cases: parenthetical canonical
        m = re.search(r'\((\d{1,6}(?:,\d{3})*)\s+cases?\)', measure, re.IGNORECASE)
        if m:
            return {"type": "cases", "quantity": float(m.group(1).replace(",", "")), "unit": "cases"}
        # Cases: any "N cases" not immediately followed by "per ..."
        for m in re.finditer(r'(\d{1,6}(?:,\d{3})*)\s+cases?\b', measure, re.IGNORECASE):
            after = measure[m.end():m.end() + 6].lower()
            if not after.startswith(" per"):
                return {"type": "cases", "quantity": float(m.group(1).replace(",", "")), "unit": "cases"}
        # Last-resort cases match
        m = re.search(r'(\d{1,6}(?:,\d{3})*)\s+cases?', measure, re.IGNORECASE)
        if m:
            return {"type": "cases", "quantity": float(m.group(1).replace(",", "")), "unit": "cases"}
        # Pure pallet count (no case info)
        m = re.search(r'(\d+(?:\.\d+)?)\s+pallets?', measure, re.IGNORECASE)
        if m:
            return {"type": "pallets", "quantity": float(m.group(1)), "unit": "pallets"}

    return {"type": "other", "quantity": None, "unit": None}


def load_moq(svc):
    rows = get_values(svc, MOQ_SPREADSHEET_ID, "Dataset!A1:G")
    if not rows:
        return {"by_id": {}, "by_name": {}}
    header = rows[0]
    ci_name = col_index(header, "vendor_name")
    ci_id = col_index(header, "vendor_id")
    ci_constraint = col_index(header, "MOQ constraint")
    ci_measure = col_index(header, "MOQ measure")
    by_id, by_name = {}, {}
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        name = (r[ci_name] or "").strip()
        vid = (r[ci_id] or "").strip()
        if not name and not vid:
            continue
        constraint = (r[ci_constraint] or "").strip()
        measure = (r[ci_measure] or "").strip()
        parsed = parse_moq(constraint, measure)
        rec = {
            "constraint": constraint,
            "measure": measure,
            "type": parsed["type"],
            "quantity": parsed["quantity"],
            "unit": parsed["unit"],
        }
        if vid:
            by_id[vid] = rec
        if name:
            by_name[name.lower()] = rec
    return {"by_id": by_id, "by_name": by_name}


def lookup_moq(moq, vendor_id, vendor_name):
    if vendor_id and vendor_id in moq["by_id"]:
        return moq["by_id"][vendor_id]
    if vendor_name and vendor_name.lower() in moq["by_name"]:
        return moq["by_name"][vendor_name.lower()]
    return None


def read_today_tab(svc, tab_title):
    rows = get_values(svc, SPREADSHEET_ID, f"'{tab_title}'!A1:CF")
    if not rows:
        return None, []
    header = rows[0]
    return header, rows[1:]


def build_items(header, rows, mapping):
    """Yield per-SKU records with min/max + computed order qty."""
    needed = {
        "vendor_name": col_index(header, "vendor_name"),
        "vendor_id": col_index(header, "vendor_id"),
        "item_id": col_index(header, "item_id"),
        "item_name": col_index(header, "item_name"),
        "item_class": col_index(header, "item_class"),
        "inventory": col_index(header, "inventory"),
        "net_days_of_cover": col_index(header, "net_days_of_cover"),
        "past_due": col_index(header, "past_due"),
        "purchase_orders_past_due": col_index(header, "purchase_orders_past_due"),
        "arriving_today": col_index(header, "arriving_today"),
        "purchase_orders": col_index(header, "purchase_orders"),
        "purchase_unit": col_index(header, "purchase_unit"),
        "as_of": col_index(header, "as_of"),
        # Fields needed to emit po_upload_template-shaped CSVs
        "warehouse_location_id": col_index(header, "warehouse_location_id"),
        "warehouse_uuid": col_index(header, "warehouse_uuid"),
        "procurement_vendor_uuid": col_index(header, "procurement_vendor_uuid"),
        "item_uuid": col_index(header, "item_uuid"),
        "delivery_date": col_index(header, "delivery_date"),
        # Ti/Hi (pallet) constraints — round order up to nearest layer
        "cases_per_layer": col_index(header, "cases_per_layer"),
        "layers_per_pallet": col_index(header, "layers_per_pallet"),
        "cases_per_pallet": col_index(header, "cases_per_pallet"),
        # Source's own order signals (cross-check, not authoritative)
        "source_order_qty": col_index(header, "Order QTY"),
        "source_ti_hi": col_index(header, "Ti/Hi"),
    }
    items = []
    as_of = None
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        item_class = (r[needed["item_class"]] or "").strip()
        if not item_class:
            continue
        inv = parse_num(r[needed["inventory"]])
        if inv is None:
            continue
        m = mapping.get(item_class)
        if not m:
            continue
        order_qty_raw = max(0.0, m["max"] - inv) if inv <= m["min"] else 0.0

        # Ti/Hi rounding: snap order up to the nearest full layer when
        # cases_per_layer is set. cases_per_pallet drives a separate
        # "full pallet" hint we just expose to the UI for now.
        cpl = parse_num(r[needed["cases_per_layer"]])
        lpp = parse_num(r[needed["layers_per_pallet"]])
        cpp = parse_num(r[needed["cases_per_pallet"]])
        if order_qty_raw > 0 and cpl and cpl > 0:
            order_qty = math.ceil(order_qty_raw / cpl) * cpl
        else:
            order_qty = order_qty_raw

        past_due = parse_num(r[needed["past_due"]]) or 0
        po_past_due = parse_num(r[needed["purchase_orders_past_due"]]) or 0
        if order_qty > 0:
            status = "needs_order"
        elif past_due > 0 or po_past_due > 0:
            status = "past_due"
        else:
            status = "on_track"
        items.append({
            "vendor": (r[needed["vendor_name"]] or "").strip(),
            "vendorId": (r[needed["vendor_id"]] or "").strip(),
            "vendorUuid": (r[needed["procurement_vendor_uuid"]] or "").strip(),
            "itemId": (r[needed["item_id"]] or "").strip(),
            "itemUuid": (r[needed["item_uuid"]] or "").strip(),
            "name": (r[needed["item_name"]] or "").strip(),
            "itemClass": item_class,
            "inventory": round(inv, 2),
            "minOh": m["min"],
            "maxOh": m["max"],
            "orderQtyRaw": round(order_qty_raw, 2),
            "orderQty": round(order_qty, 2),
            "casesPerLayer": cpl,
            "layersPerPallet": lpp,
            "casesPerPallet": cpp,
            "tiHiBumped": order_qty > order_qty_raw + 1e-9,
            "sourceOrderQty": parse_num(r[needed["source_order_qty"]]),
            "sourceTiHi": parse_num(r[needed["source_ti_hi"]]),
            "purchaseUnit": (r[needed["purchase_unit"]] or "").strip(),
            "deliveryDate": (r[needed["delivery_date"]] or "").strip(),
            "warehouseLocationId": (r[needed["warehouse_location_id"]] or "").strip(),
            "warehouseUuid": (r[needed["warehouse_uuid"]] or "").strip(),
            "netDoc": parse_num(r[needed["net_days_of_cover"]]),
            "pastDue": past_due,
            "poPastDue": po_past_due,
            "arrivingToday": parse_num(r[needed["arriving_today"]]) or 0,
            "openPos": parse_num(r[needed["purchase_orders"]]) or 0,
            "status": status,
        })
        if as_of is None:
            as_of = r[needed["as_of"]] or None
    return items, as_of


def build_vendors(items, moq):
    by_vendor = defaultdict(lambda: {
        "vendor": "", "vendorId": "",
        "casesToOrder": 0.0, "skusToOrder": 0,
        "skusPastDue": 0, "skusTotal": 0,
    })
    for it in items:
        key = it["vendorId"] or it["vendor"].lower()
        v = by_vendor[key]
        if not v["vendor"]:
            v["vendor"] = it["vendor"]
            v["vendorId"] = it["vendorId"]
        v["skusTotal"] += 1
        if it["orderQty"] > 0:
            v["casesToOrder"] += it["orderQty"]
            v["skusToOrder"] += 1
        if it["status"] == "past_due":
            v["skusPastDue"] += 1

    out = []
    for v in by_vendor.values():
        m = lookup_moq(moq, v["vendorId"], v["vendor"])
        moq_type = m["type"] if m else "unknown"
        moq_qty = m["quantity"] if m else None
        moq_unit = m["unit"] if m else None
        moq_constraint = m["constraint"] if m else ""
        moq_measure = m["measure"] if m else ""
        cases = round(v["casesToOrder"], 2)
        pct = None
        if not m or moq_type in ("none", "unknown"):
            status = "no_moq"
        elif moq_type == "cadence":
            status = "cadence_moq"
        elif moq_type == "dollars":
            status = "dollar_moq"
        elif moq_type == "weight":
            status = "weight_moq"
        elif moq_type == "eaches":
            status = "eaches_moq"
        elif moq_type == "pallets":
            status = "pallet_moq"
        elif moq_type == "cases" and moq_qty:
            pct = (cases / moq_qty * 100)
            status = "meets" if cases >= moq_qty else ("short" if cases > 0 else "inactive")
        else:
            status = "other_moq"
        out.append({
            "vendor": v["vendor"],
            "vendorId": v["vendorId"],
            "moqConstraint": moq_constraint,
            "moqMeasure": moq_measure,
            "moqType": moq_type,
            "moqQuantity": moq_qty,
            "moqUnit": moq_unit,
            "casesToOrder": cases,
            "skusToOrder": v["skusToOrder"],
            "skusPastDue": v["skusPastDue"],
            "skusTotal": v["skusTotal"],
            "pctToMoq": round(pct, 1) if pct is not None else None,
            "status": status,
        })
    return out


def trend_summary(svc, tab_title, mapping):
    """Lightweight summary for one dated tab — just counts."""
    rows = get_values(svc, SPREADSHEET_ID, f"'{tab_title}'!A1:CF")
    if not rows:
        return None
    header = rows[0]
    ci_class = col_index(header, "item_class")
    ci_inv = col_index(header, "inventory")
    ci_pd = col_index(header, "past_due")
    ci_po_pd = col_index(header, "purchase_orders_past_due")
    if None in (ci_class, ci_inv):
        return None
    needs_order = 0
    past_due = 0
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        ic = (r[ci_class] or "").strip()
        inv = parse_num(r[ci_inv])
        if not ic or inv is None:
            continue
        m = mapping.get(ic)
        if m and inv <= m["min"]:
            needs_order += 1
        pd = parse_num(r[ci_pd]) if ci_pd is not None else 0
        ppd = parse_num(r[ci_po_pd]) if ci_po_pd is not None else 0
        if (pd or 0) > 0 or (ppd or 0) > 0:
            past_due += 1
    m = DATED_TAB_RE.match(tab_title)
    label = f"{m.group(2)}-{m.group(3)}" if m else tab_title
    return {"date": label, "tab": tab_title, "skusToOrder": needs_order, "pastDue": past_due}


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    http = AuthorizedHttp(creds, http=httplib2.Http(ca_certs=_CA)) if _CA else None
    svc = build(
        "sheets", "v4",
        credentials=None if http else creds,
        http=http,
        cache_discovery=False,
    )

    tab_names = list_tabs(svc, SPREADSHEET_ID)
    dated = dated_tabs_sorted(tab_names)
    if not dated:
        sys.exit("No dated tabs found in DCA1 Tracking")

    expected = expected_today_tab()
    tab_titles = [t[0] for t in dated]
    today_tab = expected if expected in tab_titles else tab_titles[0]

    mapping = load_mapping(svc)
    moq = load_moq(svc)

    header, rows = read_today_tab(svc, today_tab)
    if header is None:
        sys.exit(f"Tab {today_tab} is empty")

    items, as_of = build_items(header, rows, mapping)
    vendors = build_vendors(items, moq)

    # Trends: today + up to TREND_TAB_LIMIT-1 prior tabs (oldest -> newest)
    trend_tabs = []
    for title, _ in dated[:TREND_TAB_LIMIT]:
        if title == today_tab:
            continue
        trend_tabs.append(title)
    trend_tabs = trend_tabs[: TREND_TAB_LIMIT - 1]
    trend = []
    for t in reversed(trend_tabs):
        s = trend_summary(svc, t, mapping)
        if s:
            trend.append(s)
    # Append today
    today_summary = {
        "date": dict(dated).get(today_tab, today_tab),
        "tab": today_tab,
        "skusToOrder": sum(1 for i in items if i["status"] == "needs_order"),
        "pastDue": sum(1 for i in items if i["status"] == "past_due"),
    }
    trend.append(today_summary)

    raw_total = sum(i["orderQtyRaw"] for i in items if i["orderQty"] > 0)
    rounded_total = sum(i["orderQty"] for i in items if i["orderQty"] > 0)
    summary = {
        "skusToOrder": sum(1 for i in items if i["status"] == "needs_order"),
        "vendorsWithAction": sum(1 for v in vendors if v["skusToOrder"] > 0),
        "vendorsMeetingMoq": sum(1 for v in vendors if v["status"] == "meets"),
        "vendorsShortOfMoq": sum(1 for v in vendors if v["status"] == "short"),
        "skusPastDue": sum(1 for i in items if i["status"] == "past_due"),
        "totalCasesToOrder": round(rounded_total, 1),
        "rawCasesToOrder": round(raw_total, 1),
        "tiHiOverorder": round(rounded_total - raw_total, 1),
        "skusBumpedByTiHi": sum(1 for i in items if i.get("tiHiBumped")),
        "totalSkus": len(items),
    }

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouse": WAREHOUSE,
        "sourceTab": today_tab,
        "asOf": as_of,
        "summary": summary,
        "vendors": sorted(
            vendors,
            key=lambda v: (
                v["status"] != "short",
                -(v["pctToMoq"] or 0) if v["status"] == "short" else 0,
                -v["casesToOrder"],
            ),
        ),
        "items": sorted(
            items,
            key=lambda i: (i["status"] != "needs_order", -(i["orderQty"] or 0)),
        ),
        "trend": trend,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote tab={today_tab} items={len(items)} vendors={len(vendors)} "
        f"to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
