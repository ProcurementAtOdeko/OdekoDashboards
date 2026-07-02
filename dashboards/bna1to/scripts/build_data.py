#!/usr/bin/env python3
"""Build the dashboard's data.json by merging two CSV exports per UUID.

Source sheet (procurement source-side):
  - eta:          col G, parsed list of YYYY-MM-DD strings
  - ohUnits:      col J, on-hand quantity in purchase units (EWR1)
  - consumption:  col K, purchase-unit consumption per day (EWR1)
  - toAvailable:  col L, available TO quantity in purchase units
  - leadDays:     col M, vendor lead time in days

Destination sheet (BNA1 inventory view):
  - bnaInventory:    column 'inventory' (purchase units on hand at BNA1)
  - bnaConsumption:  column 'consumption_rate' (purchase units / day at BNA1)
  - bnaDaysOfCover:  column 'net_days_of_cover'
  - casesPerLayer:   column 'cases_per_layer'   (BK)
  - layersPerPallet: column 'layers_per_pallet' (BL)
  - casesPerPallet:  column 'cases_per_pallet'  (BM)
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _lib.sheets import csv_export

SOURCE_SPREADSHEET_ID = "1FGsAgYm72Sttg9zK-eGMqnbbIB4d71rDyrpUupAV4OE"
SOURCE_GID = 0
DEST_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"
DEST_GID = 1794766977

SOURCE_URL = f"https://docs.google.com/spreadsheets/d/{SOURCE_SPREADSHEET_ID}/edit?gid={SOURCE_GID}#gid={SOURCE_GID}"
DEST_URL = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}/edit?gid={DEST_GID}#gid={DEST_GID}"

SRC_COLS = {
    "uuid": "Item Extid",
    "eta": "Upcoming Expectedreceiptdates",
    "ohUnits": "OH Purchase Unit",
    "consumption": "Purchase Unit Cons",
    "toAvailable": "TO QTY Aval",
    "leadDays": "Lead",
}

DEST_COLS = {
    "warehouse": "warehouse_name",
    "uuid": "item_uuid",
    "bnaInventory": "inventory",
    "bnaConsumption": "consumption_rate",
    "bnaDaysOfCover": "net_days_of_cover",
    "casesPerLayer": "cases_per_layer",
    "layersPerPallet": "layers_per_pallet",
    "casesPerPallet": "cases_per_pallet",
}

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_dates(raw: str) -> list[str]:
    return sorted(set(DATE_RE.findall(raw)))


# Local rather than _lib.sheets.parse_num: this dashboard's payload relies on
# int coercion and 4-decimal rounding to stay compact.
def parse_num(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    if math.isnan(n) or math.isinf(n):
        return None
    return int(n) if n.is_integer() else round(n, 4)


def require_cols(reader: csv.DictReader, needed, label: str) -> None:
    missing = [c for c in needed if c not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f"{label}: required columns missing: {missing}. Have: {reader.fieldnames}")


def load_source(csv_text: str, label: str) -> dict:
    items: dict = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    require_cols(reader, SRC_COLS.values(), label)
    for row in reader:
        uuid = (row.get(SRC_COLS["uuid"]) or "").strip()
        if not uuid:
            continue
        rec = items.setdefault(uuid, {})
        dates = parse_dates(row.get(SRC_COLS["eta"]) or "")
        if dates:
            rec["eta"] = dates
        for key in ("ohUnits", "consumption", "toAvailable", "leadDays"):
            v = parse_num(row.get(SRC_COLS[key]))
            if v is None:
                continue
            # EWR1's available TO quantity can be negative when the warehouse
            # is already overdrawn. Floor at 0 so downstream math and display
            # treat "negative" as "none available".
            if key == "toAvailable" and v < 0:
                v = 0
            rec[key] = v
    return items


def load_dest(csv_text: str, label: str, warehouse: str = "BNA1") -> dict:
    items: dict = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    require_cols(reader, DEST_COLS.values(), label)
    for row in reader:
        if (row.get(DEST_COLS["warehouse"]) or "").strip().upper() != warehouse.upper():
            continue
        uuid = (row.get(DEST_COLS["uuid"]) or "").strip()
        if not uuid:
            continue
        rec = {}
        for key in (
            "bnaInventory",
            "bnaConsumption",
            "bnaDaysOfCover",
            "casesPerLayer",
            "layersPerPallet",
            "casesPerPallet",
        ):
            v = parse_num(row.get(DEST_COLS[key]))
            if v is not None:
                rec[key] = v
        if rec:
            items[uuid] = rec
    return items


def main(out_path: str) -> int:
    source_items = load_source(
        csv_export(SOURCE_SPREADSHEET_ID, SOURCE_GID), "procurement source"
    )
    dest_items = load_dest(
        csv_export(DEST_SPREADSHEET_ID, DEST_GID), "BNA1 inventory", "BNA1"
    )

    merged: dict = {}
    for uuid, rec in source_items.items():
        merged[uuid] = dict(rec)
    for uuid, rec in dest_items.items():
        merged.setdefault(uuid, {}).update(rec)

    payload = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"procurement": SOURCE_URL, "bna1Inventory": DEST_URL},
        "count": len(merged),
        "items": merged,
    }
    Path(out_path).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {out_path} with {len(merged)} entries "
        f"({len(source_items)} from source, {len(dest_items)} from BNA1)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data.json"))
