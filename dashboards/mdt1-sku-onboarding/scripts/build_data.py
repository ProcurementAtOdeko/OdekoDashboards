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

Preferred format:
  We don't always want to onboard the exact SKU MDT1 customers bought — for
  some brands DCA1 standardises on a different pack. PREFERRED_FORMAT maps a
  brand to the format we'd rather carry (Monin → glass, Torani → 1L plastic).
  Where the cohort SKU isn't already in that format and a same-flavour
  variant exists, the tracker retargets onto the preferred variant and tracks
  *that* SKU through the pipeline. If a PO or catalog entry exists on the
  original (non-preferred) format instead, that's surfaced as off-format
  activity rather than silently counting as progress.

Order quantities:
  The cohort's Units column is trailing DEMAND_WINDOW_DAYS (90) of MDT1
  demand. Recommendations are sized to TARGET_DAYS (60) of that demand and
  converted to cases using the item's case pack, which is parsed from the
  PO feed's "Purchase Unit Name" ("Case (6x)" → 6, "Each" → 1) across all
  warehouses — the pack is a property of the item, not of one warehouse.

Transfer-order recommendation:
  MDT1 is being wound down, so where MDT1 still holds deep stock we would
  rather transfer it than buy new. Any cohort SKU whose MDT1 "Days of Cover
  60 Days Eaches" exceeds TO_DOC_THRESHOLD and which is not already received
  or live at DCA1 is flagged as a transfer-order candidate, sized in cases.

  MDT1 is still serving its own customers while it winds down, so a transfer
  must never strip it below MIN_RETAIN_DOC days of cover. On hand is
  proportional to days of cover (doc = on_hand_eaches / daily_consumption),
  so pulling X leaves doc * (1 - X / on_hand); the releasable share is
  therefore (doc - MIN_RETAIN_DOC) / doc. That fraction also caps at what
  MDT1 physically holds, so it subsumes a plain on-hand cap.

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
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

PO_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
PO_RANGE = "'PO Data for Automating.csv'!A1:N"
MODELS_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
MODELS_RANGE = "'Warehouse Raw'!A1:H"
# DCA1 ordering model — carries the NetSuite UUIDs the bulk PO/TO upload
# tools require (warehouse_uuid, procurement_vendor_uuid) for DCA1's own
# vendors.
DCA1_MODEL_SPREADSHEET_ID = "162M43zm7D65Z3JqHpPqh1pa5Xrd6NLLvPuDu9qmpM8M"
# Network-wide V2 model. Vendor UUIDs are universal, so vendors DCA1 doesn't
# buy from yet can still be resolved from whichever warehouse does buy from
# them. This sheet has vendor_uuid + vendor_id but no vendor_name, so it is
# joined to the combined models dump (vendor_id -> vendor_name) by vendor_id.
V2_SPREADSHEET_ID = "14cQNxWLX4Cqb2Upp-_C6TmRC0-NUNKWYzq4K_3X6mdM"
V2_RANGE = "'Warehouse Raw'!A1:AR"
ONHAND_SPREADSHEET_ID = "11PkkcjiAGOpoRLLuj1LEXH3nXp2iYkS6cjqqxJOWnuU"
ONHAND_RANGE = "'On Hand & ETA.csv'!A1:J"

TARGET_WAREHOUSE = "DCA1"   # where the SKUs are being onboarded to
SOURCE_WAREHOUSE = "MDT1"   # the warehouse being wound down
TO_DOC_THRESHOLD = 50       # MDT1 days-of-cover above which we suggest a TO

DEMAND_WINDOW_DAYS = 90     # the cohort's Units column is trailing 90 days
TARGET_DAYS = 60            # size opening orders to 60 days of that demand
MIN_RETAIN_DOC = 20         # never transfer MDT1 below this many days of cover

# Brand → pack format DCA1 would rather carry. Matched on (material, size);
# a None size means "any size in that material".
PREFERRED_FORMAT = {
    "monin": {"material": "glass", "size": None, "label": "glass"},
    "torani": {"material": "plastic", "size": "1l", "label": "1L plastic"},
}

COHORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cohort.csv")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


