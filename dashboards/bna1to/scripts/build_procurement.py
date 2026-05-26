#!/usr/bin/env python3
"""Build procurement.json by merging two CSV exports per UUID.

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
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE_URL = "https://docs.google.com/spreadsheets/d/1FGsAgYm72Sttg9zK-eGMqnbbIB4d71rDyrpUupAV4OE/edit?gid=0#gid=0"
DEST_URL = "https://docs.google.com/spreadsheets/d/1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ/edit?gid=1794766977#gid=1794766977"

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


import math


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


def load_source(path: Path) -> dict:
    items: dict = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        require_cols(reader, SRC_COLS.values(), str(path))
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


def load_dest(path: Path, warehouse: str = "BNA1") -> dict:
    items: dict = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        require_cols(reader, DEST_COLS.values(), str(path))
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


def main(src_path: str, dest_path: str, out_path: str) -> int:
    src = Path(src_path)
    dest = Path(dest_path)
    if not src.is_file():
        print(f"Source CSV not found: {src}", file=sys.stderr)
        return 1
    if not dest.is_file():
        print(f"Destination CSV not found: {dest}", file=sys.stderr)
        return 1

    source_items = load_source(src)
    dest_items = load_dest(dest, "BNA1")

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
    if len(sys.argv) != 4:
        print(
            "usage: build_procurement.py <source.csv> <bna1.csv> <output.json>",
            file=sys.stderr,
        )
        sys.exit(64)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
