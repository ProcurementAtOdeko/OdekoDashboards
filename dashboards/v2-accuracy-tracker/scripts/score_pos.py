#!/usr/bin/env python3
"""Score the day's purchase orders against the 8am model snapshot (8pm ET run).

Reads the "PO Data for Automating" Looker dump, groups PO lines by creation
date, and for each recent date that has a model snapshot compares what the
Combined V2 model recommended that morning with what was actually ordered:

  hit rate   — % of model-recommended (warehouse, item) lines that got a PO
  precision  — % of ordered (warehouse, item) lines the model recommended
  qty acc    — on matched lines, 1 - |ordered - recommended| / recommended

Each run re-scores the trailing window (POs land in the Looker export late,
receipts trickle in), upserts per-day results into history.json, and writes
data.json with the full history plus line/PO detail for the latest day.

Usage:
  score_pos.py <dashboard_dir> [--all] [--rescore-days N]

<dashboard_dir> must contain snapshots/ written by snapshot_model.py.
--all re-scores every snapshot present (used for backfill/seeding).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

PO_SPREADSHEET_ID = "1x5T4i6WrO22iGJ2-0tX8N_hrOVC4NwRRCkoA5VWMmOo"  # PO Data for Automating.csv
PO_RANGE = "'PO Data for Automating.csv'!A1:N"
RESCORE_DAYS = 8
ET = ZoneInfo("America/New_York")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
VENDOR_PREFIX = re.compile(r"^VEN\d+\s+")


def fetch_po_rows():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    res = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=PO_SPREADSHEET_ID, range=PO_RANGE)
        .execute()
    )
    return res.get("values", [])


def parse_num(s):
    if s is None or s == "":
        return 0.0
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def rate(num, den):
    return round(num / den, 4) if den else None


def mean(vals):
    return round(sum(vals) / len(vals), 4) if vals else None


def group_po_lines(rows):
    """date -> (warehouse, item_uuid) -> aggregated PO line info."""
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    required = [
        "PO Created Date Date", "Purchase Order Number", "Full Vendor Name",
        "Item Name", "Item Uuid", "Warehouse Name", "Purchase Order Units",
        "Purchase Order Status",
    ]
    missing = [c for c in required if c not in col]
    if missing:
        sys.exit(f"Missing expected PO columns: {missing}")

    by_date = defaultdict(dict)
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        date = r[col["PO Created Date Date"]].strip()
        item_uuid = r[col["Item Uuid"]].strip()
        wh = r[col["Warehouse Name"]].strip()
        if not date or not item_uuid or not wh:
            continue
        key = (wh, item_uuid)
        entry = by_date[date].setdefault(key, {
            "wh": wh,
            "itemUuid": item_uuid,
            "item": r[col["Item Name"]],
            "vendor": VENDOR_PREFIX.sub("", r[col["Full Vendor Name"]]).strip(),
            "qty": 0.0,
            "pos": set(),
        })
        entry["qty"] += parse_num(r[col["Purchase Order Units"]])
        po = r[col["Purchase Order Number"]].strip()
        if po:
            entry["pos"].add(po)
    return by_date


def rollup(keys, recommended, ordered, matched_acc, group_fn):
    """Aggregate hit/precision/qty-accuracy over a grouping of line keys."""
    groups = defaultdict(lambda: {"rec": 0, "ord": 0, "match": 0, "accs": []})
    for k in keys:
        g = groups[group_fn(k)]
        if k in recommended:
            g["rec"] += 1
        if k in ordered:
            g["ord"] += 1
        if k in matched_acc:
            g["match"] += 1
            g["accs"].append(matched_acc[k])
    out = []
    for name, g in groups.items():
        out.append({
            "name": name,
            "rec": g["rec"],
            "ord": g["ord"],
            "match": g["match"],
            "hitRate": rate(g["match"], g["rec"]),
            "precision": rate(g["match"], g["ord"]),
            "qtyAcc": mean(g["accs"]),
        })
    out.sort(key=lambda x: -(x["rec"] + x["ord"]))
    return out


def score_date(snapshot, ordered):
    """Compare one day's snapshot lines with that day's aggregated PO lines."""
    recommended = {}
    for ln in snapshot["lines"]:
        # Transfer-order pseudo-vendors are fulfilled by warehouse transfers,
        # never by purchase orders, so they can't be scored against the PO feed.
        if ln["vendor"].endswith("Transfer Order"):
            continue
        recommended[(ln["wh"], ln["itemUuid"])] = ln

    matched_acc = {}
    for key in recommended.keys() & ordered.keys():
        rec_qty = recommended[key]["qty"]
        ord_qty = ordered[key]["qty"]
        matched_acc[key] = max(0.0, 1.0 - abs(ord_qty - rec_qty) / rec_qty) if rec_qty else 0.0

    all_keys = recommended.keys() | ordered.keys()
    po_numbers = set()
    for e in ordered.values():
        po_numbers |= e["pos"]

    def vendor_of(k):
        return (ordered.get(k) or recommended.get(k))["vendor"]

    day = {
        "date": snapshot["date"],
        "rec": len(recommended),
        "ord": len(ordered),
        "match": len(matched_acc),
        "poCount": len(po_numbers),
        "hitRate": rate(len(matched_acc), len(recommended)),
        "precision": rate(len(matched_acc), len(ordered)),
        "qtyAcc": mean(list(matched_acc.values())),
        "byWarehouse": rollup(all_keys, recommended, ordered, matched_acc, lambda k: k[0]),
        "byVendor": rollup(all_keys, recommended, ordered, matched_acc, vendor_of),
    }

    # Line + per-PO detail (kept only for the latest day in data.json).
    lines = []
    for key in sorted(all_keys):
        rec = recommended.get(key)
        orde = ordered.get(key)
        status = "matched" if key in matched_acc else ("missed" if rec else "unplanned")
        lines.append({
            "wh": key[0],
            "itemUuid": key[1],
            "item": (orde or rec)["item"],
            "vendor": vendor_of(key),
            "recQty": rec["qty"] if rec else None,
            "ordQty": round(orde["qty"], 2) if orde else None,
            "pos": sorted(orde["pos"]) if orde else [],
            "status": status,
            "acc": round(matched_acc[key], 4) if key in matched_acc else None,
        })

    pos = defaultdict(lambda: {"lines": 0, "recLines": 0, "units": 0.0,
                               "accs": [], "wh": set(), "vendor": set()})
    for key, e in ordered.items():
        for po in e["pos"]:
            p = pos[po]
            p["lines"] += 1
            p["units"] += e["qty"]
            p["wh"].add(e["wh"])
            p["vendor"].add(e["vendor"])
            if key in recommended:
                p["recLines"] += 1
                p["accs"].append(matched_acc[key])
    po_list = []
    for po, p in sorted(pos.items()):
        line_scores = p["accs"] + [0.0] * (p["lines"] - p["recLines"])
        po_list.append({
            "po": po,
            "vendor": " / ".join(sorted(p["vendor"])),
            "wh": " / ".join(sorted(p["wh"])),
            "lines": p["lines"],
            "recLines": p["recLines"],
            "units": round(p["units"], 2),
            "qtyAcc": mean(p["accs"]),
            "poAcc": mean(line_scores),
        })
    po_list.sort(key=lambda x: (x["poAcc"] is None, x["poAcc"] if x["poAcc"] is not None else 0))

    return day, {"lines": lines, "pos": po_list}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dashboard_dir")
    ap.add_argument("--all", action="store_true",
                    help="re-score every snapshot present, not just the trailing window")
    ap.add_argument("--rescore-days", type=int, default=RESCORE_DAYS)
    args = ap.parse_args()

    dash = Path(args.dashboard_dir)
    snap_dir = dash / "snapshots"
    snapshots = {}
    for p in sorted(snap_dir.glob("model-*.json")):
        m = re.match(r"model-(\d{4}-\d{2}-\d{2})\.json$", p.name)
        if m:
            snapshots[m.group(1)] = p
    if not snapshots:
        sys.exit("No model snapshots found; run snapshot_model.py first")

    rows = fetch_po_rows()
    if not rows:
        sys.exit("PO sheet returned no rows")
    po_by_date = group_po_lines(rows)
    po_dates = sorted(po_by_date.keys())
    print(f"PO export covers {po_dates[0]} .. {po_dates[-1]} "
          f"({sum(len(v) for v in po_by_date.values())} aggregated lines)")

    history_path = dash / "history.json"
    history = {"days": []}
    if history_path.exists():
        with open(history_path) as f:
            history = json.load(f)
    days = {d["date"]: d for d in history.get("days", [])}

    po_min, po_max = po_dates[0], po_dates[-1]
    if args.all:
        to_score = sorted(snapshots)
    else:
        # The PO export refreshes early morning, so it lags "today" by a day
        # or two. Anchor the re-score window on the export's newest date
        # (the freshest data we can actually score against) rather than on
        # the calendar, and additionally pick up any older snapshot that has
        # never been scored — e.g. a day that was still ahead of the PO
        # export when its own 8pm job ran.
        window_start = (
            datetime.strptime(po_max, "%Y-%m-%d") - timedelta(days=args.rescore_days)
        ).strftime("%Y-%m-%d")
        to_score = sorted(
            d for d in snapshots
            if po_min <= d <= po_max and (d >= window_start or d not in days)
        )
    # Snapshots newer than the PO export can't be scored yet; they'll be
    # picked up automatically once the export catches up.
    ahead = sorted(d for d in snapshots if d > po_max)
    if ahead:
        print(f"Ahead of PO export ({po_max}); will score once POs arrive: {', '.join(ahead)}")
    if not to_score:
        print("Nothing new to score; PO export has not advanced past already-scored "
              "days. Leaving data unchanged.")
        return

    latest_detail = None
    for date in to_score:
        with open(snapshots[date]) as f:
            snapshot = json.load(f)
        day, detail = score_date(snapshot, po_by_date.get(date, {}))
        days[date] = day
        latest_detail = (date, detail)
        print(f"{date}: rec={day['rec']} ord={day['ord']} match={day['match']} "
              f"hit={day['hitRate']} prec={day['precision']} qtyAcc={day['qtyAcc']}")

    history = {"days": [days[d] for d in sorted(days)]}
    with open(history_path, "w") as f:
        json.dump(history, f, separators=(",", ":"))

    latest_date, detail = latest_detail
    data = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": "America/New_York",
        "latest": {"date": latest_date, **days[latest_date], **detail},
        "history": history["days"],
    }
    with open(dash / "data.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"Wrote history.json ({len(history['days'])} days) and data.json "
          f"(latest: {latest_date})")


if __name__ == "__main__":
    main()