# --- Format matching ------------------------------------------------------
# Same shape as the conversion dashboards: a format-agnostic key that keeps
# brand + flavour + bare spec numbers but drops size/unit and container words,
# so "Monin Lavender Syrup 1 Liter Plastic Bottle" and "…750ml Glass" collapse
# to one flavour key while "14 x 5" and "15 x 5" filters stay distinct.
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
    s = norm_name(name).replace("bottle(s)", " ")
    s = _SIZE.sub(" ", s)
    s = _UNITWORD.sub(" ", s)
    s = _CONT.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return _WS.sub(" ", s).strip()


def canon_size(name):
    """Canonical volume token, e.g. '1l', '750ml', '1/2gal'. '' if none."""
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


_CASE = re.compile(r"case\s*\((\d+(?:\.\d+)?)x\)", re.I)


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
    """DCA1 PO lines for the tracked SKUs, plus a network-wide case-pack map.

    Returns (pos, case_sizes):
      pos        — norm_name -> [PO line, ...] for DCA1 only
      case_sizes — norm_name -> eaches per case, learned from PO lines at ANY
                   warehouse (the pack is a property of the item, and most
                   tracked SKUs have no DCA1 PO yet)
    """
    rows = get_values(svc, PO_SPREADSHEET_ID, PO_RANGE)
    if not rows:
        sys.exit("PO sheet returned no rows")
    header = rows[0]
    for c in ["Item Name", "Warehouse Name", "Purchase Order Number",
              "Purchase Order Status", "Purchase Order Units",
              "Quantity Received", "Shipment Received Utc Date",
              "Purchase Unit Name"]:
        if c not in header:
            sys.exit(f"PO sheet missing expected column: {c}")

    pos = defaultdict(list)
    case_votes = defaultdict(Counter)
    for r in rows[1:]:
        g = row_getter(header, r)
        key = norm_name(g("Item Name"))
        cs = case_size_of(g("Purchase Unit Name"))
        if cs:
            case_votes[key][cs] += 1
        if g("Warehouse Name") != TARGET_WAREHOUSE or key not in wanted:
            continue
        pos[key].append({
            "po": g("Purchase Order Number"),
            "vendor": g("Full Vendor Name"),
            "status": g("Purchase Order Status"),
            "created": g("PO Created Date Date"),
            "expected": g("Expected Receipt Date Date"),
            "receivedDate": g("Shipment Received Utc Date"),
            "ordered": parse_num(g("Purchase Order Units")) or 0.0,
            "received": parse_num(g("Quantity Received")) or 0.0,
            "unit": g("Purchase Unit Name"),
            "caseSize": cs,
        })
    case_sizes = {k: v.most_common(1)[0][0] for k, v in case_votes.items()}
    return pos, case_sizes


_VEN_PREFIX = re.compile(r"^VEN\d+\s+")


def vendor_key(name):
    """Vendor names appear with and without a 'VEN00001293 ' NetSuite prefix."""
    return _VEN_PREFIX.sub("", str(name).strip()).lower()


def load_upload_refs(svc):
    """NetSuite identifiers the bulk PO/TO upload tools require.

    Returns (warehouse, vendor_uuids) where warehouse is the DCA1
    location id / uuid pair and vendor_uuids maps vendor_key -> uuid.

    Read from the newest dated tab of the DCA1 ordering model. Vendor UUIDs
    only exist for vendors DCA1 already buys from — a SKU sourced from a
    vendor DCA1 has no relationship with yet cannot be uploaded until that
    vendor is set up, so we surface the gap rather than emit a blank field.
    """
    warehouse = {"locationId": "", "uuid": ""}
    vendor_uuids = {}
    try:
        meta = svc.spreadsheets().get(spreadsheetId=DCA1_MODEL_SPREADSHEET_ID).execute()
    except Exception:
        return warehouse, vendor_uuids
    # Dated tabs look like "Tue 08-11"; the sheet lists newest first.
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
            # An id can appear against more than one uuid across warehouses;
            # take the most frequently used one.
            out.setdefault(vendor_key(name), uuid_by_vid[vid].most_common(1)[0][0])
    return out


