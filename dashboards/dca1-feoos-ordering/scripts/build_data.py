#!/usr/bin/env python3
"""Build data.json for the DCA1 FEOOS & post-conversion ordering dashboard.

Two halves:

  1. DCA1 fill errors / out-of-stocks (FEOOS) for the market as a whole —
     what customers asked for that DCA1 could not ship.
  2. The converted MDT1 cohort inside that market — what they are actually
     ordering from DCA1 now, which FEOOS SKUs they depend on, and which SKUs
     they have started buying that the transition never tracked.

A note on FEOOS sources, because the two exports disagree sharply:

  - "DCA1 Trailing 7 Day FEOOS.csv" reports ~105 DCA1 events per week. It has
    no Location Name column, so it supports SKU-level analysis only.
  - "Trailing 14 FEOOS.csv" (Looker look 1593) is network-wide and does carry
    Location Name, but reports only 7 DCA1 events in 14 days — and every DCA1
    item in it also appears in the DCA1-only export, so it is a strict subset,
    not a different population. None of the converted cohort appears in it.

The DCA1-only export therefore drives every DCA1 number here, and the cohort
drill-down is done at SKU level. The network file is still read, purely to
report its DCA1 count alongside so the discrepancy stays visible rather than
being quietly resolved in one direction. Customer-level FEOOS becomes possible
the moment the DCA1 export carries Location Name (or look 1593 stops dropping
DCA1); `locationLevel` in the output says whether that has happened.

Sources (Looker Data Dumps folder):
  - DCA1 FEOOS:    "DCA1 Trailing 7 Day FEOOS.csv"
  - Network FEOOS: "Trailing 14 FEOOS.csv"  (cross-check + location names)
  - DCA1 sales:    "DCA1 Sales Tracker Trailing 90.csv" (has customer UUIDs)
  - Transition universe and bring-in plan are read from the sibling dashboards
    in this repo, so the three can't drift apart.

Actual sold units = SO Item Qty / Conversion Rate, matching the tracker
convention used across these dashboards.
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

WAREHOUSE = "DCA1"

DCA1_FEOOS_SPREADSHEET_ID = "1EfIbKfeZGFXf6NjYyDDKjd4N6fBhBW1nlm5SjOmKgyk"
DCA1_FEOOS_RANGE = "'DCA1 Trailing 7 Day FEOOS.csv'!A1:G"
NET_FEOOS_SPREADSHEET_ID = "1tNBL8WXowviHwF5ywOaYl0xkUQCnr9c56Idmfx7kf8Y"
NET_FEOOS_RANGE = "'Trailing 14 FEOOS.csv'!A1:L"
DCA1_SALES_SPREADSHEET_ID = "18i2x-8TSifmNeEZldpIH9_Y29jJ5aJNgxvNsxtZeWSs"
DCA1_SALES_RANGE = "'DCA1 Sales Tracker Trailing 90.csv'!A1:K"

_HERE = os.path.dirname(os.path.abspath(__file__))
CONVERSION_DATA = os.path.join(_HERE, "..", "..", "mdt1-dca1-conversion", "data.json")
ONBOARDING_COHORT = os.path.join(_HERE, "..", "..", "mdt1-sku-onboarding", "scripts", "cohort.csv")
ONBOARDING_DATA = os.path.join(_HERE, "..", "..", "mdt1-sku-onboarding", "data.json")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)

# Charge lines that ride the sales feed as if they were items. They are not
# stocked, can never be a fill error, and would otherwise pad the SKU counts.
CHARGE_LINES = {"delivery fee", "fuel surcharge"}


def norm_name(name):
    """Join key across sources: lower-cased, '(DUPLICATE) ' prefix stripped."""
    return _DUP.sub("", str(name or "").strip()).lower()


def loc_key(name):
    """Loose key for customer/location names: the feeds separate the site from
    the address with ' : ' in sales and ' - ' in FEOOS, and casing varies."""
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


# ---------------------------------------------------------------------------
# Pack-format mapping, carried over from the transition work.
#
# DCA1 deliberately stocks Monin in 750 ml glass where MDT1 sold 1 L plastic,
# and Torani in 1 L plastic. Matching SKUs on their exact name would treat the
# two packs of one syrup as unrelated products — so a customer who switched
# packs at conversion would look like brand-new demand, and a fill error on the
# glass bottle would not flag the plastic one. fmt_key strips size and
# container words so both packs collapse to one product key, and the format
# helpers then say which pack a given line actually is.
# ---------------------------------------------------------------------------

# Brand -> the pack DCA1 would rather carry. size None means "any size in that
# material".
PREFERRED_FORMAT = {
    "monin": {"material": "glass", "size": None, "label": "glass"},
    "torani": {"material": "plastic", "size": "1l", "label": "1L plastic"},
}

# Same product, named differently enough that the flavour key alone would not
# collapse the packs.
FLAVOUR_ALIASES = {
    "monin chai tea concentrate": "monin chai tea",
    "torani sugar free classic caramel syrup with splenda":
        "torani sugar free classic caramel syrup",
}

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
_SIZE_ONE = re.compile(
    r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+))?\s*"
    r"(ml|l|liter|litre|gallon|gal|fl\s*oz|oz|qt|quart|lb|lbs|kg|g)\b"
)
_UNIT_CANON = {"liter": "l", "litre": "l", "gallon": "gal", "quart": "qt",
               "floz": "oz", "lbs": "lb"}
_UNIT_DISP = {"ml": "ml", "l": "L", "gal": "gal", "oz": "oz", "qt": "qt",
              "lb": "lb", "kg": "kg", "g": "g"}


def fmt_key(name):
    """Format-agnostic product key: same syrup, any pack, one key."""
    s = norm_name(name).replace("bottle(s)", " ")
    s = _SIZE.sub(" ", s)
    s = _UNITWORD.sub(" ", s)
    s = _CONT.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _WS.sub(" ", s).strip()
    s = FLAVOUR_ALIASES.get(s, s)
    # Token-sort so word order can't split a pack pair: the same syrup is
    # listed as "Orange Syrup Dairy Friendly" in glass and "Orange Dairy
    # Friendly Syrup" in plastic. Sizes and containers are already stripped,
    # so this tolerates reordering, not different products.
    return " ".join(sorted(s.split()))


def canon_size(name):
    """Canonical volume token, e.g. '1l', '750ml'. '' if the pack has none."""
    m = _SIZE_ONE.search(norm_name(name))
    if not m:
        return ""
    num = m.group(1) + (("/" + m.group(2)) if m.group(2) else "")
    unit = m.group(3).replace(" ", "")
    return num + _UNIT_CANON.get(unit, unit)


def pack_material(name):
    s = norm_name(name)
    for mat in ("glass", "plastic"):
        if re.search(r"\b" + mat + r"\b", s):
            return mat
    return ""


def pack_label(name):
    """Human pack descriptor, e.g. '1 L plastic', '750 ml glass'."""
    m = _SIZE_ONE.search(norm_name(name))
    size_disp = ""
    if m:
        num = m.group(1) + ((" / " + m.group(2)) if m.group(2) else "")
        unit = m.group(3).replace(" ", "")
        unit = _UNIT_CANON.get(unit, unit)
        size_disp = num + " " + _UNIT_DISP.get(unit, unit)
    return " ".join(p for p in (size_disp, pack_material(name)) if p)


def format_status(name, brand):
    """Is this line in the pack DCA1 chose to carry for its brand?

    Returns "preferred", "off-format", or "n/a". Items with no glass/plastic
    token are n/a rather than off-format — a 64 oz Monin sauce has no pack
    material to be wrong about, and flagging it would be noise.
    """
    pref = PREFERRED_FORMAT.get(str(brand or "").strip().lower())
    if not pref:
        return "n/a"
    mat = pack_material(name)
    if not mat:
        return "n/a"
    if mat != pref["material"]:
        return "off-format"
    if pref["size"] and canon_size(name) != pref["size"]:
        return "off-format"
    return "preferred"


def parse_num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def get_values(svc, sid, rng):
    return (
        svc.spreadsheets().values().get(spreadsheetId=sid, range=rng)
        .execute().get("values", [])
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


def day_span(dates):
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


CONVERTED_CUSTOMERS = [
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
    ("3c1b6ee9-a019-4abb-bf4b-b0de9ecfc524", "Rodman's discount store - 5100 Wisconsin Avenue"),
    ("dd3c4d7c-267b-4897-bdba-ef99994a53d0", "Roggenart - Baltimore - 1001 North Charles"),
    ("badd9cf5-d195-4135-a05f-07cfcf34562a", "Roggenart - Catonsville 706 Frederick Road"),
    ("58a10284-52b8-41e7-9222-420ef7d352c3", "Roggenart - Columbia - 6476 Dobbin Center Way"),
    ("6006308b-6ac6-4784-ba1d-5c3bfb014086", "Roggenart - Savage - 8600 Foundry St #2091"),
    ("7182a5f1-3811-44ae-8344-138b881ab429", "Root City Kava Bar and Lounge - 312 Washington Ave"),
    ("09331910-f859-423c-89f5-19ea55a99a92", "ruya juice bar cafe - 4606 Eastern Avenue"),
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

COHORT_UUIDS = {u for u, _ in CONVERTED_CUSTOMERS}
ROSTER_NAMES = dict(CONVERTED_CUSTOMERS)


def load_transition_universe():
    """Every SKU the MDT1 -> DCA1 conversion dashboard tracked, and the brand
    it recorded for each.

    The SKU set is the yardstick for "was this SKU part of the transition?" —
    it covers everything the cohort bought at MDT1, carried or gap alike. The
    brands are a fallback: the DCA1 sales export leaves Brand Name blank on
    99% of the cohort's rows (their orders are only days old and the brand
    dimension has not caught up), against 9% of DCA1 rows overall.
    """
    try:
        with open(CONVERSION_DATA) as f:
            skus = json.load(f).get("skus", [])
    except (OSError, ValueError, KeyError):
        return set(), {}, {}
    universe = {norm_name(s["name"]) for s in skus}
    # Also key by product, so ordering the glass pack of a syrup the cohort
    # bought in plastic at MDT1 counts as the same transition SKU, not new.
    universe |= {fmt_key(s["name"]) for s in skus}
    brands = {norm_name(s["name"]): s["brand"] for s in skus if s.get("brand")}
    for s_ in skus:
        if s_.get("brand"):
            brands.setdefault(fmt_key(s_["name"]), s_["brand"])
    # MDT1-era packs per product, to show what each one converted from.
    mdt1_packs = defaultdict(lambda: defaultdict(float))
    for s_ in skus:
        lbl = pack_label(s_["name"])
        if lbl:
            mdt1_packs[fmt_key(s_["name"])][lbl] += s_.get("units", 0) or 0
    return universe, brands, {k: dict(v) for k, v in mdt1_packs.items()}


def load_plan():
    """SKUs the MDT1 SKU onboarding tracker set out to bring on, including the
    format targets they were retargeted onto."""
    names = set()
    try:
        with open(ONBOARDING_COHORT, newline="") as f:
            for row in csv.DictReader(f):
                item = (row.get("Item") or "").strip()
                if item:
                    names.add(norm_name(item))
                    names.add(fmt_key(item))
    except OSError:
        pass
    try:
        with open(ONBOARDING_DATA) as f:
            for it in json.load(f).get("items", []):
                for k in ("name", "target"):
                    if it.get(k):
                        names.add(norm_name(it[k]))
                        names.add(fmt_key(it[k]))
    except (OSError, ValueError):
        pass
    return names


def load_dca1_feoos(svc):
    """DCA1 fill errors / out-of-stocks, per SKU and per day."""
    rows = get_values(svc, DCA1_FEOOS_SPREADSHEET_ID, DCA1_FEOOS_RANGE)
    skus, by_day, dates = {}, defaultdict(lambda: {"events": 0, "units": 0.0}), []
    if not rows:
        return skus, by_day, dates, 0
    header = rows[0]
    events = 0
    for r in rows[1:]:
        g = row_getter(header, r)
        name = g("Item Name")
        if not name:
            continue
        events += 1
        key = norm_name(name)
        qty = parse_num(g("Quantity of FEOOS Items Requested in Sales Units")) or 0.0
        day = str(g("FEOOS Event Date"))[:10]
        if day:
            dates.append(day)
            by_day[day]["events"] += 1
            by_day[day]["units"] += qty
        s = skus.get(key)
        if s is None:
            s = skus[key] = {
                "name": name, "brand": g("Brand Name") or "",
                "events": 0, "units": 0.0, "days": set(),
            }
        s["events"] += 1
        s["units"] += qty
        if day:
            s["days"].add(day)
    return skus, by_day, dates, events


def load_network_feoos(svc):
    """The network-wide FEOOS look, read only to cross-check DCA1 and to pick
    up location names if DCA1 rows ever appear there in volume.

    Returns (dca1_events, dca1_locations, cohort_events) where cohort_events
    maps a roster customer name to its FEOOS rows. Today all three are close
    to empty for DCA1; the dashboard reports that rather than hiding it.
    """
    rows = get_values(svc, NET_FEOOS_SPREADSHEET_ID, NET_FEOOS_RANGE)
    dca1_events, dca1_locations, cohort = 0, set(), defaultdict(list)
    if not rows:
        return dca1_events, dca1_locations, cohort
    header = rows[0]
    roster_by_key = {loc_key(n): n for n in ROSTER_NAMES.values()}
    for r in rows[1:]:
        g = row_getter(header, r)
        if g("Warehouse") != WAREHOUSE:
            continue
        dca1_events += 1
        loc = g("Location Name")
        if loc:
            dca1_locations.add(loc)
        hit = roster_by_key.get(loc_key(loc))
        if hit:
            cohort[hit].append({
                "item": g("Item Name"),
                "brand": g("Brand Name"),
                "date": str(g("FEOOS Event Date"))[:10],
                "units": parse_num(g("Quantity of FEOOS Items Requested in Sales Units")) or 0.0,
            })
    return dca1_events, dca1_locations, cohort


def load_dca1_sales(svc):
    """DCA1 sales, trailing 90, split into the converted cohort and the rest.

    Returns (cohort_skus, cohort_custs, dates, market_customers).
    """
    rows = get_values(svc, DCA1_SALES_SPREADSHEET_ID, DCA1_SALES_RANGE)
    skus, custs, dates, market = {}, {}, [], set()
    charges = 0
    if not rows:
        return skus, custs, dates, market, charges
    header = rows[0]
    for r in rows[1:]:
        g = row_getter(header, r)
        uuid = g("Odeko Account Uuid")
        name = g("Item Name")
        if not uuid or not name:
            continue
        market.add(uuid)
        if norm_name(name) in CHARGE_LINES:
            charges += 1
            continue
        if uuid not in COHORT_UUIDS:
            continue
        qty, conv = parse_num(g("SO Item Qty")), parse_num(g("Conversion Rate"))
        units = qty / conv if (qty is not None and conv and conv > 0) else 0.0
        day = str(g("Date Date"))[:10]
        if day:
            dates.append(day)

        key = norm_name(name)
        s = skus.get(key)
        if s is None:
            s = skus[key] = {
                "name": name, "brand": g("Brand Name") or "",
                "units": 0.0, "lines": 0, "custs": set(), "days": set(),
            }
        s["units"] += units
        s["lines"] += 1
        s["custs"].add(uuid)
        if day:
            s["days"].add(day)

        c = custs.get(uuid)
        if c is None:
            c = custs[uuid] = {
                "name": g("Customer Name") or ROSTER_NAMES.get(uuid, ""),
                "units": 0.0, "lines": 0, "skus": set(), "days": [],
            }
        c["units"] += units
        c["lines"] += 1
        c["skus"].add(key)
        if day:
            c["days"].append(day)
    return skus, custs, dates, market, charges


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    feoos, feoos_by_day, feoos_dates, feoos_events = load_dca1_feoos(svc)
    net_dca1_events, net_dca1_locations, net_cohort = load_network_feoos(svc)
    sales_skus, sales_custs, sales_dates, market_custs, charge_lines = load_dca1_sales(svc)

    # Guard: these Looker exports are periodically cleared and rewritten. Never
    # overwrite a good data.json with an empty snapshot.
    if not feoos and not sales_skus:
        sys.exit(
            "Both the DCA1 FEOOS and DCA1 sales exports came back empty — "
            "sources likely mid-refresh; leaving existing data.json untouched."
        )

    transition, transition_brands, mdt1_packs = load_transition_universe()
    plan = load_plan()
    cohort_keys = set(sales_skus)
    # A fill error on the glass pack matters to a customer ordering plastic,
    # so the FEOOS <-> cohort join is on the product, not the exact pack.
    cohort_products = {fmt_key(v["name"]) for v in sales_skus.values()}

    # Fill the blank brands the sales export leaves on freshly-converted
    # accounts, preferring the FEOOS export (same warehouse, same week) and
    # falling back to what the conversion dashboard recorded at MDT1.
    feoos_brands = {k: v["brand"] for k, v in feoos.items() if v.get("brand")}
    for k, v in list(feoos.items()):
        if v.get("brand"):
            feoos_brands.setdefault(fmt_key(v["name"]), v["brand"])
    for key, s_ in sales_skus.items():
        if not s_["brand"]:
            fk = fmt_key(s_["name"])
            s_["brand"] = (feoos_brands.get(key) or feoos_brands.get(fk)
                           or transition_brands.get(key)
                           or transition_brands.get(fk, ""))

    # ---- FEOOS, with cohort relevance ------------------------------------
    feoos_list = []
    for key, s in feoos.items():
        days = sorted(s.pop("days"))
        feoos_list.append({
            "name": s["name"], "brand": s["brand"],
            "events": s["events"], "units": round(s["units"], 1),
            "firstEvent": days[0] if days else None,
            "lastEvent": days[-1] if days else None,
            "daysAffected": len(days),
            # Does this shortfall touch the converted cohort?
            "cohortOrders": key in cohort_keys or fmt_key(s["name"]) in cohort_products,
            "inTransition": key in transition or fmt_key(s["name"]) in transition,
            "inPlan": key in plan or fmt_key(s["name"]) in plan,
            "pack": pack_label(s["name"]),
            "formatStatus": format_status(s["name"], s["brand"]),
        })
    feoos_list.sort(key=lambda x: (-x["units"], -x["events"]))

    brand_units = defaultdict(lambda: {"units": 0.0, "events": 0})
    for x in feoos_list:
        b = brand_units[x["brand"] or "—"]
        b["units"] += x["units"]
        b["events"] += x["events"]
    feoos_brands = [
        {"brand": b, "units": round(v["units"], 1), "events": v["events"]}
        for b, v in sorted(brand_units.items(), key=lambda kv: -kv[1]["units"])[:12]
    ]

    feoos_min, feoos_max, feoos_days = day_span(feoos_dates)
    feoos_trend = [
        {"date": d, "events": v["events"], "units": round(v["units"], 1)}
        for d, v in sorted(feoos_by_day.items())
    ]

    # ---- the cohort's DCA1 ordering --------------------------------------
    feoos_by_key = {norm_name(x["name"]): x for x in feoos_list}
    feoos_by_product = {}
    for x in feoos_list:
        feoos_by_product.setdefault(fmt_key(x["name"]), x)
    cohort_sku_list = []
    for key, s in sales_skus.items():
        fk = fmt_key(s["name"])
        f = feoos_by_key.get(key) or feoos_by_product.get(fk)
        in_transition = key in transition or fk in transition
        cohort_sku_list.append({
            "name": s["name"], "brand": s["brand"],
            "units": round(s["units"], 1), "lines": s["lines"],
            "customers": len(s["custs"]),
            "firstOrder": min(s["days"]) if s["days"] else None,
            "lastOrder": max(s["days"]) if s["days"] else None,
            "inTransition": in_transition,
            "inPlan": key in plan or fk in plan,
            # Ordering now but never tracked during the transition, in any
            # pack — the demand the conversion plan did not see coming.
            "isNew": not in_transition,
            "product": fk,
            "pack": pack_label(s["name"]),
            "formatStatus": format_status(s["name"], s["brand"]),
            "mdt1Packs": sorted(mdt1_packs.get(fk, {}).items(),
                                key=lambda kv: -kv[1]),
            "feoosEvents": f["events"] if f else 0,
            "feoosUnits": f["units"] if f else 0.0,
        })
    cohort_sku_list.sort(key=lambda x: -x["units"])
    new_skus = [x for x in cohort_sku_list if x["isNew"]]

    # ---- did the pack swap actually land? ---------------------------------
    # One row per product for the brands DCA1 made a format choice on, with
    # what the cohort bought at MDT1 beside what they are ordering now.
    products = defaultdict(lambda: {"name": "", "brand": "", "packs": defaultdict(float),
                                    "customers": set(), "units": 0.0, "_best": 0.0})
    for key, s_ in sales_skus.items():
        brand = str(s_["brand"] or "").strip().lower()
        if brand not in PREFERRED_FORMAT:
            continue
        fk = fmt_key(s_["name"])
        p = products[fk]
        # Name the product after its biggest pack, not whichever row we hit
        # first — a split product should read under the pack most of the
        # volume is actually on.
        if s_["units"] > p.get("_best", 0):
            p["_best"] = s_["units"]
            p["name"] = s_["name"]
        p["brand"] = p["brand"] or s_["brand"]
        p["packs"][pack_label(s_["name"]) or "—"] += round(s_["units"], 1)
        p["customers"] |= s_["custs"]
        p["units"] += s_["units"]

    format_rows = []
    for fk, p in products.items():
        packs = [{"pack": k, "units": round(v, 1),
                  "status": format_status(k, p["brand"])}
                 for k, v in sorted(p["packs"].items(), key=lambda kv: -kv[1])]
        pref_units = sum(x["units"] for x in packs if x["status"] == "preferred")
        off_units = sum(x["units"] for x in packs if x["status"] == "off-format")
        # n/a when the product has no pack material to be right or wrong about
        # (a 64 oz sauce), split when both packs are still flowing.
        if not pref_units and not off_units:
            state = "n/a"
        elif off_units and pref_units:
            state = "split"
        elif off_units:
            state = "off-format"
        else:
            state = "preferred"
        format_rows.append({
            "product": fk, "name": p["name"], "brand": p["brand"],
            "packs": packs,
            "preferredPack": PREFERRED_FORMAT[p["brand"].strip().lower()]["label"],
            "preferredUnits": round(pref_units, 1),
            "offFormatUnits": round(off_units, 1),
            "state": state,
            "customers": len(p["customers"]),
            "units": round(p["units"], 1),
            "mdt1Packs": sorted(mdt1_packs.get(fk, {}).items(), key=lambda kv: -kv[1]),
        })
    _ORDER = {"split": 0, "off-format": 1, "preferred": 2, "n/a": 3}
    format_rows.sort(key=lambda r: (_ORDER[r["state"]], -r["offFormatUnits"], -r["units"]))

    customers = []
    for uuid, c in sales_custs.items():
        days = sorted(c["days"])
        customers.append({
            "uuid": uuid,
            "name": c["name"] or ROSTER_NAMES.get(uuid, ""),
            "units": round(c["units"], 1), "lines": c["lines"],
            "skus": len(c["skus"]),
            "firstOrder": days[0] if days else None,
            "lastOrder": days[-1] if days else None,
            "feoosEvents": len(net_cohort.get(ROSTER_NAMES.get(uuid, ""), [])),
        })
    customers.sort(key=lambda c: -c["units"])
    not_yet = [
        {"uuid": u, "name": n} for u, n in CONVERTED_CUSTOMERS
        if u not in sales_custs
    ]
    not_yet.sort(key=lambda c: c["name"])

    sales_min, sales_max, _ = day_span(sales_dates)
    cohort_units = round(sum(x["units"] for x in cohort_sku_list), 1)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "warehouse": WAREHOUSE,
        "feoosRange": {"min": feoos_min, "max": feoos_max, "days": feoos_days},
        "salesRange": {"min": sales_min, "max": sales_max},
        # True once a FEOOS source carries customer identity for DCA1; until
        # then the cohort FEOOS view is SKU-level only and the page says so.
        "locationLevel": bool(net_cohort),
        "sourceNote": {
            "dca1FeoosEvents": feoos_events,
            "networkFeoosDca1Events": net_dca1_events,
            "networkFeoosDca1Locations": len(net_dca1_locations),
            "networkFeoosCohortLocations": len(net_cohort),
        },
        "summary": {
            "feoosEvents": feoos_events,
            "feoosSkus": len(feoos_list),
            "feoosUnits": round(sum(x["units"] for x in feoos_list), 1),
            "feoosCohortSkus": sum(1 for x in feoos_list if x["cohortOrders"]),
            "feoosCohortUnits": round(sum(x["units"] for x in feoos_list if x["cohortOrders"]), 1),
            "feoosTransitionSkus": sum(1 for x in feoos_list if x["inTransition"]),
            "cohortTotal": len(CONVERTED_CUSTOMERS),
            "cohortLive": len(customers),
            "cohortNotYet": len(not_yet),
            "cohortOrderLines": sum(x["lines"] for x in cohort_sku_list),
            "cohortSkus": len(cohort_sku_list),
            "cohortUnits": cohort_units,
            "newSkus": len(new_skus),
            "newSkuUnits": round(sum(x["units"] for x in new_skus), 1),
            "planSkusOrdered": sum(1 for x in cohort_sku_list if x["inPlan"]),
            "marketCustomers": len(market_custs),
            # Non-product charge lines dropped from the SKU analysis.
            "chargeLinesExcluded": charge_lines,
            # Pack-format adoption on the brands DCA1 made a choice about.
            "formatProducts": len(format_rows),
            "formatPreferred": sum(1 for r in format_rows if r["state"] == "preferred"),
            "formatSplit": sum(1 for r in format_rows if r["state"] == "split"),
            "formatOff": sum(1 for r in format_rows if r["state"] == "off-format"),
            "formatNa": sum(1 for r in format_rows if r["state"] == "n/a"),
            "offFormatUnits": round(sum(r["offFormatUnits"] for r in format_rows), 1),
            "preferredUnits": round(sum(r["preferredUnits"] for r in format_rows), 1),
        },
        "feoos": feoos_list,
        "feoosTrend": feoos_trend,
        "feoosBrands": feoos_brands,
        "formatAdoption": format_rows,
        "cohortSkus": cohort_sku_list,
        "customers": customers,
        "notYetOrdering": not_yet,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {len(feoos_list)} FEOOS SKUs ({feoos_events} events) and "
        f"{len(cohort_sku_list)} cohort SKUs across {len(customers)} live "
        f"customers to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
