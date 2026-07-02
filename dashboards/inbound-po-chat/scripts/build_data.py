#!/usr/bin/env python3
"""Build data.json for the Inbound PO chat assistant.

Pulls rows from the source Google Sheet via the Sheets API (service-account
auth), filters to open inbound POs, and writes a compact JSON file the
Cloudflare Worker loads to answer questions.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _lib.sheets import parse_num, read_range, sheets_service

SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"
SHEET_RANGE = "'PO Data for Automating.csv'!A1:N"

# Statuses we consider "still inbound" — units expected to arrive.
OPEN_STATUSES = {
    "Pending Receipt",
    "Partially Received",
    "Pending Billing/Partially Received",
}

VENDOR_PREFIX_RE = re.compile(r"^VEN\d+\s+")


def clean_vendor(s: str) -> str:
    return VENDOR_PREFIX_RE.sub("", (s or "").strip())


def main(out_path):
    svc = sheets_service()
    rows = read_range(svc, SPREADSHEET_ID, SHEET_RANGE)
    if not rows:
        sys.exit("Sheet returned no rows")

    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        "PO Created Date Date",
        "Purchase Order Number",
        "Full Vendor Name",
        "Item Name",
        "Item Uuid",
        "Expected Receipt Date Date",
        "Warehouse Name",
        "Purchase Order Units",
        "Purchase Unit Name",
        "Quantity Received",
        "Purchase Order Status",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    lines = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        status = r[col["Purchase Order Status"]]
        if status not in OPEN_STATUSES:
            continue
        ordered = parse_num(r[col["Purchase Order Units"]]) or 0
        received = parse_num(r[col["Quantity Received"]]) or 0
        outstanding = max(ordered - received, 0)
        lines.append(
            {
                "po": r[col["Purchase Order Number"]],
                "created": r[col["PO Created Date Date"]],
                "eta": r[col["Expected Receipt Date Date"]],
                "vendor": clean_vendor(r[col["Full Vendor Name"]]),
                "item": r[col["Item Name"]],
                "itemId": r[col["Item Uuid"]],
                "warehouse": r[col["Warehouse Name"]],
                "ordered": ordered,
                "received": received,
                "outstanding": outstanding,
                "unit": r[col["Purchase Unit Name"]],
                "status": status,
            }
        )

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lineCount": len(lines),
        "warehouses": sorted({l["warehouse"] for l in lines if l["warehouse"]}),
        "statuses": sorted({l["status"] for l in lines}),
        "lines": lines,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {len(lines)} open PO lines to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
