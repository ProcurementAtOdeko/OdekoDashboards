# Inventory Health Scorecard — setup

The read path is fully automated: `scripts/build_data.py` pulls nine sheets
with the service account and the hourly workflow commits the result. Nothing
to do there.

The **write** path — saving the team's sunset decisions so everyone sees the
same state — needs about five minutes of one-time setup, because a static page
on GitHub Pages has no backend of its own. This is the same mechanism the PO
Approval dashboard uses.

Until it's wired up the dashboard still works: decisions save to the browser's
`localStorage` and the Sunset Review tab says so plainly. They just don't
travel between people or machines.

---

## 1. Create the log spreadsheet

1. Create a new Google Sheet called **Sunset Review Log** in the
   `Looker Data Dumps` folder.
2. Rename the first tab to **Sunset Review Log**.
3. Share it (Viewer is enough) with the service account:

   ```
   sheets-mcp-bot@sheets-mcp-497414.iam.gserviceaccount.com
   ```

   The build reads the log back so decisions survive a browser wipe and show
   up for everyone.
4. Copy the spreadsheet ID out of its URL — the long string between `/d/` and
   `/edit`.

## 2. Deploy the Web App

1. In that sheet: **Extensions → Apps Script**.
2. Paste the contents of `scripts/sunset_apps_script.gs` into `Code.gs`.
3. Set `SHEET_ID` at the top to the ID from step 1. Save.
4. **Deploy → New deployment → Type: Web app**
   - Description: `Sunset review write endpoint`
   - Execute as: **Me**
   - Who has access: **Anyone with the link**
5. Deploy, authorise the script when prompted, and copy the **Web app URL**
   (it ends in `/exec`).

## 3. Point the dashboard at it

In `app.js`, set the constant near the top:

```js
const APPS_SCRIPT_URL = "https://script.google.com/macros/s/…/exec";
```

Commit and push to `main`.

## 4. Let the build read the log

Add the spreadsheet ID from step 1 as a repository variable named
`SUNSET_REVIEW_LOG_ID` (Settings → Secrets and variables → Actions →
Variables). The refresh workflow passes it through to `build_data.py`.

Without it the dashboard still *writes* decisions to the sheet, but the build
won't fold them back into the published data — so a teammate on a different
machine wouldn't see them.

---

## How the loop runs

1. Pick a warehouse, open **Sunset Review**. Every SKU the engine flagged is
   listed, newest decisions included.
2. Either click a row and record the call in the dashboard, or hit **Export
   review file**, circulate the workbook, and **Import reviewed file** when it
   comes back. The Decision column is a CONFIRM/KEEP dropdown; the importer
   also accepts the obvious synonyms and leaves anything blank as pending.
3. **CONFIRM** keeps the item at SUNSET and flags it as agreed. **KEEP** moves
   it to WATCH, carrying the reviewer's name and note into the tooltips.
4. **Export confirmed kill list** produces the optimisation hand-off: item,
   quantity on hand, on-hand cost, revenue at risk, customers affected, who
   decided and why.

Decisions are written to the shared log immediately and mirrored locally so
the UI updates without waiting. A dot beside a decision means it is saved
locally but hasn't reached the log yet; it retries on the next write.

## Caveats

- "Anyone with the link" means anyone who has the URL can POST a decision.
  That is the same trust model the other internal dashboards use, and is fine
  for a procurement tool — but it is not authentication. The reviewer name is
  recorded, not verified.
- The build folds the log in on its schedule, so a decision recorded now
  reaches other people's browsers at the next refresh. Your own browser shows
  it immediately.
- Nothing is ever deleted from the log. "Clear decision" in the UI drops the
  local override; to retract one that has already synced, record the opposite
  decision — the newest entry per item wins.

## Known data gap

The **First Fulfillment Ledger** export
(`1xQ4up0z56zvCKZH1g5fLpbgv2R1rFRFt6GL-6kUFUlE`) is currently empty, so no SKU
can be aged and **New To Market** is never assigned. The build warns on stderr
and records `daysActiveAvailable: false` in `data/manifest.json`; the header
shows a data-gap note. `overstock-mitigation` and `forecasting` read the same
export and are degraded the same way. Re-running the Looker export fixes all
three at once — no code change needed.
