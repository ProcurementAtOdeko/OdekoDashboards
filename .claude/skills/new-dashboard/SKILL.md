---
name: new-dashboard
description: Scaffold a new Odeko dashboard from a Looker Data Dumps Google Sheet — intake questions, build script, page, hub link, and deploy wiring. Use when the user asks for a new dashboard.
---

# New dashboard

Create a dashboard folder under `dashboards/<slug>/` fed by a Google Sheet
and deployed by the single `deploy.yml` workflow. No per-dashboard workflow
is needed — the deploy job discovers and runs every
`dashboards/*/scripts/build_data.py` automatically.

## 1. Intake (before writing any code)

1. **Find the source file.** Unless the user names a different sheet, search
   the **Looker Data Dumps** Drive folder
   (`parentId = '1kpM0QOi7Wriuk_Xf6uYYR9a6RqMyBCT7'`) by keyword. If several
   files match, list them and ask which one — prefer the most recently
   modified.
2. **Inspect the sheet.** `get_spreadsheet_info(url)` for the tab list, then
   `read_range(url, "Tab!A1:Z6")` to confirm columns and sample data.
3. **Ask the user (one batched `AskUserQuestion` call, skipping anything
   already answered):**
   - Purpose / audience — what decision does this dashboard support?
   - Key metrics / views — KPI cards? Charts? Tables? Which columns?
   - Filters / groupings — warehouse, item, date range, etc.?
   - Refresh cadence — hourly (default) or different?
4. **Confirm the slug** (folder name under `dashboards/`) before scaffolding.
5. **Confirm the sheet is shared** with
   `sheets-mcp-bot@sheets-mcp-497414.iam.gserviceaccount.com`.

## 2. Build script — `dashboards/<slug>/scripts/build_data.py`

- CLI contract: `python3 build_data.py <output.json>` (default `data.json`).
- Use the shared lib; put spreadsheet IDs / ranges / filter values in
  constants at the top:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _lib.sheets import parse_num, read_range, sheets_service

svc = sheets_service()
rows = read_range(svc, SPREADSHEET_ID, "'Tab'!A1:R")
```

Also available in `_lib.sheets`: `fetch_table` (rows + column accessor),
`list_tabs`, `csv_export` (public CSV export, no auth).

- `data.json` is **never committed** — it is gitignored and only exists in
  the deployed Pages artifact.

## 3. Page — `dashboards/<slug>/index.html`

Copy `dashboards/dca1-onhand-eta/index.html` as the design reference and:

- Link shared assets in `<head>`:
  `<link rel="stylesheet" href="../_shared/odeko.css" />` and, if the page
  has charts, `<script src="../_shared/odeko-charts.js"></script>` after the
  Chart.js CDN script. The shared CSS provides the palette variables, base
  typography, and the sticky header (`.brand` / `.logo` / `h1` / `.sub`);
  page-specific CSS goes in the page's own `<style>`.
- Reference the logo as `../_shared/odeko-logo.png`.

## 4. Wire up

1. Add the dashboard to the list in `dashboards/index.html` (the hub page).
2. No workflow changes needed. If the dashboard needs a cadence beyond
   hourly, add a cron entry to `.github/workflows/deploy.yml` with a comment.
3. Verify locally: run the build script once (needs
   `GOOGLE_SERVICE_ACCOUNT_JSON` set), then
   `cd dashboards && python3 -m http.server 8765` and open
   `http://localhost:8765/<slug>/`.
4. Commit and push; the deploy workflow builds the data and publishes.
