"""Shared Google Sheets helpers for dashboard build scripts.

Build scripts add the dashboards/ directory to sys.path and import this:

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from _lib import sheets

    svc = sheets.sheets_service()
    rows = sheets.read_range(svc, SPREADSHEET_ID, "'Tab'!A1:R")
"""

from __future__ import annotations

import json
import math
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def sheets_service():
    """Sheets API client authed from the GOOGLE_SERVICE_ACCOUNT_JSON env var."""
    # Imported lazily so CSV-only build scripts don't need the Google libs.
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    # httplib2 doesn't read SSL_CERT_FILE / REQUESTS_CA_BUNDLE on its own;
    # honor a custom CA bundle when the runtime sets one (e.g. sandboxed envs).
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca:
        from google_auth_httplib2 import AuthorizedHttp

        http = AuthorizedHttp(creds, http=httplib2.Http(ca_certs=ca))
        return build("sheets", "v4", http=http, cache_discovery=False)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_range(svc, spreadsheet_id, rng):
    """Raw rows (list of lists) for a range; [] when the range is empty."""
    return (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
        .get("values", [])
    )


def fetch_table(svc, spreadsheet_id, rng):
    """Data rows plus a get(row, column_name) accessor; exits if empty."""
    rows = read_range(svc, spreadsheet_id, rng)
    if not rows:
        sys.exit(f"{spreadsheet_id} returned no rows for {rng}")
    col = {name: i for i, name in enumerate(rows[0])}

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    return rows[1:], get


def list_tabs(svc, spreadsheet_id):
    """Tab titles of a spreadsheet, in sheet order."""
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return [s["properties"]["title"] for s in meta["sheets"]]


def csv_export(spreadsheet_id, gid):
    """CSV text of a link-shared sheet tab via the public export endpoint."""
    # httplib2 rather than urllib: it copes with proxies and large chunked
    # responses, and is already a google-api-python-client dependency.
    import httplib2

    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    http = httplib2.Http(ca_certs=ca) if ca else httplib2.Http()
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    resp, content = http.request(url, "GET")
    if resp.status != 200 or not content:
        sys.exit(
            f"CSV export failed for {spreadsheet_id} gid={gid}: HTTP {resp.status}"
        )
    return content.decode("utf-8")


def parse_num(s):
    """Lenient float parse: strips $ and thousands separators; None on failure."""
    if s is None or s == "":
        return None
    try:
        f = float(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None
