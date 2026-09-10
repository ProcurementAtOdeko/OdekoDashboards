#!/usr/bin/env python3
"""Build data.json for the MDT1 → DCA1 seasonal gap dashboard.

The conversion plan is sized from what the converting customers are buying
*now* (trailing 90 days). That window ends before the autumn ramp, so a SKU
these customers only order in October looks like nothing at all — and the
bring-in plan never picks it up.

This dashboard sets the same cohort's October-2025 purchases beside their
current demand and asks, per SKU:

  - does DCA1 already carry it?
  - is it in the MDT1 SKU onboarding plan?
  - if neither, how much did the cohort buy last October, and how many cases
    would cover the same demand this year?

The third bucket is the point: seasonal demand the conversion would walk
into unstocked.

Sources (all in the Looker Data Dumps folder / Combined models dump):
  - Last October:  "Network Sales Next Month, Last Year.csv" — network-wide
                   order lines for 2025-10-01 → 2025-10-31, filtered to the
                   converting cohort.
  - Current:       newest "Network Sales Tracker - MDT1.csv", same cohort.
  - DCA1 carried:  union of three signals (catalog / on hand / sold in the
                   trailing 90) so a SKU DCA1 already has is never called a gap.
  - Bring-in plan: the MDT1 SKU onboarding tracker's cohort, read from the
                   repo so the two dashboards can't drift apart.
  - Order refs:    PO feed + On Hand for item UUID, vendor and purchase unit;
                   DCA1 ordering model and the V2 model for the NetSuite
                   UUIDs the bulk PO tool needs.

Both feeds express quantity the same way: actual sold units = SO Item Qty /
Conversion Rate. The two windows are different lengths, so the comparison
normalises current demand to October's 31 days before calling anything a
seasonal lift.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

LOOKER_FOLDER_ID = "1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7"
MDT1_FILE_NAME = "Network Sales Tracker - MDT1.csv"

# Last-October baseline. Same 13-column schema as the network sales trackers.
LASTYEAR_SPREADSHEET_ID = "1lnUdqz5GsWkevXYSiQxWcDXdm0ZZUdkSk5YZg58uATA"
LASTYEAR_RANGE = "'Network Sales Next Month, Last Year.csv'!A1:M"

# DCA1 "carried" signal sources.
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:J"
DCA1_SOLD_SPREADSHEET_ID = "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs"
DCA1_SOLD_RANGE = "A1:C"

# Ordering references. The PO export is the only source carrying item UUIDs
# for SKUs DCA1 doesn't stock yet, which is most of a seasonal gap list.
PO_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
PO_RANGE = "'PO Data for Automating.csv'!A1:N"
# DCA1 ordering model — warehouse_uuid / location id, and vendor UUIDs for
# vendors DCA1 already buys from.
DCA1_MODEL_SPREADSHEET_ID = "162M43zm7D65Z3JqHpPqh1pa5Xrd6NLLvPuDu9qmpM8M"
# Network-wide V2 model. Vendor UUIDs are universal, so a vendor DCA1 has
# never bought from still has one wherever else in the network it is used.
V2_SPREADSHEET_ID = "14cQNxWLX4Cqb2Upp-_C6TmRC0-NUNKWYzq4K_3X6mdM"
V2_RANGE = "'Warehouse Raw'!A1:AR"

WAREHOUSE = "DCA1"
OCTOBER_DAYS = 31           # the baseline window, 2025-10-01 → 2025-10-31

# The MDT1 SKU onboarding tracker's cohort — what we are already bringing on.
ONBOARDING_COHORT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mdt1-sku-onboarding", "scripts", "cohort.csv",
)
ONBOARDING_DATA = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mdt1-sku-onboarding", "data.json",
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# The MDT1 customer locations converting to DCA1 (Group 2 aligned list) —
# the same roster the conversion dashboard tracks.
CUSTOMERS = [
    ("3fa9f9f0-113e-40ad-b3f7-a796a2c1f179", "Addis Coffee Trading DBA Warka Coffee - 500 New Jersey Ave NW"),
    ("88b708ae-06fc-4495-9060-3d77cd6be950", "Artifact Coffee - 1500 Union Ave"),
    ("d8143089-c480-4143-bd2c-a05f54d62cec", "Back Creek Cafe & Boat Supply - 7310 Edgewood Rd"),
    ("8e7f4544-6ea6-4397-bc74-1d71bcda1d1c", "Bean Rush Cafe - Crownsville"),
    ("ad13b290-d24f-4713-9c19-87dd12c49766", "Bean Rush Cafe - Glen Burnie"),
    ("831b2e49-68dd-41d3-8db1-7db19a746219", "Beauty by Jo llc - 502 Westgate Road"),
    ("a400ffa1-815c-4817-96eb-c826898b02fd", "Black Acres Roastery - 1720 Edison Hwy"),
    ("66b6c017-b9e6-481d-9782-58bbe1544208", "Blackcap Coffee Concepts & Blackcap Pour Studio - 1707 Saint Paul Street"),
    ("be0f9db5-a0e0-4469-9052-bb8b088ef23b", "Blue Rooster Cafe - 1372 Cape St Claire Rd"),
    ("f062df16-1b93-43bc-82ce-10ac9333428b", "Bon Fresco - Gaither Rd"),
    ("57dec49c-a9a3-40a4-bfec-ea86abff6fd2", "Bon Fresco - Oakland Mills Rd"),
    ("21f893eb-fd9a-4532-98ce-9a3e933eaa0a", "Cafe Olé - 33 1/2 West Street"),
    ("5f504934-1ab3-47d3-9537-d7c105c50726", "Café Alice - 440 First Street Northwest"),
    ("4b59e1bb-d052-4820-8202-53a39ff7c4fd", "Capo Italian Deli - Annapolis - 139 Main St"),
    ("2c3b698a-7b67-4afc-8dbe-a6a617d4ed5c", "Capo Italian Deli - Potomac - Cabin John - 7731 Tuckerman Lane"),
    ("48e19e54-5515-4f80-a414-c479b69c493f", "Casey's Coffee - College Park"),
    ("847b9501-2908-48df-8a65-e5e98a75fe3e", "CBRC LLC dba Chesapeake Coffee Roasters - 2100 Concord Boulevard"),
    ("db56c472-4b05-497a-86d6-632c17d852db", "Centrado Cafe Shop - 15530 Old Columbia Pike"),
    ("5d8b9264-fcff-4ab3-b49c-0d89be7ce985", "Coffee Land - 222 N Charles St # A"),
    ("7f820124-c597-41b0-9cb7-2d5159a50184", "Cove Cafe - 2600 Tower Oaks Blvd"),
    ("66d54bfe-f0d2-4e83-92f6-9f81fc8b165c", "Cube Coffee - 8492 Baltimore National Pike"),
    ("1bbd0189-7f80-432c-b88f-f33e9ee6680d", "David and Dad's Cafe - 115 N Charles St"),
    ("2cdb0293-1e1b-4bc9-b17e-84209f455ee3", "Filicori - 12430 Park Potomac Ave R-3"),
    ("7b890e64-9771-4344-ae84-cb3d03d4ecd9", "French Press - 4918 Saint Elmo Avenue"),
    ("098ef508-4ce4-4647-9b2d-fa5b5969cd21", "Ivy by the Lake - 46110 Lake Center Plaza"),
    ("966024dc-249b-4340-9c79-891c1b45a0df", "Jaliyaa Coffee - 5038 Oakmoore Dr"),
    ("d72506ae-c4b7-4241-8554-579be5adf412", "Java Nation - 11120 Rockville Pike"),
    ("64efba8c-fde3-4c2e-b272-2c815516ca94", "Jems Bottle & Cafe - 2200 East Fayette Street"),
    ("551e1c3a-69fc-4ec9-8ad1-7216e6d589bb", "Kneads Bakeshop - Canton - 3601 Boston St"),
    ("dace9b1c-3f61-4e93-a75c-7312ef992a43", "Kneads Bakeshop - Cross Keys - 6 Village Square"),
    ("afddcebe-179f-49f4-86d8-0643750a0781", "Kneads Bakeshop - Harbor East - 506 S Ctrl Ave"),
    ("a7746087-0d4b-49a3-8202-8523d14a2c1e", "Kyo Matcha - Columbia"),
    ("0f33d26d-264d-4e41-8096-99e90ff2bccf", "Kyotomatcha - 33 Maryland Avenue"),
    ("6efa988c-bcac-4198-ab11-b9771a1beb89", "Lavande Patisserie - 275 N Washington St"),
    ("619696e0-1b37-4639-8866-7d88cb188894", "Little Market Cafe - 3731 Hamilton Street"),
    ("e1186f80-fb5d-4cf9-896e-8a3d6b6a25c9", "Magothy roasting company - 8116 Forest Glen Drive"),
    ("7c3582a4-6f79-4363-a197-43902f3a7a68", "Market House, LLC - 25 Market Space"),
    ("ebcf259d-c6aa-4204-babc-73efc839cb3f", "Mehfil Cafe - 7 North Calvert Street"),
    ("68fe1e79-3dfa-4099-a032-81ceca71db0d", "merrit star pharmacy - 5022 Rome Red Way"),
    ("810cf95e-71ff-47b4-b45e-82d5feb4cb7b", "Mirabeau - 5751 Fishers Lane"),
    ("ecb938dd-12ef-4ace-89b6-f44bf5ba95ac", "Miskiri Hospitality Group - 2 East Wells St"),
    ("8e8a3c92-c77e-4624-aef2-cb65013839b4", "Mixt Food Hall - 3809 Rhode Island Ave"),
    ("7cfb5a6b-d049-4124-bf46-202a0284667f", "Morning Mugs - 15 West Hughes Street"),
    ("074c63cf-744f-4c82-a6e0-ad531181f481", "Morning Mugs Coffee - 15 West Hughes Street"),
    ("c8041d8f-4427-4624-923d-371da8cbc641", "Old Mill Cafe - Ellicott City"),
    ("1544bb63-e61d-422d-86c2-387ff379820e", "Order and Chaos Coffee - 1410 Key Hwy"),
    ("8d361f48-e28e-40cf-b29b-9bee280e6d11", "Others Coffee - 9922 Evergreen Avenue"),
    ("4b274a8c-e64c-4852-a53b-3c6f3239eecc", "PJ's Coffee - 4501 - Camp Springs MD"),
    ("efb7e2b9-1162-45c4-8dd3-3e44e010e6d4", "Quartermaine - 4972 Wyaconda Road"),
    ("c5a56314-b962-4957-8e08-516db05daf29", "Ragamuffins Coffee House - 385 Main St"),
    ("8261c545-198b-4cd4-9c52-49ce3fc10346", "ROCO Kitchen + Coffee - 6430 Freetown Road"),
    ("09331910-f859-423c-89f5-19ea55a99a92", "ruya juice bar cafe - 4606 Eastern Avenue"),
    ("3c1b6ee9-a019-4abb-bf4b-b0de9ecfc524", "Rodman's discount store - 5100 Wisconsin Avenue"),
    ("dd3c4d7c-267b-4897-bdba-ef99994a53d0", "Roggenart - Baltimore - 1001 North Charles"),
    ("badd9cf5-d195-4135-a05f-07cfcf34562a", "Roggenart - Catonsville 706 Frederick Road"),
    ("58a10284-52b8-41e7-9222-420ef7d352c3", "Roggenart - Columbia - 6476 Dobbin Center Way"),
    ("6006308b-6ac6-4784-ba1d-5c3bfb014086", "Roggenart - Savage - 8600 Foundry St #2091"),
    ("7182a5f1-3811-44ae-8344-138b881ab429", "Root City Kava Bar and Lounge - 312 Washington Ave"),
    ("fbcbd5a1-958d-4e90-840a-044dbb857916", "Sandy Pony Donuts - Annapolis"),
    ("5983b2f4-e51e-4c43-b1ab-90c401c715f6", "Sidamo Coffee and Tea - Fulton - 8180 Maple Lawn Blvd"),
    ("7a6d3a58-3118-4f4a-b72c-4134e17a12c0", "Simona Cafe - 4520 East-West Highway"),
    ("fa9f6ea7-5d6b-444f-ab65-53069ca01e26", "Sweeteria Bethesda - 7525 Old Georgetown Road"),
    ("b2ac9bdb-f07c-441f-8eb7-7d8793636cdb", "Takoma Beverage Company - 6917 Laurel Ave"),
    ("c53e6cb4-b398-4814-b7d1-fcbeec253903", "The Bean Bag Deli & Catering C - 1605 East Gude Drive"),
    ("59edebfc-d2d4-4ca7-b28c-a141e777812c", "The Fountain at Drug City - 2805 North Point Rd"),
    ("9e5225b4-ae06-445c-b0de-c81df1b931f6", "THE pearl - 10285 Little Patuxent Parkway"),
    ("b6d43d93-6f0a-4548-8a2c-3a802044354f", "The Soulfull Cafe - 50 Monroe Pl"),
    ("2b1dd9f2-bff1-4c6e-bc94-e241bee9e418", "Thread Coffee - 1812 Greenmount Avenue"),
    ("617c35f2-8993-4d99-bfd7-bccedd929234", "Trifecto Bar - 12250 Clarksville Pike suite a"),
    ("c63202f6-a198-4b24-8ebc-6066a4d59979", "two5eats inc. - Love Melts - 613 Emerson Place"),
    ("a7845c1d-1737-409e-a729-293c0a6df8c4", "Vent Coffee Roasters - 1700 W 41st St"),
    ("0ee21cde-237f-4f88-84a8-fe238d9ddabe", "Wild Bean Coffee - 1532 Rockville Pike"),
    ("572317a2-65be-45d2-bfe6-df85f05be314", "WildBay kombucha - 4820 Seton Drive"),
]

CUSTOMER_UUIDS = {u for u, _ in CUSTOMERS}
ROSTER_NAMES = dict(CUSTOMERS)

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)
_VEN_PREFIX = re.compile(r"^VEN\d+\s+")
_CASE = re.compile(r"case\s*\((\d+(?:\.\d+)?)x\)", re.I)


def norm_name(name):
    """Join key across sources: lower-cased, '(DUPLICATE) ' prefix stripped."""
    return _DUP.sub("", str(name or "").strip()).lower()


def vendor_key(name):
    """Vendor names appear with and without a 'VEN00001293 ' NetSuite prefix."""
    return _VEN_PREFIX.sub("", str(name or "").strip()).lower()


def case_size_of(purchase_unit):
    """Eaches per purchase unit: 'Case (6x)' -> 6, 'Each' -> 1."""
    if not purchase_unit:
        return None
    u = str(purchase_unit).strip()
    if u.lower() == "each":
        return 1.0
    m = _CASE.search(u)
    return float(m.group(1)) if m else None


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


def find_mdt1_file(drive):
    """Newest MDT1 sales export in the Looker folder."""
    q = (
        f"'{LOOKER_FOLDER_ID}' in parents and trashed=false "
        f"and name contains 'Network Sales Tracker - MDT1'"
    )
    res = (
        drive.files()
        .list(q=q, orderBy="modifiedTime desc", pageSize=5,
              fields="files(id,name,modifiedTime)")
        .execute()
    )
    files = res.get("files", [])
    if not files:
        sys.exit("MDT1 sales export not found in the Looker Data Dumps folder")
    return files[0]["id"]


def scan_sales(rows, days_hint=None):
    """Aggregate cohort order lines by SKU.

    Both feeds share the network sales tracker schema, so one scanner serves
    the October baseline and the current window. Actual sold units =
    SO Item Qty / Conversion Rate, matching the tracker convention.

    Returns (skus, customers, dates) where skus maps norm_name -> aggregate.
    """
    if not rows:
        return {}, {}, []
    header = rows[0]
    col = {n: i for i, n in enumerate(header)}
    need = ("Item Name", "Odeko Account Uuid", "SO Item Qty", "Conversion Rate")
    if any(col.get(n) is None for n in need):
        return {}, {}, []
    i_item, i_uuid = col["Item Name"], col["Odeko Account Uuid"]
    i_qty, i_conv = col["SO Item Qty"], col["Conversion Rate"]
    i_brand, i_date = col.get("Brand Name"), col.get("Date Date")
    i_cust, i_iuuid = col.get("Customer Name"), col.get("Item Uuid")

    skus = {}
    custs = {}
    dates = []
    for r in rows[1:]:
        if len(r) <= i_uuid or r[i_uuid] not in CUSTOMER_UUIDS:
            continue
        name = r[i_item] if len(r) > i_item else ""
        if not name:
            continue
        uuid = r[i_uuid]
        key = norm_name(name)
        if i_date is not None and len(r) > i_date and r[i_date]:
            dates.append(r[i_date])

        qty = parse_num(r[i_qty]) if len(r) > i_qty else None
        conv = parse_num(r[i_conv]) if len(r) > i_conv else None
        units = qty / conv if (qty is not None and conv and conv > 0) else 0.0

        s = skus.get(key)
        if s is None:
            s = skus[key] = {
                "name": name,
                "brand": (r[i_brand] if i_brand is not None and len(r) > i_brand else "") or "",
                "itemUuid": (r[i_iuuid] if i_iuuid is not None and len(r) > i_iuuid else "") or "",
                "units": 0.0,
                "lines": 0,
                "custs": set(),
            }
        s["units"] += units
        s["lines"] += 1
        s["custs"].add(uuid)

        c = custs.get(uuid)
        if c is None:
            name_seen = (r[i_cust] if i_cust is not None and len(r) > i_cust else "") or ""
            c = custs[uuid] = {"name": name_seen, "units": 0.0, "skus": set()}
        c["units"] += units
        c["skus"].add(key)
    return skus, custs, dates


def load_onboarding_skus():
    """Normalised names of every SKU the bring-in plan covers.

    Includes both the cohort's original SKUs and the format targets they were
    retargeted onto (Monin -> glass, Torani -> 1L plastic), so a SKU counts as
    "already being brought on" whichever pack the feed reports.
    """
    import csv

    names = set()
    try:
        with open(ONBOARDING_COHORT, newline="") as f:
            for row in csv.DictReader(f):
                item = (row.get("Item") or "").strip()
                if item:
                    names.add(norm_name(item))
    except OSError:
        return names
    try:
        with open(ONBOARDING_DATA) as f:
            for it in json.load(f).get("items", []):
                for k in ("name", "target"):
                    if it.get(k):
                        names.add(norm_name(it[k]))
    except (OSError, ValueError):
        pass
    return names


def build_carried_set(svc):
    """norm_name -> {source, ...} for everything DCA1 already has.

    Three signals unioned so a SKU DCA1 stocks is never reported as a gap:
    flagged in_catalog, sitting in on-hand inventory, or sold in the
    trailing 90 days.
    """
    carried = defaultdict(set)

    def add(name, source):
        k = norm_name(name)
        if k:
            carried[k].add(source)

    def truthy(v):
        return str(v).strip().upper() in ("TRUE", "1", "YES", "Y")

    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni, ci = col.get("warehouse_name"), col.get("item_name"), col.get("in_catalog")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ci is not None and len(r) > ci and truthy(r[ci]):
                    if ni is not None and len(r) > ni:
                        add(r[ni], "catalog")

    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        wi, ni = col.get("Warehouse Name"), col.get("Item Name")
        for r in rows[1:]:
            if wi is not None and len(r) > wi and r[wi] == WAREHOUSE:
                if ni is not None and len(r) > ni:
                    add(r[ni], "onhand")

    rows = get_values(svc, DCA1_SOLD_SPREADSHEET_ID, DCA1_SOLD_RANGE)
    if rows:
        col = {n: i for i, n in enumerate(rows[0])}
        ni = col.get("Item Name")
        for r in rows[1:]:
            if ni is not None and len(r) > ni:
                add(r[ni], "sold90")

    return carried


def load_item_refs(svc, wanted):
    """item name -> {uuid, vendor, purchaseUnit} from the network PO feed.

    The PO export is the only source that carries item UUIDs for SKUs DCA1
    doesn't stock yet, which is most of a seasonal gap list. On Hand & ETA
    fills in the procurement vendor where no PO exists.
    """
    refs = {}
    rows = get_values(svc, PO_SPREADSHEET_ID, PO_RANGE)
    if rows:
        header = rows[0]
        for r in rows[1:]:
            g = row_getter(header, r)
            key = norm_name(g("Item Name"))
            if key not in wanted:
                continue
            ref = refs.setdefault(key, {"uuid": "", "vendor": "", "purchaseUnit": ""})
            ref["uuid"] = ref["uuid"] or g("Item Uuid")
            ref["vendor"] = ref["vendor"] or g("Full Vendor Name")
            ref["purchaseUnit"] = ref["purchaseUnit"] or g("Purchase Unit Name")

    rows = get_values(svc, ONHAND_SPREADSHEET_ID, ONHAND_RANGE)
    if rows:
        header = rows[0]
        for r in rows[1:]:
            g = row_getter(header, r)
            key = norm_name(g("Item Name"))
            if key not in wanted:
                continue
            ref = refs.setdefault(key, {"uuid": "", "vendor": "", "purchaseUnit": ""})
            ref["uuid"] = ref["uuid"] or g("Item Extid")
            ref["vendor"] = ref["vendor"] or g("Procurement Vendor")
    return refs


def load_conversion_rates(svc, sales_id):
    """item -> eaches per sales unit, from the MDT1 sales feed.

    Units here are *sales* units, and the sales unit isn't always an each.
    Most items sell by the each (rate 1), but some sell by the case — Pacific
    Barista Almond Milk has rate 12 against a Case (12x) purchase unit, so its
    demand is already in cases and dividing by the case pack again would
    under-order it twelvefold. Ordering therefore needs
    purchase_units = sales_units * rate / eaches_per_purchase_unit.

    Only two columns are fetched; the feed is hundreds of thousands of rows.
    """
    try:
        res = (
            svc.spreadsheets()
            .values()
            .batchGet(spreadsheetId=sales_id, ranges=["B:B", "J:J"])
            .execute()
        )
    except Exception:
        return {}
    ranges = res.get("valueRanges", [])
    if len(ranges) < 2:
        return {}
    names = [r[0] if r else "" for r in ranges[0].get("values", [])]
    rates = [r[0] if r else "" for r in ranges[1].get("values", [])]
    votes = defaultdict(Counter)
    for name, rate in zip(names[1:], rates[1:]):  # skip headers
        v = parse_num(rate)
        if name and v and v > 0:
            votes[norm_name(name)][v] += 1
    return {k: c.most_common(1)[0][0] for k, c in votes.items()}


def load_upload_refs(svc):
    """NetSuite identifiers the bulk PO upload tool requires.

    Returns (warehouse, vendor_uuids) where warehouse is the DCA1 location
    id / uuid pair. Read from the newest dated tab of the DCA1 ordering model.
    """
    warehouse = {"locationId": "", "uuid": ""}
    vendor_uuids = {}
    try:
        meta = svc.spreadsheets().get(spreadsheetId=DCA1_MODEL_SPREADSHEET_ID).execute()
    except Exception:
        return warehouse, vendor_uuids
    dated = [
        s["properties"]["title"] for s in meta.get("sheets", [])
        if re.match(r"^[A-Z][a-z]{2} \d{2}-\d{2}$", s["properties"]["title"])
    ]
    for tab in dated[:3]:  # fall back a couple of days if the newest is empty
        rows = get_values(svc, DCA1_MODEL_SPREADSHEET_ID, f"'{tab}'!A1:CF")
        if not rows:
            continue
        header = rows[0]
        for r in rows[1:]:
            g = row_getter(header, r)
            if not warehouse["uuid"] and g("warehouse_uuid"):
                warehouse["uuid"] = g("warehouse_uuid")
                warehouse["locationId"] = g("warehouse_location_id")
            vn, vu = g("vendor_name"), g("procurement_vendor_uuid")
            if vn and vu:
                vendor_uuids.setdefault(vendor_key(vn), vu)
        if vendor_uuids:
            break
    return warehouse, vendor_uuids


def load_network_vendor_uuids(svc):
    """vendor_key -> procurement vendor UUID, across every warehouse.

    Vendor UUIDs are universal, so a vendor DCA1 has never bought from still
    has one wherever else in the network it is used. The V2 model carries
    vendor_uuid + vendor_id but no vendor name, and the combined models dump
    carries vendor_id + vendor_name, so the two are joined on vendor_id.
    """
    uuid_by_vid = defaultdict(Counter)
    rows = get_values(svc, V2_SPREADSHEET_ID, V2_RANGE)
    if rows:
        header = rows[0]
        for r in rows[1:]:
            g = row_getter(header, r)
            vid, vu = str(g("vendor_id")).strip(), g("vendor_uuid")
            if vid and vu:
                uuid_by_vid[vid][vu] += 1
    if not uuid_by_vid:
        return {}

    out = {}
    rows = get_values(svc, MODELS_SPREADSHEET_ID, MODELS_RANGE)
    if rows:
        header = rows[0]
        for r in rows[1:]:
            g = row_getter(header, r)
            vid, name = str(g("vendor_id")).strip(), g("vendor_name")
            if not vid or not name or vid not in uuid_by_vid:
                continue
            out.setdefault(vendor_key(name), uuid_by_vid[vid].most_common(1)[0][0])
    return out


def _existing_has_current(out_path):
    """True when a previous build already captured current-window demand."""
    try:
        with open(out_path) as f:
            return bool(json.load(f).get("currentAvailable"))
    except (OSError, ValueError):
        return False


def day_span(dates):
    """Inclusive day count covered by a list of YYYY-MM-DD strings."""
    parsed = []
    for d in dates:
        try:
            parsed.append(datetime.strptime(str(d)[:10], "%Y-%m-%d"))
        except ValueError:
            continue
    if not parsed:
        return None, None, None
    lo, hi = min(parsed), max(parsed)
    return lo.date().isoformat(), hi.date().isoformat(), (hi - lo).days + 1


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---- the two demand windows -------------------------------------------
    oct_rows = get_values(svc, LASTYEAR_SPREADSHEET_ID, LASTYEAR_RANGE)
    oct_skus, oct_custs, oct_dates = scan_sales(oct_rows)

    sales_id = find_mdt1_file(drive)
    cur_rows = get_values(svc, sales_id, "A1:M")
    cur_skus, cur_custs, cur_dates = scan_sales(cur_rows)

    # Guard: the Looker exports are periodically cleared and rewritten, and the
    # MDT1 one has sat empty for days at a time. The October baseline is this
    # dashboard's spine, so an empty read there is fatal — never overwrite a
    # good data.json with an empty snapshot.
    if not oct_skus:
        sys.exit(
            "Last-October sheet has no rows for the converting cohort "
            f"(read {max(len(oct_rows) - 1, 0)} data rows) — source likely "
            "mid-refresh; leaving existing data.json untouched."
        )
    # The current window only enriches the comparison, so a missing one
    # degrades rather than fails: the gap classification (carried / planned /
    # neither) does not depend on it. But never regress a file that already
    # has current demand back to a snapshot without it.
    if not cur_skus:
        if _existing_has_current(out_path):
            sys.exit(
                "MDT1 sales sheet has no rows for the converting cohort "
                f"(read {max(len(cur_rows) - 1, 0)} data rows) and the existing "
                "data.json has current demand — source likely mid-refresh; "
                "leaving it untouched."
            )
        print(
            "WARNING: MDT1 sales sheet returned no cohort rows — building the "
            "October baseline without the current-demand comparison. The next "
            "refresh will fill it in once the export repopulates.",
            file=sys.stderr,
        )

    oct_min, oct_max, _ = day_span(oct_dates)
    cur_min, cur_max, cur_days = day_span(cur_dates)
    cur_days = cur_days or 90
    # October is 31 days; scale current demand to the same length so the
    # comparison isn't just an artefact of the wider trailing window.
    scale = OCTOBER_DAYS / cur_days

    carried = build_carried_set(svc)
    onboarding = load_onboarding_skus()
    refs = load_item_refs(svc, set(oct_skus))
    rates = load_conversion_rates(svc, sales_id)
    warehouse, dca1_vendor_uuids = load_upload_refs(svc)
    network_vendor_uuids = load_network_vendor_uuids(svc)

    # ---- per-SKU comparison ------------------------------------------------
    items = []
    for key, s in oct_skus.items():
        cur = cur_skus.get(key)
        cur_units = round(cur["units"], 1) if cur else 0.0
        cur_custs_n = len(cur["custs"]) if cur else 0
        expected = cur_units * scale          # current run-rate over 31 days
        oct_units = round(s["units"], 1)

        in_dca1 = key in carried
        in_plan = key in onboarding
        status = "carried" if in_dca1 else ("planned" if in_plan else "gap")

        ref = refs.get(key, {})
        vendor = ref.get("vendor", "") or ""
        vkey = vendor_key(vendor)
        vendor_uuid = dca1_vendor_uuids.get(vkey) or network_vendor_uuids.get(vkey, "")
        case_size = case_size_of(ref.get("purchaseUnit"))
        rate = rates.get(key)
        # purchase_units = sales_units * eaches_per_sales_unit / eaches_per_case
        cases = None
        if case_size and case_size > 0:
            eaches = oct_units * (rate if rate and rate > 0 else 1.0)
            cases = int(-(-eaches // case_size))  # ceil

        items.append({
            "key": key,
            "name": s["name"],
            "brand": s["brand"],
            "itemUuid": s["itemUuid"] or ref.get("uuid", ""),
            "status": status,
            "inDca1": in_dca1,
            "dca1Sources": sorted(carried.get(key, [])),
            "inPlan": in_plan,
            "octUnits": oct_units,
            "octCustomers": len(s["custs"]),
            "octLines": s["lines"],
            "curUnits": cur_units,
            "curCustomers": cur_custs_n,
            "expectedUnits": round(expected, 1),
            # >1 means last October ran hotter than today's rate — the bigger
            # the number, the more the trailing window understates the month.
            "lift": round(oct_units / expected, 2) if expected > 0 else None,
            # Unknowable without the current window: absent is not the same
            # as zero, so leave it null rather than flagging every SKU.
            "onlyLastYear": (cur is None) if cur_skus else None,
            "vendorName": vendor,
            "vendorUuid": vendor_uuid,
            "vendorKnownToDca1": bool(dca1_vendor_uuids.get(vkey)),
            "purchaseUnit": ref.get("purchaseUnit", ""),
            "caseSize": case_size,
            "salesConversion": rate,
            "cases": cases,
        })

    items.sort(key=lambda x: -x["octUnits"])

    gaps = [x for x in items if x["status"] == "gap"]
    planned = [x for x in items if x["status"] == "planned"]
    covered = [x for x in items if x["status"] == "carried"]

    # ---- customers ---------------------------------------------------------
    customers = []
    for uuid in sorted(set(oct_custs) | set(cur_custs)):
        o = oct_custs.get(uuid)
        c = cur_custs.get(uuid)
        gap_keys = {x["key"] for x in gaps}
        customers.append({
            "uuid": uuid,
            "name": (o or {}).get("name") or (c or {}).get("name")
                    or ROSTER_NAMES.get(uuid, ""),
            "octUnits": round(o["units"], 1) if o else 0.0,
            "octSkus": len(o["skus"]) if o else 0,
            "octGapSkus": len(o["skus"] & gap_keys) if o else 0,
            "curUnits": round(c["units"], 1) if c else 0.0,
            "curSkus": len(c["skus"]) if c else 0,
        })
    customers.sort(key=lambda c: -c["octUnits"])

    top_gaps = [
        {"name": x["name"], "units": x["octUnits"], "customers": x["octCustomers"]}
        for x in gaps[:20]
    ]
    brand_units = defaultdict(float)
    for x in gaps:
        brand_units[x["brand"] or "—"] += x["octUnits"]
    top_gap_brands = [
        {"brand": b, "units": round(u, 1)}
        for b, u in sorted(brand_units.items(), key=lambda kv: -kv[1])[:12]
    ]

    gap_cases = sum(x["cases"] or 0 for x in gaps)
    # A gap can only reach a PO if we know both its case pack and its vendor
    # UUID. Count the two blockers separately — they have different fixes.
    needs_vendor = [x for x in gaps if not x["vendorUuid"]]
    no_case_pack = [x for x in gaps if not x["cases"]]
    orderable = [x for x in gaps if x["cases"] and x["vendorUuid"]]

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "targetWarehouse": WAREHOUSE,
        "sourceWarehouse": "MDT1",
        "baselineRange": {"min": oct_min, "max": oct_max, "days": OCTOBER_DAYS},
        "currentRange": {"min": cur_min, "max": cur_max, "days": cur_days},
        # False when the MDT1 export was empty at build time: the October
        # baseline still stands, but every current-demand figure is absent
        # rather than zero, and the UI hides the comparison columns.
        "currentAvailable": bool(cur_skus),
        "warehouseLocationId": warehouse["locationId"],
        "warehouseUuid": warehouse["uuid"],
        "summary": {
            "cohortSize": len(CUSTOMERS),
            "octCustomers": len(oct_custs),
            "curCustomers": len(cur_custs),
            "octSkus": len(items),
            "octUnits": round(sum(x["octUnits"] for x in items), 1),
            "skusCarried": len(covered),
            "skusPlanned": len(planned),
            "skusGap": len(gaps),
            "gapUnits": round(sum(x["octUnits"] for x in gaps), 1),
            "gapCases": gap_cases,
            "gapNeedsVendor": len(needs_vendor),
            "gapNoCasePack": len(no_case_pack),
            "gapOrderable": len(orderable),
            "onlyLastYear": sum(1 for x in items if x["onlyLastYear"]) if cur_skus else None,
            "gapOnlyLastYear": sum(1 for x in gaps if x["onlyLastYear"]) if cur_skus else None,
            "coveragePct": round(100 * len(covered) / len(items)) if items else 0,
        },
        "coverage": [
            {"status": "Carried in DCA1", "count": len(covered)},
            {"status": "In the bring-in plan", "count": len(planned)},
            {"status": "Neither (seasonal gap)", "count": len(gaps)},
        ],
        "topGaps": top_gaps,
        "topGapBrands": top_gap_brands,
        "items": items,
        "customers": customers,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {len(items)} SKUs ({len(gaps)} seasonal gaps, {gap_cases} cases) "
        f"across {len(oct_custs)} customers to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
