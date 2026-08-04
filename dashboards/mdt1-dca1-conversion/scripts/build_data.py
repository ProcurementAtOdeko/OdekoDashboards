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

# The MDT1 customer locations being converted to DCA1.
CUSTOMER_UUIDS = [
    "6ce85566-76a2-4e37-9819-639debd6288b", "5fa6fca8-4063-4855-9425-63939993b892",
    "b5de424c-3b83-4502-9e0e-5b440041da1a", "d8143089-c480-4143-bd2c-a05f54d62cec",
    "d1a0608e-fe4f-4a66-8fa8-cb97767137b5", "4d886efa-7fc9-4085-a8bd-37e6feec253b",
    "8e7f4544-6ea6-4397-bc74-1d71bcda1d1c", "ad13b290-d24f-4713-9c19-87dd12c49766",
    "a400ffa1-815c-4817-96eb-c826898b02fd", "be0f9db5-a0e0-4469-9052-bb8b088ef23b",
    "db120cef-b1fe-48a1-8ab3-516688df5cd7", "314ec033-41c4-428a-a33a-f794f0ad104d",
    "78306611-1468-4cc5-891f-aa1da719167c", "f062df16-1b93-43bc-82ce-10ac9333428b",
    "57dec49c-a9a3-40a4-bfec-ea86abff6fd2", "2c938724-5256-42e4-8c6a-2e7857dcfc71",
    "2c3b698a-7b67-4afc-8dbe-a6a617d4ed5c", "4b59e1bb-d052-4820-8202-53a39ff7c4fd",
    "48e19e54-5515-4f80-a414-c479b69c493f", "14ef1d3d-a95a-4bc0-99a1-c806bf0f44e6",
    "847b9501-2908-48df-8a65-e5e98a75fe3e", "5d8b9264-fcff-4ab3-b49c-0d89be7ce985",
    "7f820124-c597-41b0-9cb7-2d5159a50184", "1bbd0189-7f80-432c-b88f-f33e9ee6680d",
    "4b724839-42d8-4742-b7f8-c3e3f2e1836c", "77e70cf1-25a4-4f5d-8ed9-567809488115",
    "2cdb0293-1e1b-4bc9-b17e-84209f455ee3", "669dfdc6-733a-4240-b1b2-9336b9989f95",
    "7b890e64-9771-4344-ae84-cb3d03d4ecd9", "f910ed38-49df-498e-b3b1-c4ce48758bcc",
    "3e6436c3-8748-4842-85d2-0c54ed97a33a", "966024dc-249b-4340-9c79-891c1b45a0df",
    "b5479af9-506d-449e-ae21-fcfc12623666", "b601db59-206f-4fbf-a10a-e95041f5c855",
    "551e1c3a-69fc-4ec9-8ad1-7216e6d589bb", "dace9b1c-3f61-4e93-a75c-7312ef992a43",
    "afddcebe-179f-49f4-86d8-0643750a0781", "a7746087-0d4b-49a3-8202-8523d14a2c1e",
    "0f33d26d-264d-4e41-8096-99e90ff2bccf", "6efa988c-bcac-4198-ab11-b9771a1beb89",
    "619696e0-1b37-4639-8866-7d88cb188894", "7c3582a4-6f79-4363-a197-43902f3a7a68",
    "7cfb5a6b-d049-4124-bf46-202a0284667f", "074c63cf-744f-4c82-a6e0-ad531181f481",
    "c8041d8f-4427-4624-923d-371da8cbc641", "1544bb63-e61d-422d-86c2-387ff379820e",
    "4b274a8c-e64c-4852-a53b-3c6f3239eecc", "499bcc70-8f24-4fa2-99aa-3fd7335a69dd",
    "efb7e2b9-1162-45c4-8dd3-3e44e010e6d4", "c5a56314-b962-4957-8e08-516db05daf29",
    "8418f15b-ef6c-4243-ac49-a567157b673a", "3c1b6ee9-a019-4abb-bf4b-b0de9ecfc524",
    "6006308b-6ac6-4784-ba1d-5c3bfb014086", "7182a5f1-3811-44ae-8344-138b881ab429",
    "9b9cc65d-6d86-45aa-b3de-f85286caf89e", "fbcbd5a1-958d-4e90-840a-044dbb857916",
    "5983b2f4-e51e-4c43-b1ab-90c401c715f6", "7a6d3a58-3118-4f4a-b72c-4134e17a12c0",
    "fb7f2fd9-4650-449d-9bcc-37230c53cdb0", "37731787-1336-4837-96ab-71e56a841326",
    "fa9f6ea7-5d6b-444f-ab65-53069ca01e26", "b2ac9bdb-f07c-441f-8eb7-7d8793636cdb",
    "391c7681-9a0b-41c1-b6be-52d38c64f9c8", "59edebfc-d2d4-4ca7-b28c-a141e777812c",
    "6b3cdeb7-75e0-4105-88ed-527b39f42c87", "92a3d2d5-b765-46cf-8809-0d0724a03105",
    "36c9263a-f603-4de4-ac7a-b8879aca5e14", "1d4366c4-0a8b-412f-9ca4-401d7510ee02",
    "b6d43d93-6f0a-4548-8a2c-3a802044354f", "2b1dd9f2-bff1-4c6e-bc94-e241bee9e418",
    "5d5e1635-a5fe-4054-9f0f-6aed94048b89", "617c35f2-8993-4d99-bfd7-bccedd929234",
    "23664754-8a14-485d-94ef-fa8b0ef5131b", "ed22a26d-5798-4d5c-864e-84a34ed9bd0a",
    "0ee21cde-237f-4f88-84a8-fe238d9ddabe", "e881d827-7e01-4112-a55a-02bfd2983a18",
    "182b63fe-1cc7-4140-8021-ee67809b970a",
]

_DUP = re.compile(r"^\(DUPLICATE\)\s*", re.I)


def norm_name(name):
    return _DUP.sub("", str(name).strip()).lower()


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
    """Normalized names of SKUs DCA1 already carries (union of 3 signals)."""
    carried = defaultdict(set)  # norm_name -> {source, ...}

    def add(name, source):
        k = norm_name(name)
        if k:
            carried[k].add(source)

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

    return carried


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
    carried = build_carried_set(svc)

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
                    "name": cust_names.get(u, ""),
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
            "name": cust_names.get(uuid, ""),
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
        },
        "coverage": [
            {"status": "Carried in DCA1", "count": len(covered)},
            {"status": "Not carried (gap)", "count": len(gaps)},
        ],
        "topGapSkus": top_gap_skus,
        "topGapBrands": top_gap_brands,
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
