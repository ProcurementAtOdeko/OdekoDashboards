# PO Approval — one-time setup

The dashboard reads two sheets and writes approvals via a Google Apps Script
Web App. The read path is automated; the write path needs ~5 minutes of
manual setup the first time.

## 1. Share read access with the service account

Both sheets must be shared (Viewer is enough) with:

```
sheets-mcp-bot@sheets-mcp-497414.iam.gserviceaccount.com
```

- **Combined Models Dump for Dashbaord** — `1sPEc5rBdRB9qaJijBh4z8DK4ZVo--5xmTGbPTZ5n2nQ`
- **PO Approval Log** — `19kWUzVFHTzVDb64fU9r83oaKmbO8rgEaNUJG6r2oyOk` (created in `Looker Data Dumps`)

The first is already shared (the DCA1 dashboards use it). Share the second
manually now.

## 2. Deploy the Apps Script Web App

1. Open the **PO Approval Log** sheet:
   <https://docs.google.com/spreadsheets/d/19kWUzVFHTzVDb64fU9r83oaKmbO8rgEaNUJG6r2oyOk/edit>
2. Extensions → Apps Script.
3. Paste the contents of `scripts/approvals_apps_script.gs` into `Code.gs`. Save.
4. Deploy → New deployment → Type: **Web app**.
   - Description: `PO approval write endpoint`
   - Execute as: **Me** (your Odeko account)
   - Who has access: **Anyone with the link**
5. Click Deploy, authorize the script (it needs Sheets write access on your
   behalf), copy the **Web app URL** that ends in `/exec`.

## 3. Wire the Web App URL into the dashboard

Edit `dashboards/po-approval/index.html` and set `APPS_SCRIPT_URL` near the
top of the `<script>` block to the URL you copied. Commit and push to main.

## 4. Trigger the first data refresh

```
gh workflow run refresh-po-approval.yml
```

…or just push to main; the workflow runs hourly at `:25`.

## Notes / caveats

- "Anyone with the link" means anyone who can guess or share the URL can
  POST approvals. This is the same trust model the existing dashboards
  use for *reading* — fine for an internal procurement tool, not fine for
  anything sensitive. If we ever need real auth, swap to a Cloud Function
  with IAP / OAuth.
- The dashboard prompts for an approver email on first use and stores it
  in `localStorage`. It's recorded with every approval but is not verified.
- The build job reads the **whole** approval log each refresh and keeps
  the latest entry per (warehouse, item_uuid) — so re-approving overwrites
  the prior state. Older entries stay in the sheet as an audit trail.
