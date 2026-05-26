"""MCP server exposing Google Sheets read/write tools via FastMCP.

Supports two auth modes, picked at runtime:
  1. Service account  - set GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON string)
                        or GOOGLE_SERVICE_ACCOUNT_FILE (path to key file).
                        Headless; required for Claude Code on the web.
  2. OAuth user flow - falls back to ~/.config/google-sheets-mcp/credentials.json
                        and runs a browser flow on first use. For local use only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CONFIG_DIR = Path(
    os.environ.get(
        "GOOGLE_SHEETS_MCP_CONFIG_DIR",
        Path.home() / ".config" / "google-sheets-mcp",
    )
)
TOKEN_PATH = CONFIG_DIR / "token.json"
CLIENT_SECRETS_PATH = Path(
    os.environ.get(
        "GOOGLE_OAUTH_CLIENT_SECRETS",
        CONFIG_DIR / "credentials.json",
    )
)

mcp = FastMCP("google-sheets")
_service = None


def _extract_id(value: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)
    return match.group(1) if match else value


def _service_account_creds():
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if path and Path(path).exists():
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)

    return None


def _oauth_user_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS_PATH.exists():
                raise RuntimeError(
                    "No auth configured. Either set GOOGLE_SERVICE_ACCOUNT_JSON / "
                    "GOOGLE_SERVICE_ACCOUNT_FILE for headless use, or place an OAuth "
                    f"Desktop-app client_secret JSON at {CLIENT_SECRETS_PATH} and "
                    "run `python authorize.py` once."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())

    return creds


def _get_service():
    global _service
    if _service is not None:
        return _service

    creds = _service_account_creds() or _oauth_user_creds()
    _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


@mcp.tool()
def get_spreadsheet_info(spreadsheet: str) -> dict[str, Any]:
    """Return spreadsheet title, URL, and metadata for each tab. Accepts a sheet ID or full URL."""
    sid = _extract_id(spreadsheet)
    meta = _get_service().spreadsheets().get(spreadsheetId=sid, includeGridData=False).execute()
    return {
        "spreadsheetId": meta["spreadsheetId"],
        "title": meta["properties"]["title"],
        "url": meta["spreadsheetUrl"],
        "sheets": [
            {
                "sheetId": s["properties"]["sheetId"],
                "title": s["properties"]["title"],
                "index": s["properties"]["index"],
                "rowCount": s["properties"].get("gridProperties", {}).get("rowCount"),
                "columnCount": s["properties"].get("gridProperties", {}).get("columnCount"),
            }
            for s in meta.get("sheets", [])
        ],
    }


@mcp.tool()
def read_range(spreadsheet: str, range: str) -> dict[str, Any]:
    """Read values from an A1-notation range (e.g. 'Sheet1!A1:D10'). Accepts a sheet ID or URL."""
    sid = _extract_id(spreadsheet)
    res = (
        _get_service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=sid, range=range)
        .execute()
    )
    return {"range": res.get("range"), "values": res.get("values", [])}


@mcp.tool()
def update_range(
    spreadsheet: str, range: str, values: list[list[Any]]
) -> dict[str, Any]:
    """Overwrite values in an A1 range. `values` is a list of rows."""
    sid = _extract_id(spreadsheet)
    return (
        _get_service()
        .spreadsheets()
        .values()
        .update(
            spreadsheetId=sid,
            range=range,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )


@mcp.tool()
def append_rows(
    spreadsheet: str, range: str, values: list[list[Any]]
) -> dict[str, Any]:
    """Append rows to the table whose top-left anchor is `range` (e.g. 'Sheet1!A1')."""
    sid = _extract_id(spreadsheet)
    return (
        _get_service()
        .spreadsheets()
        .values()
        .append(
            spreadsheetId=sid,
            range=range,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )


@mcp.tool()
def clear_range(spreadsheet: str, range: str) -> dict[str, Any]:
    """Clear values in an A1 range without deleting formatting."""
    sid = _extract_id(spreadsheet)
    return (
        _get_service()
        .spreadsheets()
        .values()
        .clear(spreadsheetId=sid, range=range, body={})
        .execute()
    )


@mcp.tool()
def create_sheet(spreadsheet: str, title: str) -> dict[str, Any]:
    """Add a new tab (sheet) to the spreadsheet."""
    sid = _extract_id(spreadsheet)
    return (
        _get_service()
        .spreadsheets()
        .batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        )
        .execute()
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
