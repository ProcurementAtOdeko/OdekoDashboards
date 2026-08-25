#!/usr/bin/env python3
"""Build data.json for the MDT1 → DCA1 customer-conversion dashboard.

For a fixed list of MDT1 customer location UUIDs being converted to DCA1,
this answers two questions:

  1. What have these customers been purchasing (from the MDT1 sales feed)?
  2. Which of those SKUs are NOT currently carried in DCA1 — i.e. the
     bring-in candidates the conversion needs.

Sources (all in the Looker Data Dumps folder / Combined models dump):
  - MDT1 sales:   newest "Network Sales Tracker - MDT1.csv" in the Looker
                  folder (per-order-line: item, customer UUID, qty, ...).
  - DCA1 carried: union of three signals so we don't falsely flag a SKU as
                  a gap when DCA1 already has it —
                    * Combined Models Dump "Warehouse Raw" rows where
                      warehouse_name == DCA1 and in_catalog is truthy,
                    * "On Hand & ETA.csv" DCA1 rows (has inventory),
                    * "DCA1 Sales Tracker Trailing 90.csv" (recently sold).

SKUs join across sources by normalized item name (lower-cased, with a
leading "(DUPLICATE) " stripped) because the numeric/uuid item keys are not
populated consistently across these exports.

Actual sold units per line = SO Item Qty / Conversion Rate, matching the
network Sales Tracker convention.
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

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
MDT1_FILE_NAME = "Network Sales Tracker - MDT1.csv"

# DCA1 "carried" signal sources.
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:D"
DCA1_SOLD_SPREADSHEET_ID = "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs"
DCA1_SOLD_RANGE = "A1:C"

WAREHOUSE = "DCA1"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# The MDT1 customer locations being converted to DCA1 (Group 2 aligned
# list). Names come from the roster so locations with no MDT1 purchase
# history still render with a label; the sales feed name wins when present.
CUSTOMERS = [
    ("88b708ae-06fc-4495-9060-3d77cd6be950", "Artifact Coffee - 1500 Union Ave"),
    ("d8143089-c480-4143-bd2c-a05f54d62cec", "Back Creek Cafe & Boat Supply - 7310 Edgewood Rd"),
    ("8e7f4544-6ea6-4397-bc74-1d71bcda1d1c", "Bean Rush Cafe - Crownsville"),
    ("ad13b290-d24f-4713-9c19-87dd12c49766", "Bean Rush Cafe - Glen Burnie"),
    ("831b2e49-68dd-41d3-8db1-7db19a746219", "Beauty by Jo llc - 502 Westgate Road"),
    ("a400ffa1-815c-4817-96eb-c826898b02fd", "Black Acres Roastery - 1720 Edison Hwy"),
    ("66b6c017-b9e6-481d-9782-58bbe1544208", "Blackcap Coffee Concepts & Blackcap Pour Studio - 1707 Saint Paul Street"),
    ("be0f9db5-a0e0-4469-9052-bb8b088ef23b", "Blue Rooster Cafe - 1372 Cape St Claire Rd"),
    ("57dec49c-a9a3-40a4-bfec-ea86abff6fd2", "Bon Fresco - Oakland Mills Rd"),
    ("21f893eb-fd9a-4532-98ce-9a3e933eaa0a", "Cafe Olé - 33 1/2 West Street"),
    ("4b59e1bb-d052-4820-8202-53a39ff7c4fd", "Capo Italian Deli - Annapolis - 139 Main St"),
    ("847b9501-2908-48df-8a65-e5e98a75fe3e", "CBRC LLC dba Chesapeake Coffee Roasters - 2100 Concord Boulevard"),
    ("db56c472-4b05-497a-86d6-632c17d852db", "Centrado Cafe Shop - 15530 Old Columbia Pike"),
    ("5d8b9264-fcff-4ab3-b49c-0d89be7ce985", "Coffee Land - 222 N Charles St # A"),
    ("7f820124-c597-41b0-9cb7-2d5159a50184", "Cove Cafe - 2600 Tower Oaks Blvd"),
    ("66d54bfe-f0d2-4e83-92f6-9f81fc8b165c", "Cube Coffee - 8492 Baltimore National Pike"),
    ("1bbd0189-7f80-432c-b88f-f33e9ee6680d", "David and Dad's Cafe - 115 N Charles St"),
    ("098ef508-4ce4-4647-9b2d-fa5b5969cd21", "Ivy by the Lake - 46110 Lake Center Plaza"),
    ("966024dc-249b-4340-9c79-891c1b45a0df", "Jaliyaa Coffee - 5038 Oakmoore Dr"),
    ("d72506ae-c4b7-4241-8554-579be5adf412", "Java Nation - 11120 Rockville Pike"),
    ("64efba8c-fde3-4c2e-b272-2c815516ca94", "Jems Bottle & Cafe - 2200 East Fayette Street"),
    ("551e1c3a-69fc-4ec9-8ad1-7216e6d589bb", "Kneads Bakeshop - Canton - 3601 Boston St"),
    ("dace9b1c-3f61-4e93-a75c-7312ef992a43", "Kneads Bakeshop - Cross Keys - 6 Village Square"),
    ("afddcebe-179f-49f4-86d8-0643750a0781", "Kneads Bakeshop - Harbor East - 506 S Ctrl Ave"),
    ("a7746087-0d4b-49a3-8202-8523d14a2c1e", "Kyo Matcha - Columbia"),
    ("619696e0-1b37-4639-8866-7d88cb188894", "Little Market Cafe - 3731 Hamilton Street"),
    ("e1186f80-fb5d-4cf9-896e-8a3d6b6a25c9", "Magothy roasting company - 8116 Forest Glen Drive"),
    ("7c3582a4-6f79-4363-a197-43902f3a7a68", "Market House, LLC - 25 Market Space"),
    ("ebcf259d-c6aa-4204-babc-73efc839cb3f", "Mehfil Cafe - 7 North Calvert Street"),
    ("68fe1e79-3dfa-4099-a032-81ceca71db0d", "merrit star pharmacy - 5022 Rome Red Way"),
    ("810cf95e-71ff-47b4-b45e-82d5feb4cb7b", "Mirabeau - 5751 Fishers Lane"),
    ("ecb938dd-12ef-4ace-89b6-f44bf5ba95ac", "Miskiri Hospitality Group - 2 East Wells St"),
    ("7cfb5a6b-d049-4124-bf46-202a0284667f", "Morning Mugs - 15 West Hughes Street"),
    ("074c63cf-744f-4c82-a6e0-ad531181f481", "Morning Mugs Coffee - 15 West Hughes Street"),
    ("c8041d8f-4427-4624-923d-371da8cbc641", "Old Mill Cafe - Ellicott City"),
    ("1544bb63-e61d-422d-86c2-387ff379820e", "Order and Chaos Coffee - 1410 Key Hwy"),
    ("8d361f48-e28e-40cf-b29b-9bee280e6d11", "Others Coffee - 9922 Evergreen Avenue"),
    ("4b274a8c-e64c-4852-a53b-3c6f3239eecc", "PJ's Coffee - 4501 - Camp Springs MD"),
    ("efb7e2b9-1162-45c4-8dd3-3e44e010e6d4", "Quartermaine - 4972 Wyaconda Road"),
    ("c5a56314-b962-4957-8e08-516db05daf29", "Ragamuffins Coffee House - 385 Main St"),
    ("8261c545-198b-4cd4-9c52-49ce3fc10346", "ROCO Kitchen + Coffee - 6430 Freetown Road"),
    ("dd3c4d7c-267b-4897-bdba-ef99994a53d0", "Roggenart - Baltimore - 1001 North Charles"),
    ("badd9cf5-d195-4135-a05f-07cfcf34562a", "Roggenart - Catonsville 706 Frederick Road"),
    ("58a10284-52b8-41e7-9222-420ef7d352c3", "Roggenart - Columbia - 6476 Dobbin Center Way"),
    ("6006308b-6ac6-4784-ba1d-5c3bfb014086", "Roggenart - Savage - 8600 Foundry St #2091"),
    ("7182a5f1-3811-44ae-8344-138b881ab429", "Root City Kava Bar and Lounge - 312 Washington Ave"),
    ("09331910-f859-423c-89f5-19ea55a99a92", "ruya juice bar cafe - 4606 Eastern Avenue"),
    ("fbcbd5a1-958d-4e90-840a-044dbb857916", "Sandy Pony Donuts - Annapolis"),
    ("5983b2f4-e51e-4c43-b1ab-90c401c715f6", "Sidamo Coffee and Tea - Fulton - 8180 Maple Lawn Blvd"),
    ("59edebfc-d2d4-4ca7-b28c-a141e777812c", "The Fountain at Drug City - 2805 North Point Rd"),
    ("9e5225b4-ae06-445c-b0de-c81df1b931f6", "THE pearl - 10285 Little Patuxent Parkway"),
    ("2b1dd9f2-bff1-4c6e-bc94-e241bee9e418", "Thread Coffee - 1812 Greenmount Avenue"),
    ("617c35f2-8993-4d99-bfd7-bccedd929234", "Trifecto Bar - 12250 Clarksville Pike suite a"),
    ("c63202f6-a198-4b24-8ebc-6066a4d59979", "two5eats inc. - Love Melts - 613 Emerson Place"),
    ("a7845c1d-1737-409e-a729-293c0a6df8c4", "Vent Coffee Roasters - 1700 W 41st St"),
    ("0ee21cde-237f-4f88-84a8-fe238d9ddabe", "Wild Bean Coffee - 1532 Rockville Pike"),
    ("572317a2-65be-45d2-bfe6-df85f05be314", "WildBay kombucha - 4820 Seton Drive"),
]

CUSTOMER_UUIDS = [u for u, _ in CUSTOMERS]
ROSTER_NAMES = dict(CUSTOMERS)

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


# --- Format-substitute matching ------------------------------------------
# A "format substitute" is the same product in a different pack/size/material
# (e.g. a "1 Liter Plastic Bottle" the customer buys vs a "750ml Glass" DCA1
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


def find_mdt1_file(drive):
    """Newest 'Network Sales Tracker - MDT1.csv' in the Looker folder."""
    q = (
        f"'{LOOKER_FOLDER_ID}' in parents and "
        f"name = '{MDT1_FILE_NAME}' and trashed = false"
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
        sys.exit(f"Could not find '{MDT1_FILE_NAME}' in Looker folder")
    return files[0]["id"]


def build_carried_set(svc):
    """SKUs DCA1 already carries (union of 3 signals).

    Returns (carried, carried_named):
      carried       — norm_name -> {source, ...}
      carried_named — norm_name -> {"name": display_name, "sources": {...}}
    carried_named preserves an original display name so we can compute
    format-substitute keys and show the specific DCA1 item to swap to.
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

    # 1. Combined Models Dump — DCA1 rows flagged in_catalog.
    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni, ci = col.get("warehouse_name"), col.get("item_name"), col.get("in_catalog")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ci is not None and len(r) > ci and truthy(r[ci]):
                    if ni is not None and len(r) > ni:
                        add(r[ni], "catalog")

    # 2. On Hand & ETA — DCA1 rows (has inventory).
    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni = col.get("Warehouse Name"), col.get("Item Name")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ni is not None and len(r) > ni:
                    add(r[ni], "onhand")

    # 3. DCA1 Sales Tracker Trailing 90 — recently sold at DCA1.
    rows = get_values(svc, DCA1_SOLD_SPREADSHEET_ID, DCA1_SOLD_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        ni = col.get("Item Name")
        for r in rows[1:]:
            if ni is not None and len(r) > ni:
                add(r[ni], "sold90")

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

    wanted = set(CUSTOMER_UUIDS)
    carried, carried_named = build_carried_set(svc)

    mdt1_id = find_mdt1_file(drive)
    rows = get_values(svc, mdt1_id, "A1:M")
    if not rows:
        sys.exit("MDT1 sales sheet returned no rows")
    col = {n: i for i, n in enumerate(rows[0])}
    for c in ["Item Name", "Customer Name", "Odeko Account Uuid", "SO Item Qty",
              "Conversion Rate", "Date Date"]:
        if c not in col:
            sys.exit(f"MDT1 sheet missing expected column: {c}")
    c_name, c_brand = col["Item Name"], col.get("Brand Name")
    c_cust, c_uuid = col["Customer Name"], col["Odeko Account Uuid"]
    c_qty, c_conv, c_date = col["SO Item Qty"], col["Conversion Rate"], col["Date Date"]
    c_item_uuid = col.get("Item Uuid")

    skus = {}            # norm_name -> aggregate
    cust_names = {}      # uuid -> most recent/seen customer name
    cust_agg = {}        # uuid -> {units, skus:set, gapSkus:set}
    dates = []

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
                "inDca1": key in carried,
                "dca1Sources": sorted(carried.get(key, [])),
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
                "inDca1": key in carried,
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
                    "name": cust_names.get(u) or ROSTER_NAMES.get(u, ""),
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
    # for any target customer), do NOT overwrite the last-good data.json with
    # an empty snapshot — fail loudly so the caller keeps the existing file.
    if not sku_list:
        sys.exit(
            "MDT1 sheet has no purchase rows for the target customers "
            f"(read {len(rows) - 1} data rows) — source likely mid-refresh; "
            "leaving existing data.json untouched."
        )

    gaps = [s for s in sku_list if not s["inDca1"]]
    covered = [s for s in sku_list if s["inDca1"]]

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

    # Format substitutes: gap SKUs where DCA1 already stocks the same product
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
    for uuid in CUSTOMER_UUIDS:
        ca = cust_agg.get(uuid)
        items = []
        if ca:
            for it in ca["items"].values():
                items.append({
                    "name": it["name"],
                    "brand": it["brand"],
                    "units": round(it["units"], 1),
                    "lines": it["lines"],
                    "inDca1": it["inDca1"],
                })
            items.sort(key=lambda x: -x["units"])
        customers.append({
            "uuid": uuid,
            "name": cust_names.get(uuid) or ROSTER_NAMES.get(uuid, ""),
            "hasHistory": ca is not None,
            "skus": len(ca["skus"]) if ca else 0,
            "gapSkus": len(ca["gapSkus"]) if ca else 0,
            "units": round(ca["units"], 1) if ca else 0.0,
            "items": items,
        })
    customers.sort(key=lambda c: (-c["units"], c["name"]))

    active = sum(1 for c in customers if c["hasHistory"])
    gap_units = round(sum(s["units"] for s in gaps), 1)
    total_units = round(sum(s["units"] for s in sku_list), 1)
    coverage_pct = round(100 * len(covered) / len(sku_list)) if sku_list else 0

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dateRange": {
            "min": min(dates) if dates else None,
            "max": max(dates) if dates else None,
        },
        "summary": {
            "customersListed": len(CUSTOMER_UUIDS),
            "customersActive": active,
            "skusPurchased": len(sku_list),
            "skusCarried": len(covered),
            "skusGap": len(gaps),
            "coveragePct": coverage_pct,
            "unitsTotal": total_units,
            "gapUnitsTotal": gap_units,
            "formatSubstitutes": len(format_subs),
        },
        "coverage": [
            {"status": "Carried in DCA1", "count": len(covered)},
            {"status": "Not carried (gap)", "count": len(gaps)},
        ],
        "topGapSkus": top_gap_skus,
        "topGapBrands": top_gap_brands,
        "formatSubstitutes": format_subs,
        "skus": sku_list,
        "customers": customers,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {len(sku_list)} SKUs ({len(gaps)} gaps) across {active} active "
        f"customers to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
