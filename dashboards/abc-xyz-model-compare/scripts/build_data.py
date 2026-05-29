#!/usr/bin/env python3
"""Build data.json for the ABC/XYZ Model Comparison dashboard.

Downloads two CSVs from the Looker Data Dumps Google Drive folder
(model_with_categories and model_without_categories), joins them on
(warehouse_uuid, item_uuid), and emits a JSON describing the per-item
ABC/XYZ classification in each model plus per-category change counts.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

WITH_CATS_FILE_ID = "1VSXy14cq3Lx4ki8y2hOdj2BAXLetMEb6"
WITHOUT_CATS_FILE_ID = "14Kk7tf5y8DYWKKQNkJy2mF7MYotYZspN"

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

ABC_BUCKETS = ["a", "b", "c", "d"]
XYZ_BUCKETS = ["x", "y", "z"]

ABC_RANK = {"a": 1, "b": 2, "c": 3, "d": 4}
XYZ_RANK = {"x": 1, "y": 2, "z": 3}


def direction(old: str, new: str, ranks: dict) -> str:
    """Return '+' if new is a better rank than old (closer to A/X),
    '-' if worse, '0' if same, '' if either side is missing."""
    if not old or not new:
        return ""
    if old == new:
        return "0"
    ro = ranks.get(old, 99)
    rn = ranks.get(new, 99)
    if rn < ro:
        return "+"
    if rn > ro:
        return "-"
    return "0"


def download_csv(creds, file_id: str) -> str:
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {creds.token}"}
    r = requests.get(url, headers=headers, stream=True, timeout=120)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def parse_rows(text: str):
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [f.replace("\\_", "_").strip() for f in reader.fieldnames or []]
    reader.fieldnames = fieldnames
    rows = []
    for row in reader:
        clean = {k.replace("\\_", "_").strip(): (v or "").strip() for k, v in row.items() if k}
        rows.append(clean)
    return rows


def norm(v):
    if v is None:
        return ""
    s = str(v).strip().lower()
    return s


def main(out_path: str):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    creds.refresh(GoogleAuthRequest())

    print("Downloading model_with_categories…", file=sys.stderr)
    with_text = download_csv(creds, WITH_CATS_FILE_ID)
    print(f"  {len(with_text):,} bytes", file=sys.stderr)
    print("Downloading model_without_categories…", file=sys.stderr)
    without_text = download_csv(creds, WITHOUT_CATS_FILE_ID)
    print(f"  {len(without_text):,} bytes", file=sys.stderr)

    with_rows = parse_rows(with_text)
    without_rows = parse_rows(without_text)
    print(f"with={len(with_rows):,} rows, without={len(without_rows):,} rows", file=sys.stderr)

    def index_by_uuid(rows):
        idx = {}
        for r in rows:
            wh = r.get("warehouse_uuid", "")
            it = r.get("item_uuid", "")
            if not wh or not it:
                continue
            idx[(wh, it)] = r
        return idx

    with_idx = index_by_uuid(with_rows)
    without_idx = index_by_uuid(without_rows)

    keys = set(with_idx) | set(without_idx)
    print(f"unique uuid pairs: {len(keys):,}", file=sys.stderr)

    items = []
    cat_stats = defaultdict(lambda: {
        "category": "",
        "total": 0,
        "abcChanged": 0,
        "xyzChanged": 0,
        "abcXyzChanged": 0,
        "abcUp": 0,
        "abcDown": 0,
        "xyzUp": 0,
        "xyzDown": 0,
        "onlyWith": 0,
        "onlyWithout": 0,
        "both": 0,
        "abcMatrix": defaultdict(int),
        "xyzMatrix": defaultdict(int),
    })
    abc_matrix = defaultdict(int)
    xyz_matrix = defaultdict(int)

    total_both = 0
    total_only_with = 0
    total_only_without = 0
    total_abc_changed = 0
    total_xyz_changed = 0
    total_class_changed = 0
    total_abc_up = 0
    total_abc_down = 0
    total_xyz_up = 0
    total_xyz_down = 0

    for key in keys:
        w = with_idx.get(key)
        o = without_idx.get(key)
        src = w or o
        warehouse_uuid, item_uuid = key

        category = (src.get("category") or "") if src else ""
        subcategory = (src.get("subcategory") or "") if src else ""
        warehouse_name = (
            (w.get("warehouse_name") if w else "")
            or (o.get("warehouse_name") if o else "")
        )
        warehouse_id = (
            (w.get("warehouse_id") if w else "")
            or (o.get("warehouse_id") if o else "")
        )
        item_name = (
            (w.get("item_name") if w else "")
            or (o.get("item_name") if o else "")
        )
        item_id = (
            (w.get("item_id") if w else "")
            or (o.get("item_id") if o else "")
        )
        vendor_name = (
            (w.get("vendor_name") if w else "")
            or (o.get("vendor_name") if o else "")
        )

        abc_with = norm(w.get("abc")) if w else ""
        xyz_with = norm(w.get("xyz")) if w else ""
        abc_without = norm(o.get("abc")) if o else ""
        xyz_without = norm(o.get("xyz")) if o else ""

        present = "both" if (w and o) else ("with" if w else "without")
        if present == "both":
            total_both += 1
        elif present == "with":
            total_only_with += 1
        else:
            total_only_without += 1

        abc_changed = present == "both" and abc_with != abc_without
        xyz_changed = present == "both" and xyz_with != xyz_without
        any_changed = abc_changed or xyz_changed
        abc_dir = direction(abc_with, abc_without, ABC_RANK) if present == "both" else ""
        xyz_dir = direction(xyz_with, xyz_without, XYZ_RANK) if present == "both" else ""
        if abc_changed:
            total_abc_changed += 1
        if xyz_changed:
            total_xyz_changed += 1
        if any_changed:
            total_class_changed += 1
        if abc_dir == "+": total_abc_up += 1
        if abc_dir == "-": total_abc_down += 1
        if xyz_dir == "+": total_xyz_up += 1
        if xyz_dir == "-": total_xyz_down += 1

        cs = cat_stats[category]
        cs["category"] = category
        cs["total"] += 1
        if present == "both":
            cs["both"] += 1
            if abc_changed:
                cs["abcChanged"] += 1
            if xyz_changed:
                cs["xyzChanged"] += 1
            if any_changed:
                cs["abcXyzChanged"] += 1
            if abc_dir == "+": cs["abcUp"] += 1
            if abc_dir == "-": cs["abcDown"] += 1
            if xyz_dir == "+": cs["xyzUp"] += 1
            if xyz_dir == "-": cs["xyzDown"] += 1
            cs["abcMatrix"][f"{abc_with or '–'}|{abc_without or '–'}"] += 1
            cs["xyzMatrix"][f"{xyz_with or '–'}|{xyz_without or '–'}"] += 1
            abc_matrix[f"{abc_with or '–'}|{abc_without or '–'}"] += 1
            xyz_matrix[f"{xyz_with or '–'}|{xyz_without or '–'}"] += 1
        elif present == "with":
            cs["onlyWith"] += 1
        else:
            cs["onlyWithout"] += 1

        items.append({
            "whUuid": warehouse_uuid,
            "itemUuid": item_uuid,
            "warehouse": warehouse_name,
            "warehouseId": warehouse_id,
            "item": item_name,
            "itemId": item_id,
            "vendor": vendor_name,
            "category": category,
            "subcategory": subcategory,
            "abcWith": abc_with.upper() if abc_with else "",
            "xyzWith": xyz_with.upper() if xyz_with else "",
            "abcWithout": abc_without.upper() if abc_without else "",
            "xyzWithout": xyz_without.upper() if xyz_without else "",
            "abcChanged": abc_changed,
            "xyzChanged": xyz_changed,
            "abcDir": abc_dir,
            "xyzDir": xyz_dir,
            "presence": present,
        })

    categories = []
    for cat, cs in cat_stats.items():
        categories.append({
            "category": cat or "(uncategorized)",
            "total": cs["total"],
            "both": cs["both"],
            "onlyWith": cs["onlyWith"],
            "onlyWithout": cs["onlyWithout"],
            "abcChanged": cs["abcChanged"],
            "xyzChanged": cs["xyzChanged"],
            "abcXyzChanged": cs["abcXyzChanged"],
            "abcUp": cs["abcUp"],
            "abcDown": cs["abcDown"],
            "xyzUp": cs["xyzUp"],
            "xyzDown": cs["xyzDown"],
            "abcChangedPct": round(100 * cs["abcChanged"] / cs["both"], 1) if cs["both"] else 0,
            "xyzChangedPct": round(100 * cs["xyzChanged"] / cs["both"], 1) if cs["both"] else 0,
            "abcMatrix": dict(cs["abcMatrix"]),
            "xyzMatrix": dict(cs["xyzMatrix"]),
        })
    categories.sort(key=lambda c: -c["total"])

    items.sort(key=lambda x: (
        not (x["abcChanged"] or x["xyzChanged"]),
        x["category"] or "zzz",
        x["item"] or "",
    ))

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "totalPairs": len(items),
            "matchedBoth": total_both,
            "onlyWith": total_only_with,
            "onlyWithout": total_only_without,
            "abcChanged": total_abc_changed,
            "xyzChanged": total_xyz_changed,
            "anyChanged": total_class_changed,
            "abcUp": total_abc_up,
            "abcDown": total_abc_down,
            "xyzUp": total_xyz_up,
            "xyzDown": total_xyz_down,
            "abcChangedPct": round(100 * total_abc_changed / total_both, 1) if total_both else 0,
            "xyzChangedPct": round(100 * total_xyz_changed / total_both, 1) if total_both else 0,
        },
        "abcBuckets": ABC_BUCKETS,
        "xyzBuckets": XYZ_BUCKETS,
        "abcMatrix": dict(abc_matrix),
        "xyzMatrix": dict(xyz_matrix),
        "categories": categories,
        "items": items,
    }

    with open(out_path, "w") as f:
        json.dump(out, f)
    print(f"Wrote {len(items):,} rows to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
