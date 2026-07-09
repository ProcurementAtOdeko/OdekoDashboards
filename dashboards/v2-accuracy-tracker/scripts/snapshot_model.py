#!/usr/bin/env python3
"""Snapshot the Combined V2 model's order recommendations (8am ET run).

Reads the "Combined V2 For Dashboards" Looker dump, keeps rows where the
model triggered an order (order_trigger = TRUE and order_quantity_pu > 0),
resolves warehouse / vendor / item names via the "Combined Models Dump for
Dashbaord" sheet, and writes a dated snapshot JSON that the 8pm scoring run
compares against the day's purchase orders.

Usage:
  snapshot_model.py <snapshots_dir> [--date YYYY-MM-DD]
                    [--spreadsheet ID] [--tab 'Warehouse Raw']

--spreadsheet/--tab exist so historical tabs of the archived
"Combined Model V2 Dump " sheet can be backfilled with the same script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

MODEL_SPREADSHEET_ID = "14cQNxWLX4Cqb2Upp-_C6TmRC0-NUNKWYzq4K_3X6mdM"  # Combined V2 For Dashboards
MODEL_TAB = "Warehouse Raw"
NAMES_SPREADSHEET_ID = "1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ"  # Combined Models Dump for Dashbaord
NAMES_TAB = "Warehouse Raw"
SNAPSHOT_RETENTION_DAYS = 60
ET = ZoneInfo("America/New_York")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def sheets_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def parse_num(s):
    if s is None or s == "":
        return 0.0
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def read_tab(svc, spreadsheet_id: str, tab: str) -> list[list[str]]:
    res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{tab}'", majorDimension="ROWS")
        .execute()
    )
    return res.get("values", [])


def build_name_maps(svc):
    """uuid -> human-readable name maps from the combined-models dump."""
    header = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=NAMES_SPREADSHEET_ID, range=f"'{NAMES_TAB}'!1:1")
        .execute()
        .get("values", [[]])[0]
    )
    col = {name: i for i, name in enumerate(header)}
    wanted = [
        "warehouse_uuid", "warehouse_name",
        "procurement_vendor_uuid", "vendor_name",
        "item_uuid", "item_name",
    ]
    missing = [c for c in wanted if c not in col]
    if missing:
        sys.exit(f"Names sheet missing columns: {missing}")
    ranges = [f"'{NAMES_TAB}'!{col_letter(col[c])}2:{col_letter(col[c])}" for c in wanted]
    res = (
        svc.spreadsheets()
        .values()
        .batchGet(spreadsheetId=NAMES_SPREADSHEET_ID, ranges=ranges, majorDimension="COLUMNS")
        .execute()
    )
    cols = [vr.get("values", [[]])[0] if vr.get("values") else [] for vr in res["valueRanges"]]

    def to_map(key_col, val_col):
        m = {}
        for k, v in zip(key_col, val_col):
            if k and v and k not in m:
                m[k] = v
        return m

    return (
        to_map(cols[0], cols[1]),  # warehouse
        to_map(cols[2], cols[3]),  # vendor
        to_map(cols[4], cols[5]),  # item
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshots_dir")
    ap.add_argument("--date", help="Snapshot date (ET), default today")
    ap.add_argument("--spreadsheet", default=MODEL_SPREADSHEET_ID)
    ap.add_argument("--tab", default=MODEL_TAB)
    args = ap.parse_args()

    date = args.date or datetime.now(ET).strftime("%Y-%m-%d")
    svc = sheets_service()
    wh_names, vendor_names, item_names = build_name_maps(svc)

    rows = read_tab(svc, args.spreadsheet, args.tab)
    if not rows:
        sys.exit("Model sheet returned no rows")
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        "warehouse_uuid", "vendor_uuid", "item_uuid",
        "order_trigger", "order_quantity_pu", "raw_order_quantity_pu", "as_of",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected columns: {missing}")

    as_of = ""
    lines = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        if not as_of and r[col["as_of"]]:
            as_of = r[col["as_of"]]
        if str(r[col["order_trigger"]]).strip().upper() != "TRUE":
            continue
        qty = parse_num(r[col["order_quantity_pu"]])
        if qty <= 0:
            continue
        wh_uuid = r[col["warehouse_uuid"]]
        vendor_uuid = r[col["vendor_uuid"]]
        item_uuid = r[col["item_uuid"]]
        if not item_uuid:
            continue
        lines.append({
            "wh": wh_names.get(wh_uuid, wh_uuid[:8]),
            "vendor": vendor_names.get(vendor_uuid, vendor_uuid[:8]),
            "itemUuid": item_uuid,
            "item": item_names.get(item_uuid, item_uuid[:8]),
            "qty": round(qty, 2),
            "raw": round(parse_num(r[col["raw_order_quantity_pu"]]), 2),
        })

    out_dir = Path(args.snapshots_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"model-{date}.json"
    payload = {
        "date": date,
        "asOf": as_of,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": args.spreadsheet,
        "tab": args.tab,
        "lines": lines,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {len(lines)} triggered lines to {out_path} (as_of {as_of})")

    # Prune snapshots older than the retention window so the repo stays lean.
    cutoff = datetime.now(ET).timestamp() - SNAPSHOT_RETENTION_DAYS * 86400
    for p in sorted(out_dir.glob("model-*.json")):
        m = re.match(r"model-(\d{4}-\d{2}-\d{2})\.json$", p.name)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=ET).timestamp()
        if ts < cutoff:
            p.unlink()
            print(f"Pruned {p.name}")


if __name__ == "__main__":
    main()