def load_item_refs(svc, wanted):
    """item name -> {uuid, vendor, purchaseUnit} from the network PO feed.

    The PO export is the only source that carries item UUIDs for SKUs DCA1
    doesn't stock yet, which is most of this cohort.
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
    # On Hand & ETA fills in the procurement vendor where no PO exists.
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


def load_universe(svc):
    """Every item name we know of, for resolving preferred-format variants.

    Spans all warehouses on purpose: DCA1 carries no Torani today, so the 1L
    plastic variant we want to bring in only exists elsewhere in the network.
    """
    universe = {}
    for sid, rng, col in (
        (ONHAND_SPREADSHEET_ID, ONHAND_RANGE, "Item Name"),
        (MODELS_SPREADSHEET_ID, MODELS_RANGE, "item_name"),
    ):
        rows = get_values(svc, sid, rng)
        if not rows:
            continue
        header = rows[0]
        for r in rows[1:]:
            name = row_getter(header, r)(col)
            if name:
                universe.setdefault(norm_name(name), str(name).strip())
    return universe


def build_variant_index(universe):
    """flavour key -> [item name, ...] across the network."""
    idx = defaultdict(list)
    for name in universe.values():
        idx[fmt_key(name)].append(name)
    return idx


def preferred_target(name, brand, variants):
    """The SKU we'd rather onboard for this cohort item, or None to keep it.

    Returns (target_name, format_label) where target_name is None when the
    item is already in the preferred format or no variant exists.
    """
    pref = PREFERRED_FORMAT.get(str(brand).strip().lower())
    if not pref:
        return None, ""
    want_mat, want_size = pref["material"], pref["size"]
    if pack_material(name) == want_mat and (
        want_size is None or canon_size(name) == want_size
    ):
        return None, pref["label"]  # already preferred
    cands = [
        c for c in variants.get(fmt_key(name), [])
        if pack_material(c) == want_mat
        and (want_size is None or canon_size(c) == want_size)
    ]
    if not cands:
        return None, pref["label"]
    # Prefer the most common/shortest name when several variants collide.
    cands.sort(key=lambda x: (len(x), x))
    return cands[0], pref["label"]


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
    universe = load_universe(svc)
    variants = build_variant_index(universe)

    # Resolve each cohort SKU onto the pack format DCA1 would rather carry,
    # then track that target through the pipeline.
    for c in cohort:
        target, label = preferred_target(c["name"], c["brand"], variants)
        c["target"] = target or c["name"]
        c["formatPref"] = label
        c["formatSwitched"] = bool(target)

    # Look up both the target and the original: activity on the original is
    # off-format and should be flagged, not counted as progress.
    wanted = {norm_name(c["name"]) for c in cohort}
    wanted |= {norm_name(c["target"]) for c in cohort}

    pos, case_sizes = load_pos(svc, wanted)
    live = load_catalog(svc, wanted)
    stock = load_mdt1_stock(svc, wanted)
    item_refs = load_item_refs(svc, wanted)
    wh_ref, dca1_vendor_uuids = load_upload_refs(svc)
    network_vendor_uuids = load_network_vendor_uuids(svc)

    # Guard: if the PO export is mid-refresh we'd wrongly reset every SKU to
    # "not started". Only trust an empty PO map when the sheet genuinely has
    # DCA1 rows for other items.
    if not pos and not live:
        sys.exit(
            "No DCA1 PO or catalog rows matched the cohort — sources likely "
            "mid-refresh; leaving existing data.json untouched."
        )

    scale = TARGET_DAYS / float(DEMAND_WINDOW_DAYS)

    items = []
    for c in cohort:
        key = norm_name(c["target"])
        orig_key = norm_name(c["name"])
        lines = pos.get(key, [])
        st = stock.get(key) or stock.get(orig_key) or {}

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

        # Off-format activity: the non-preferred pack is on PO or in catalog
        # while the pack we actually want is not.
        off_format = ""
        if c["formatSwitched"]:
            if orig_key in live:
                off_format = "in catalog"
            elif pos.get(orig_key):
                off_format = "on PO"

        # Opening order sized to TARGET_DAYS of trailing-window demand.
        case_size = case_sizes.get(key) or case_sizes.get(orig_key)
        units60 = c["units"] * scale
        cases60 = math.ceil(units60 / case_size) if case_size else None

        doc = st.get("doc")
        # Recommend pulling from MDT1 rather than buying when cover is deep
        # and the SKU hasn't already landed at DCA1.
        to_rec = doc is not None and doc > TO_DOC_THRESHOLD and stage_i < 2

        # What MDT1 can release without dropping below MIN_RETAIN_DOC.
        mdt1_cases = st.get("onHand")
        releasable = None
        if doc and doc > 0 and mdt1_cases is not None:
            share = max(0.0, (doc - MIN_RETAIN_DOC) / doc)
            releasable = max(0, math.floor(mdt1_cases * share))

        to_cases = None
        to_capped = ""
        if to_rec and cases60 is not None:
            to_cases = cases60
            if releasable is not None and releasable < cases60:
                to_cases = releasable
                to_capped = f"holds MDT1 at {MIN_RETAIN_DOC} DOC"

        # Days of cover MDT1 is left with after the recommended transfer.
        doc_after = doc
        if to_cases and doc and mdt1_cases:
            doc_after = round(doc * (1 - to_cases / mdt1_cases), 1)

        # NetSuite identifiers for the bulk PO/TO upload templates.
        ref = item_refs.get(key) or item_refs.get(orig_key) or {}
        vendor_name = _VEN_PREFIX.sub("", (ref.get("vendor") or "").strip())
        vk = vendor_key(vendor_name) if vendor_name else ""
        # DCA1's own record first; otherwise the universal UUID from whichever
        # warehouse already buys from this vendor.
        vendor_uuid = dca1_vendor_uuids.get(vk, "") if vk else ""
        vendor_from_dca1 = bool(vendor_uuid)
        if not vendor_uuid and vk:
            vendor_uuid = network_vendor_uuids.get(vk, "")
        purchase_unit = ref.get("purchaseUnit") or ""
        if not purchase_unit and case_size:
            purchase_unit = "Each" if case_size == 1 else f"Case ({int(case_size)}x)"

        lines_sorted = sorted(lines, key=lambda l: l["created"] or "", reverse=True)
        items.append({
            "itemUuid": ref.get("uuid", ""),
            "vendorName": vendor_name,
            "vendorUuid": vendor_uuid,
            "purchaseUnit": purchase_unit,
            # No UUID anywhere — the row genuinely can't be uploaded.
            "needsVendorSetup": bool(vendor_name) and not vendor_uuid,
            # Uploadable, but DCA1 has never bought from this vendor, so the
            # warehouse/vendor delivery rules the PO SOP requires may not be
            # set up yet.
            "vendorNewToDca1": bool(vendor_uuid) and not vendor_from_dca1,
            "name": c["name"],
            "target": c["target"],
            "formatSwitched": c["formatSwitched"],
            "formatPref": c["formatPref"],
            "pack": pack_label(c["target"]),
            "originalPack": pack_label(c["name"]),
            "offFormat": off_format,
            "brand": c["brand"],
            "units": round(c["units"], 1),
            "units60": round(units60, 1),
            "caseSize": case_size,
            "cases60": cases60,
            "customers": c["customers"],
            "stage": stage,
            "stageIndex": stage_i,
            "hasPo": has_po,
            "poCount": len(lines),
            "ordered": round(ordered, 1),
            "received": round(received, 1),
            "inCatalog": is_live,
            "mdt1Doc": doc,
            "mdt1OnHand": mdt1_cases,
            "mdt1Consumption60": st.get("consumption60"),
            "toRecommended": to_rec,
            "toCases": to_cases,
            "toCapped": to_capped,
            "mdt1Releasable": releasable,
            "mdt1DocAfter": doc_after,
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
    # A "transfer" only counts if MDT1 can actually release something once its
    # MIN_RETAIN_DOC reserve is honoured. Candidates that can release nothing
    # fall through to the buy list rather than sitting in a list of zeroes.
    to_items = [i for i in items if i["toRecommended"] and (i["toCases"] or 0) > 0]
    to_keys = {id(i) for i in to_items}
    # Everything still to source that a transfer won't cover becomes a buy.
    po_items = [
        i for i in items if i["stageIndex"] == 0 and id(i) not in to_keys
    ]
    total = len(items)
    done = counts["Live in catalog"]
    switched = sum(1 for i in items if i["formatSwitched"])
    off_fmt = [i for i in items if i["offFormat"]]

    def rec_row(i):
        # On a partial transfer the reserve leaves a gap — buy the remainder.
        shortfall = max(0, (i["cases60"] or 0) - (i["toCases"] or 0)) \
            if i["toRecommended"] and (i["toCases"] or 0) > 0 else 0
        return {
            "buyCases": shortfall,
            # Fields the bulk PO/TO upload templates require verbatim.
            "itemUuid": i["itemUuid"],
            "vendorName": i["vendorName"],
            "vendorUuid": i["vendorUuid"],
            "purchaseUnit": i["purchaseUnit"],
            "needsVendorSetup": i["needsVendorSetup"],
            "vendorNewToDca1": i["vendorNewToDca1"],
            "name": i["target"],
            "originalName": i["name"] if i["formatSwitched"] else "",
            "brand": i["brand"],
            "pack": i["pack"],
            "stage": i["stage"],
            "units": i["units"],
            "units60": i["units60"],
            "caseSize": i["caseSize"],
            "cases": i["cases60"],
            "mdt1Doc": i["mdt1Doc"],
            "mdt1OnHand": i["mdt1OnHand"],
            "toCases": i["toCases"],
            "toCapped": i["toCapped"],
            "mdt1Releasable": i["mdt1Releasable"],
            "mdt1DocAfter": i["mdt1DocAfter"],
        }

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targetWarehouse": TARGET_WAREHOUSE,
        "sourceWarehouse": SOURCE_WAREHOUSE,
        "docThreshold": TO_DOC_THRESHOLD,
        "minRetainDoc": MIN_RETAIN_DOC,
        "demandWindowDays": DEMAND_WINDOW_DAYS,
        "targetDays": TARGET_DAYS,
        "formatPreferences": {
            b: p["label"] for b, p in sorted(PREFERRED_FORMAT.items())
        },
        # Constants the bulk upload templates need for the destination WH.
        "warehouseLocationId": wh_ref["locationId"],
        "warehouseUuid": wh_ref["uuid"],
        "summary": {
            "skusTracked": total,
            "notStarted": counts["Not started"],
            "onPo": counts["On PO"],
            "received": counts["Received"],
            "liveInCatalog": done,
            "completePct": round(100 * done / total) if total else 0,
            "toRecommended": len(to_items),
            "poRecommended": len(po_items),
            "formatSwitched": switched,
            "offFormatActivity": len(off_fmt),
            "needsVendorSetup": sum(
                1 for i in to_items + po_items if i["needsVendorSetup"]
            ),
            "vendorNewToDca1": sum(
                1 for i in to_items + po_items if i["vendorNewToDca1"]
            ),
            "unitsTracked": round(sum(i["units"] for i in items), 1),
            "units60Total": round(sum(i["units60"] for i in items), 1),
            "toCasesTotal": sum(i["toCases"] or 0 for i in to_items),
            "toCappedByReserve": sum(1 for i in to_items if i["toCapped"]),
            # Cases a partial transfer can't cover — buy these on top.
            "toShortCases": sum(
                max(0, (i["cases60"] or 0) - (i["toCases"] or 0)) for i in to_items
            ),
            "poCasesTotal": sum(i["cases60"] or 0 for i in po_items),
        },
        "pipeline": [
            {"stage": "Not started", "count": counts["Not started"]},
            {"stage": "On PO", "count": counts["On PO"]},
            {"stage": "Received", "count": counts["Received"]},
            {"stage": "Live in catalog", "count": counts["Live in catalog"]},
        ],
        "transferCandidates": sorted(
            (rec_row(i) for i in to_items), key=lambda x: -(x["mdt1Doc"] or 0)
        ),
        "purchaseCandidates": sorted(
            (rec_row(i) for i in po_items), key=lambda x: -(x["units"] or 0)
        ),
        "items": items,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"Wrote {total} SKUs "
        f"(not started {counts['Not started']}, on PO {counts['On PO']}, "
        f"received {counts['Received']}, live {done}; "
        f"{switched} format-switched, {len(off_fmt)} off-format; "
        f"{len(to_items)} TO / {len(po_items)} PO candidates) to {out_path}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
