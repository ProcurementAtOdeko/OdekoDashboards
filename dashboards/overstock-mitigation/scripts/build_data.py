#!/usr/bin/env python3
"""Build data.json for the Overstock Transfer Mitigation dashboard.

Takes the overstock model (historic-sales burn + on-hand + expiration, per
market) and answers: for stock we're long on, where can we MOVE it so it
actually sells before it expires?

Scope rules (from the brief):
  * omit SKUs less than 120 days old   -> age = today - first-fulfillment date
                                          for that (warehouse, item), from the
                                          First Fulfillment Ledger export
  * omit refrigerated SKUs             -> cold-chain can't be freely transferred
  * everything else that's overstock   -> find transfer destinations
  * try to move 120 days before MDSL   -> a lot's last shippable day is
                                          expiration - MDSL; we want >=120 days
                                          of runway before that, else it's
                                          urgent / past-MDSL

A destination market qualifies if it already sells the SKU AND is under the
180-DOC target (real room to absorb), OR simply sells it faster than the donor
market. Absorb capacity = 180-day target demand minus its current on-hand;
the donor's excess units are allocated greedily across destinations by
capacity.

Inventory, sales-burn (with the live-export -> sales-tracker-snapshot source
chain) and the models-dump parsing are imported from the sibling overstock
build so the two dashboards stay in lockstep.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- Reuse the overstock build's data layer ---------------------------------
_OV_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "overstock", "scripts", "build_data.py"))
_spec = importlib.util.spec_from_file_location("overstock_build", _OV_PATH)
ov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ov)

# --- Model knobs -------------------------------------------------------------
TARGET_DOC = 180           # destination room measured against a 180-day target
AGE_MIN_DAYS = 120         # omit SKUs first fulfilled within this many days
MDSL_LEAD_DAYS = 120       # want this much runway before a lot's MDSL deadline
MAX_RECEIVERS = 8          # destinations listed per donor SKU

FFL_SPREADSHEET_ID = "1xQ4up0z56zvCKZH1g5fLpbgv2R1rFRFt6GL-6kUFUlE"
FFL_RANGE = "'First Fulfillment Ledger.csv'!A1:H"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

parse_num = ov.parse_num
parse_date = ov.parse_date
iso = ov.iso


def is_cold(refrig):
    r = (refrig or "").lower()
    return any(k in r for k in ("refriger", "frozen", "cold", "chill"))


def burn_for_warehouse(sheets, wh, file_ids):
    """(burn dict, sales_through date, source) via live exports then the
    sales-tracker snapshot. Empty dict if nothing usable."""
    for fid in file_ids:
        try:
            rows = (sheets.spreadsheets().values()
                    .get(spreadsheetId=fid, range=ov.SALES_RANGE).execute()
                    ).get("values", [])
            if not rows:
                raise ValueError("empty")
            burn, _buyers, smax = ov.sales_burn_for_warehouse(rows, wh)
            return burn, smax, "live"
        except Exception:
            continue
    snap = ov.snapshot_burn_for_warehouse(wh)
    if snap:
        burn, _buyers, end = snap
        return burn, end, "snapshot"
    return {}, None, None


def load_first_fulfillment(sheets):
    """{(warehouse, uuid): first_fulfillment_date}."""
    rows = (sheets.spreadsheets().values()
            .get(spreadsheetId=FFL_SPREADSHEET_ID, range=FFL_RANGE).execute()
            ).get("values", [])
    if not rows:
        return {}
    col = {n: i for i, n in enumerate(rows[0])}
    need = ["Item Extid", "Warehouse Name", "Min Date"]
    if any(c not in col for c in need):
        print(f"First Fulfillment Ledger missing {need}; ages unavailable", file=sys.stderr)
        return {}
    out = {}
    for r in rows[1:]:
        r = r + [""] * (len(rows[0]) - len(r))
        uuid = r[col["Item Extid"]].strip()
        wh = r[col["Warehouse Name"]].strip()
        d = parse_date(r[col["Min Date"]])
        if uuid and wh and d:
            key = (wh, uuid)
            if key not in out or d < out[key]:
                out[key] = d
    return out


def main(out_path):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    inv_by_wh, as_of = ov.load_inventory(sheets)
    today = as_of or datetime.now(timezone.utc).date()
    first_ff = load_first_fulfillment(sheets)
    sources = ov.discover_sales_sources(drive)
    if not sources:
        sys.exit("No sales tracker sources found")

    # Per-market burn + provenance, and a full cross-market position index so
    # any market can be evaluated as a transfer destination.
    burn_by_wh, meta_by_wh = {}, {}
    for wh in sorted(sources):
        if wh not in inv_by_wh:
            continue
        burn, smax, source = burn_for_warehouse(sheets, wh, sources[wh])
        if source is None:
            print(f"{wh}: no sales; skipped", file=sys.stderr)
            continue
        burn_by_wh[wh] = burn
        stale = bool(smax and (today - smax).days > ov.STALE_AFTER_DAYS)
        meta_by_wh[wh] = {"salesThrough": iso(smax), "source": source, "stale": stale}

    # position[uuid][wh] = full picture used for donor + receiver logic
    position = defaultdict(dict)
    for wh, recs in inv_by_wh.items():
        if wh not in burn_by_wh:
            continue
        burn = burn_by_wh[wh]
        for rec in recs:
            b = burn.get(rec["uuid"], 0.0)
            on_hand = rec["onHandPU"]
            doc = None if b <= 0 else on_hand / b
            position[rec["uuid"]][wh] = {
                "burn": b, "onHand": on_hand, "doc": doc,
                "cost": rec["costPerPU"], "cls": rec["cls"], "refrig": rec["refrig"],
                "name": rec["name"], "vendor": rec["vendor"],
                "expDate": rec["expDate"], "expQty": rec["expQty"],
                "mdslDays": rec["mdslDays"],
                "ageDays": (today - first_ff[(wh, rec["uuid"])]).days
                           if (wh, rec["uuid"]) in first_ff else None,
            }

    markets = {}
    dest_inbound = defaultdict(lambda: {"units": 0.0, "value": 0.0})
    for uuid, by_wh in position.items():
        for wh, p in by_wh.items():
            no_sales = p["burn"] <= 0
            overstock = no_sales or (p["doc"] is not None and p["doc"] > TARGET_DOC)
            if not overstock:
                continue
            if is_cold(p["refrig"]):
                continue
            if p["ageDays"] is not None and p["ageDays"] < AGE_MIN_DAYS:
                continue  # too new to judge / act on

            excess = p["onHand"] if no_sales else max(0.0, p["onHand"] - TARGET_DOC * p["burn"])
            if excess <= 0:
                continue
            cost = p["cost"]

            # MDSL runway: last shippable day = expiration - MDSL.
            days_to_deadline = None
            if p["expDate"] is not None and p["mdslDays"] is not None:
                deadline = p["expDate"].toordinal() - int(p["mdslDays"])
                days_to_deadline = deadline - today.toordinal()
            past_mdsl = days_to_deadline is not None and days_to_deadline < 0

            # Past MDSL can't be sold to a customer even after a transfer, so
            # it's markdown/donate — no destinations, no allocation.
            receivers = []
            transfer_units = 0.0
            if not past_mdsl:
                # Candidate destinations: other markets that sell it and are
                # under-covered, OR simply move it faster than here.
                for owh, op in by_wh.items():
                    if owh == wh or op["burn"] <= 0:
                        continue
                    under = op["doc"] is not None and op["doc"] < TARGET_DOC
                    faster = op["burn"] > p["burn"]
                    if not (under or faster):
                        continue
                    absorb = max(0.0, TARGET_DOC * op["burn"] - op["onHand"])
                    receivers.append({
                        "mkt": owh, "burn": round(op["burn"], 3),
                        "doc": round(op["doc"], 1) if op["doc"] is not None else None,
                        "onHand": round(op["onHand"], 1),
                        "absorb": absorb, "under": under, "faster": faster,
                    })
                # Allocate excess greedily across the roomiest destinations first.
                receivers.sort(key=lambda x: (-x["absorb"], -x["burn"]))
                remaining = excess
                for r in receivers:
                    take = min(remaining, r["absorb"]) if r["absorb"] > 0 else 0.0
                    r["take"] = round(take, 1)
                    r["absorb"] = round(r["absorb"], 1)
                    remaining -= take
                    if take > 0:
                        dest_inbound[r["mkt"]]["units"] += take
                        if cost is not None:
                            dest_inbound[r["mkt"]]["value"] += take * cost
                transfer_units = excess - remaining
                receivers = [r for r in receivers if r["take"] > 0 or r["faster"]][:MAX_RECEIVERS]

            if past_mdsl:
                action, timing = "MARKDOWN / DONATE", "past-mdsl"
            elif transfer_units > 0:
                action = "TRANSFER"
                timing = ("comfortable" if days_to_deadline is None or days_to_deadline >= MDSL_LEAD_DAYS
                          else "urgent")
            else:
                action, timing = "NO DESTINATION", "none"

            m = markets.setdefault(wh, {"code": wh, **meta_by_wh[wh], "items": []})
            m["items"].append({
                "uuid": uuid, "name": p["name"], "vendor": p["vendor"], "cls": p["cls"],
                "onHandPU": round(p["onHand"], 1),
                "burn": round(p["burn"], 3),
                "doc": round(p["doc"], 1) if p["doc"] is not None else None,
                "noSales": no_sales,
                "excessUnits": round(excess, 1),
                "excessValue": round(excess * cost, 2) if cost is not None else None,
                "expDate": iso(p["expDate"]),
                "mdslDays": int(p["mdslDays"]) if p["mdslDays"] is not None else None,
                "daysToDeadline": days_to_deadline,
                "ageDays": p["ageDays"],
                "action": action, "timing": timing,
                "transferUnits": round(transfer_units, 1),
                "transferValue": round(transfer_units * cost, 2) if cost is not None else None,
                "receivers": receivers,
            })

    def kpis(items):
        tr = [i for i in items if i["action"] == "TRANSFER"]
        return {
            "donorSkus": len(items),
            "transferSkus": len(tr),
            "transferUnits": round(sum(i["transferUnits"] for i in tr), 1),
            "transferValue": round(sum(i["transferValue"] or 0 for i in tr), 2),
            "noDestSkus": sum(1 for i in items if i["action"] == "NO DESTINATION"),
            "pastMdslSkus": sum(1 for i in items if i["action"] == "MARKDOWN / DONATE"),
            "urgentSkus": sum(1 for i in tr if i["timing"] == "urgent"),
        }

    market_list = []
    for wh in sorted(markets):
        m = markets[wh]
        # Worst first: transferable value desc, then excess value.
        m["items"].sort(key=lambda x: (-(x["transferValue"] or 0), -(x["excessValue"] or 0)))
        m["kpis"] = kpis(m["items"])
        market_list.append(m)

    all_items = [i for m in market_list for i in m["items"]]
    network = kpis(all_items)
    network["marketsCount"] = len(market_list)

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asOf": iso(today),
        "docThreshold": TARGET_DOC,
        "ageMinDays": AGE_MIN_DAYS,
        "mdslLeadDays": MDSL_LEAD_DAYS,
        "salesWindowDays": ov.SALES_WINDOW_DAYS,
        "network": network,
        "markets": market_list,
        "destinations": sorted(
            ({"code": k, "inboundUnits": round(v["units"], 1),
              "inboundValue": round(v["value"], 2)} for k, v in dest_inbound.items()),
            key=lambda x: -x["inboundValue"]),
    }
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {len(market_list)} markets, {network['transferSkus']} transferable SKUs "
          f"(${network['transferValue']:,.0f}), {network['noDestSkus']} no-destination, "
          f"{network['pastMdslSkus']} past-MDSL to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data.json")
