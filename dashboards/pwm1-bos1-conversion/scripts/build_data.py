#!/usr/bin/env python3
"""Build data.json for the PWM1 → BOS1 customer-conversion dashboard.

For the fixed roster of PWM1 customer locations transferring to BOS1 (the
Blank Street and Van Leeuwen Boston-area stores), this answers two questions:

  1. What have these customers been purchasing (from the PWM1 sales feed)?
  2. Which of those SKUs are NOT currently carried in BOS1 — i.e. the
     bring-in candidates the transfer needs.

Sources (all in the Looker Data Dumps folder / Combined models dump):
  - PWM1 sales:   newest "Network Sales Tracker - PWM1.csv" in the Looker
                  folder (per-order-line: item, customer UUID, qty, ...).
  - BOS1 carried: union of three signals so we don't falsely flag a SKU as
                  a gap when BOS1 already has it —
                    * Combined Models Dump "Warehouse Raw" rows where
                      warehouse_name == BOS1 and in_catalog is truthy,
                    * "On Hand & ETA.csv" BOS1 rows (has inventory),
                    * "BOS1 Sales Tracker.csv" (recently sold).

The roster itself lives in roster.csv next to this script — it carries the
delivery-day change each location is making, which the sales feed doesn't
know about.

SKUs join across sources by normalized item name (lower-cased, with a
leading "(DUPLICATE) " stripped) because the numeric/uuid item keys are not
populated consistently across these exports.

Actual sold units per line = SO Item Qty / Conversion Rate, matching the
network Sales Tracker convention.
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

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
SALES_FILE_NAME = "Network Sales Tracker - PWM1.csv"

# BOS1 "carried" signal sources.
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:D"
BOS1_SOLD_SPREADSHEET_ID = "1Kf3G7NztpiE08zfbOA_os0lLipqukIGqsZ61b0N9LoE"
BOS1_SOLD_RANGE = "A1:N"

WAREHOUSE = "BOS1"
ORIGIN = "PWM1"

# The transferring locations, with the delivery-day change each one is making.
ROSTER_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roster.csv")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def load_roster():
    """The transferring locations, in file order (Blank Street, then VL)."""
    roster = []
    with open(ROSTER_CSV, newline="") as f:
        for row in csv.DictReader(f):
            uuid = (row.get("UUID") or "").strip()
            if not uuid:
                continue
            roster.append({
                "uuid": uuid,
                "rosterName": (row.get("Account Name") or "").strip(),
                "group": (row.get("Group") or "").strip(),
                "location": (row.get("Location") or "").strip(),
                "state": (row.get("State") or "").strip(),
                "zip": (row.get("Zip") or "").strip(),
                "currentDeliveryDay": (row.get("Current Delivery Day") or "").strip(),
                "newDeliveryDay": (row.get("New Delivery Day") or "").strip(),
            })
    if not roster:
        sys.exit(f"Roster {ROSTER_CSV} is empty")
    return roster


_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


# --- Format-substitute matching ------------------------------------------
# A "format substitute" is the same product in a different pack/size/material
# (e.g. a "1 Liter Plastic Bottle" the customer buys vs a "750ml Glass" BOS1
# already stocks). We compute a format-agnostic key by stripping size/unit and
# container/material tokens from the item name while preserving brand, flavor,
# and any bare spec numbers (so "14 x 5" filters don't collapse into "15 x 5").
_SIZE = re.compile(
    r"\b\d+(\.\d+)?\s*(/\s*\d+)?\s*"
    r"(ml|l|liter|liters|litre|fl\s*oz|oz|gallon|gal|qt|quart|"
    r"lb|lbs|kg|g|gram|grams|ct|count|pk|pack|pcs)\b"
)
_UNITWORD = re.compile(
    r"\b(ml|l|liter|liters|litre|oz|gallon|gal|qt|quart|lb|lbs|kg|"
    r"ct|count|pk|pack|pcs)\b"
)
_CONT = re.compile(
    r"\b(glass|plastic|bottle\(s\)|bottles|bottle|can|cans|jug|jugs|jar|jars|"
    r"pouch|pouches|bag|bags|carton|box|boxes|tub|tubs|container|containers)\b"
)
_WS = re.compile(r"\s+")


def fmt_key(name):
    s = norm_name(name).replace("bottle(s)", " ")
    s = _SIZE.sub(" ", s)
    s = _UNITWORD.sub(" ", s)
    s = _CONT.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)  # keep digits (brand/spec numbers)
    return _WS.sub(" ", s).strip()


# The container/volume of an item, used to tell a true unit-for-unit swap
# (same size, only material differs — e.g. glass vs plastic) from a same-flavor
# match at a different pack size (e.g. 1L plastic vs 750ml glass).
_SIZE_ONE = re.compile(
    r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+))?\s*"
    r"(ml|l|liter|litre|gallon|gal|fl\s*oz|oz|qt|quart|lb|lbs|kg|g)\b"
)
_UNIT_CANON = {"liter": "l", "litre": "l", "gallon": "gal", "quart": "qt",
               "floz": "oz", "lbs": "lb"}
_UNIT_DISP = {"ml": "ml", "l": "L", "gal": "gal", "oz": "oz", "qt": "qt",
              "lb": "lb", "kg": "kg", "g": "g"}


def canon_size(name):
    """Canonical volume token, e.g. '1l', '750ml', '1/2gal'. '' if none."""
    m = _SIZE_ONE.search(norm_name(name))
    if not m:
        return ""
    num = m.group(1) + (("/" + m.group(2)) if m.group(2) else "")
    unit = m.group(3).replace(" ", "")
    unit = _UNIT_CANON.get(unit, unit)
    return num + unit


def pack_material(name):
    s = norm_name(name)
    for mat in ("glass", "plastic"):
        if re.search(r"\b" + mat + r"\b", s):
            return mat
    return ""


def pack_label(name):
    """Human pack descriptor, e.g. '1 L plastic', '750 ml glass', 'glass'."""
    m = _SIZE_ONE.search(norm_name(name))
    size_disp = ""
    if m:
        num = m.group(1) + ((" / " + m.group(2)) if m.group(2) else "")
        unit = m.group(3).replace(" ", "")
        unit = _UNIT_CANON.get(unit, unit)
        size_disp = num + " " + _UNIT_DISP.get(unit, unit)
    parts = [p for p in (size_disp, pack_material(name)) if p]
    return " ".join(parts)


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


def find_sales_file(drive):
    """Newest 'Network Sales Tracker - PWM1.csv' in the Looker folder."""
    q = (
        f"'{LOOKER_FOLDER_ID}' in parents and "
        f"name = '{SALES_FILE_NAME}' and trashed = false"
    )
    res = (
        drive.files()
        .list(
            q=q,
            orderBy="modifiedTime desc",
            pageSize=5,
            fields="files(id,name,modifiedTime)",
        )
        .execute()
    )
    files = res.get("files", [])
    if not files:
        sys.exit(f"Could not find '{SALES_FILE_NAME}' in Looker folder")
    return files[0]["id"]


def build_carried_set(svc):
    """SKUs BOS1 already carries (union of 3 signals).

    Returns (carried, carried_named):
      carried       — norm_name -> {source, ...}
      carried_named — norm_name -> {"name": display_name, "sources": {...}}
    carried_named preserves an original display name so we can compute
    format-substitute keys and show the specific BOS1 item to swap to.
    """
    carried = defaultdict(set)  # norm_name -> {source, ...}
    carried_named = {}          # norm_name -> {"name", "sources"}

    def add(name, source):
        k = norm_name(name)
        if k:
            carried[k].add(source)
            info = carried_named.get(k)
            if info is None:
                info = carried_named[k] = {"name": str(name).strip(), "sources": set()}
            info["sources"].add(source)

    def truthy(v):
        return str(v).strip().upper() in ("TRUE", "1", "YES", "Y")

    # 1. Combined Models Dump — BOS1 rows flagged in_catalog.
    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni, ci = col.get("warehouse_name"), col.get("item_name"), col.get("in_catalog")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ci is not None and len(r) > ci and truthy(r[ci]):
                    if ni is not None and len(r) > ni:
                        add(r[ni], "catalog")

    # 2. On Hand & ETA — BOS1 rows (has inventory).
    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni = col.get("Warehouse Name"), col.get("Item Name")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ni is not None and len(r) > ni:
                    add(r[ni], "onhand")

    # 3. BOS1 Sales Tracker — recently sold at BOS1. This export is narrower
    #    than the network trackers, so find the item column by name rather
    #    than assuming a position.
    rows = get_values(svc, BOS1_SOLD_SPREADSHEET_ID, BOS1_SOLD_RANGE)
    if rows:
        col = {str(n).strip().lower(): i for i, n in enumerate(rows[0])}
        ni = col.get("item name")
        for r in rows[1:]:
            if ni is not None and len(r) > ni:
                add(r[ni], "sold")

    return carried, carried_named


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    roster = load_roster()
    roster_by_uuid = {r["uuid"]: r for r in roster}
    wanted = set(roster_by_uuid)
    carried, carried_named = build_carried_set(svc)

    sales_id = find_sales_file(drive)
    rows = get_values(svc, sales_id, "A1:N")

    skus = {}            # norm_name -> aggregate
    cust_names = {}      # uuid -> customer name as the sales feed spells it
    cust_agg = {}        # uuid -> {units, skus:set, gapSkus:set, items:{}}
    dates = []
    data_rows = max(len(rows) - 1, 0)

    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        for c in ["Item Name", "Customer Name", "Odeko Account Uuid", "SO Item Qty",
                  "Conversion Rate", "Date Date"]:
            if c not in col:
                sys.exit(f"{ORIGIN} sheet missing expected column: {c}")
        c_name, c_brand = col["Item Name"], col.get("Brand Name")
        c_cust, c_uuid = col["Customer Name"], col["Odeko Account Uuid"]
        c_qty, c_conv, c_date = col["SO Item Qty"], col["Conversion Rate"], col["Date Date"]
        c_item_uuid = col.get("Item Uuid")

        for r in rows[1:]:
            if len(r) <= c_uuid:
                continue
            uuid = r[c_uuid]
            if uuid not in wanted:
                continue
            name = r[c_name] if len(r) > c_name else ""
            if not name:
                continue
            key = norm_name(name)
            d = r[c_date] if len(r) > c_date else ""
            if d:
                dates.append(d)

            cn = r[c_cust] if len(r) > c_cust else ""
            if cn:
                cust_names.setdefault(uuid, cn)

            qty = parse_num(r[c_qty]) if len(r) > c_qty else None
            conv = parse_num(r[c_conv]) if len(r) > c_conv else None
            units = qty / conv if (qty is not None and conv and conv > 0) else 0.0

            s = skus.get(key)
            if s is None:
                s = skus[key] = {
                    "name": name,
                    "brand": (r[c_brand] if c_brand is not None and len(r) > c_brand else "") or "",
                    "itemUuid": (r[c_item_uuid] if c_item_uuid is not None and len(r) > c_item_uuid else "") or "",
                    "units": 0.0,
                    "lines": 0,
                    "_custs": {},
                    "inBos1": key in carried,
                    "bos1Sources": sorted(carried.get(key, [])),
                }
            s["units"] += units
            s["lines"] += 1
            sc = s["_custs"].get(uuid)
            if sc is None:
                sc = s["_custs"][uuid] = {"units": 0.0, "lines": 0}
            sc["units"] += units
            sc["lines"] += 1

            ca = cust_agg.setdefault(
                uuid, {"units": 0.0, "skus": set(), "gapSkus": set(), "items": {}}
            )
            ca["units"] += units
            ca["skus"].add(key)
            if key not in carried:
                ca["gapSkus"].add(key)
            ci = ca["items"].get(key)
            if ci is None:
                ci = ca["items"][key] = {
                    "name": name,
                    "brand": (r[c_brand] if c_brand is not None and len(r) > c_brand else "") or "",
                    "units": 0.0,
                    "lines": 0,
                    "inBos1": key in carried,
                }
            ci["units"] += units
            ci["lines"] += 1

    sku_list = []
    for s in skus.values():
        cust_map = s.pop("_custs")
        s["customers"] = len(cust_map)
        s["custDetail"] = sorted(
            (
                {
                    "uuid": u,
                    "name": cust_names.get(u) or roster_by_uuid[u]["rosterName"],
                    "units": round(v["units"], 1),
                    "lines": v["lines"],
                }
                for u, v in cust_map.items()
            ),
            key=lambda x: -x["units"],
        )
        s["units"] = round(s["units"], 1)
        sku_list.append(s)
    sku_list.sort(key=lambda x: -x["units"])

    # Guard: the Looker export is periodically cleared and rewritten. If we
    # catch it mid-refresh (header row present but no data rows, or no rows
    # for any target customer), do NOT overwrite a good data.json with an
    # empty snapshot — fail loudly so the caller keeps the existing file.
    # On the very first run there is nothing to protect, so still write a
    # roster-only snapshot (flagged sourceEmpty) rather than shipping a
    # dashboard that 404s on its own data.
    source_empty = not sku_list
    if source_empty and os.path.exists(out_path):
        sys.exit(
            f"{ORIGIN} sheet has no purchase rows for the roster "
            f"(read {data_rows} data rows) — source likely mid-refresh; "
            "leaving existing data.json untouched."
        )

    gaps = [s for s in sku_list if not s["inBos1"]]
    covered = [s for s in sku_list if s["inBos1"]]

    # Top gap SKUs and brands (bring-in candidates).
    top_gap_skus = [
        {"name": s["name"], "units": s["units"], "customers": s["customers"]}
        for s in gaps[:20]
    ]
    brand_units = defaultdict(float)
    brand_gapcount = defaultdict(int)
    for s in gaps:
        b = s["brand"] or "(no brand)"
        brand_units[b] += s["units"]
        brand_gapcount[b] += 1
    top_gap_brands = sorted(
        ({"brand": b, "units": round(u, 1), "skus": brand_gapcount[b]}
         for b, u in brand_units.items()),
        key=lambda x: -x["units"],
    )[:12]

    # Format substitutes: gap SKUs where BOS1 already stocks the same product
    # in a different pack/size/material (e.g. plastic 1L bought vs 750ml glass
    # carried). Turns a "bring-in" into a "swap to a SKU we already have".
    # sameSize flags a true unit-for-unit swap vs a same-flavor size change.
    fmt_index = defaultdict(list)  # fmt_key -> [(norm_name, display, sources)]
    for nn, info in carried_named.items():
        fmt_index[fmt_key(info["name"])].append(
            (nn, info["name"], sorted(info["sources"]))
        )
    format_subs = []
    for s in gaps:
        nk = norm_name(s["name"])
        fk = fmt_key(s["name"])
        if len(fk.split()) < 2:  # too generic to match confidently
            continue
        p_size = canon_size(s["name"])
        alts = [
            {
                "name": dn,
                "sources": srcs,
                "pack": pack_label(dn),
                "sameSize": canon_size(dn) == p_size,
            }
            for (nn, dn, srcs) in fmt_index.get(fk, [])
            if nn != nk
        ]
        if alts:
            format_subs.append({
                "name": s["name"],
                "brand": s["brand"],
                "units": s["units"],
                "customers": s["customers"],
                "pack": pack_label(s["name"]),
                "substitutes": alts,
            })
    format_subs.sort(key=lambda x: -x["units"])

    customers = []
    for r in roster:
        uuid = r["uuid"]
        ca = cust_agg.get(uuid)
        items = []
        if ca:
            for it in ca["items"].values():
                items.append({
                    "name": it["name"],
                    "brand": it["brand"],
                    "units": round(it["units"], 1),
                    "lines": it["lines"],
                    "inBos1": it["inBos1"],
                })
            items.sort(key=lambda x: -x["units"])
        customers.append({
            "uuid": uuid,
            "name": cust_names.get(uuid) or r["rosterName"],
            "group": r["group"],
            "location": r["location"],
            "state": r["state"],
            "zip": r["zip"],
            "currentDeliveryDay": r["currentDeliveryDay"],
            "newDeliveryDay": r["newDeliveryDay"],
            "deliveryDayChanged": bool(
                r["newDeliveryDay"] and r["newDeliveryDay"] != r["currentDeliveryDay"]
            ),
            "hasHistory": ca is not None,
            "skus": len(ca["skus"]) if ca else 0,
            "gapSkus": len(ca["gapSkus"]) if ca else 0,
            "units": round(ca["units"], 1) if ca else 0.0,
            "items": items,
        })
    customers.sort(key=lambda c: (-c["units"], c["name"]))

    # Per-brand rollup (Blank Street vs Van Leeuwen) — the two chains move as
    # separate conversations even though they share a transfer date.
    group_agg = {}
    for c in customers:
        g = group_agg.setdefault(
            c["group"] or "(ungrouped)",
            {"group": c["group"] or "(ungrouped)", "locations": 0, "active": 0,
             "units": 0.0, "_skus": set(), "_gaps": set()},
        )
        g["locations"] += 1
        g["active"] += 1 if c["hasHistory"] else 0
        g["units"] += c["units"]
        for it in c["items"]:
            k = norm_name(it["name"])
            g["_skus"].add(k)
            if not it["inBos1"]:
                g["_gaps"].add(k)
    groups = []
    for g in group_agg.values():
        groups.append({
            "group": g["group"],
            "locations": g["locations"],
            "active": g["active"],
            "units": round(g["units"], 1),
            "skus": len(g["_skus"]),
            "gapSkus": len(g["_gaps"]),
        })
    groups.sort(key=lambda x: -x["units"])

    active = sum(1 for c in customers if c["hasHistory"])
    gap_units = round(sum(s["units"] for s in gaps), 1)
    total_units = round(sum(s["units"] for s in sku_list), 1)
    coverage_pct = round(100 * len(covered) / len(sku_list)) if sku_list else 0

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": ORIGIN,
        "destination": WAREHOUSE,
        # True when the PWM1 Looker export was empty at build time — the page
        # says so rather than presenting "0 gaps" as an answer.
        "sourceEmpty": source_empty,
        "sourceRows": data_rows,
        "dateRange": {
            "min": min(dates) if dates else None,
            "max": max(dates) if dates else None,
        },
        "summary": {
            "customersListed": len(roster),
            "customersActive": active,
            "skusPurchased": len(sku_list),
            "skusCarried": len(covered),
            "skusGap": len(gaps),
            "coveragePct": coverage_pct,
            "unitsTotal": total_units,
            "gapUnitsTotal": gap_units,
            "formatSubstitutes": len(format_subs),
            "bos1CatalogSkus": len(carried),
            "deliveryDayChanges": sum(1 for c in customers if c["deliveryDayChanged"]),
        },
        "coverage": [
            {"status": f"Carried in {WAREHOUSE}", "count": len(covered)},
            {"status": "Not carried (gap)", "count": len(gaps)},
        ],
        "groups": groups,
        "topGapSkus": top_gap_skus,
        "topGapBrands": top_gap_brands,
        "formatSubstitutes": format_subs,
        "skus": sku_list,
        "customers": customers,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    if source_empty:
        print(
            f"WARNING: {ORIGIN} sheet had no purchase rows for the roster "
            f"(read {data_rows} data rows). Wrote a roster-only {out_path} "
            f"with {len(carried)} {WAREHOUSE} catalog SKUs; it will fill in on "
            "the next refresh once the Looker export repopulates."
        )
    else:
        print(
            f"Wrote {len(sku_list)} SKUs ({len(gaps)} gaps) across {active} active "
            f"customers to {out_path}"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
