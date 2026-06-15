/**
 * PO Approval Log — Web App backend.
 *
 * Deploy as Web App (Manage Deployments → New Deployment → Type: Web App):
 *   - Execute as: Me
 *   - Who has access: Anyone with the link
 * Bind to the "PO Approval Log" spreadsheet (id 19kWUzVFHTzVDb64fU9r83oaKmbO8rgEaNUJG6r2oyOk).
 *
 * Each POST appends one row. Columns must match build_data.py / SETUP.md:
 *   timestamp_utc, approver_email, warehouse, item_uuid, item_name, vendor,
 *   purchase_unit, recommended_qty, approved_qty, status, notes
 *
 * Returns JSON: {ok: true} or {ok: false, error: "..."}.
 */

var SHEET_ID = "19kWUzVFHTzVDb64fU9r83oaKmbO8rgEaNUJG6r2oyOk";
var TAB_NAME = "PO Approval Log"; // change if you rename the tab in the spreadsheet

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var required = ["approver_email", "warehouse", "item_uuid", "status"];
    for (var i = 0; i < required.length; i++) {
      if (!body[required[i]]) throw new Error("missing " + required[i]);
    }
    var allowed = {approved: 1, skipped: 1, edited: 1, reset: 1};
    if (!allowed[body.status]) throw new Error("bad status: " + body.status);

    var ss = SpreadsheetApp.openById(SHEET_ID);
    var sheet = ss.getSheetByName(TAB_NAME) || ss.getSheets()[0];
    sheet.appendRow([
      new Date().toISOString(),
      String(body.approver_email).slice(0, 200),
      String(body.warehouse).slice(0, 50),
      String(body.item_uuid).slice(0, 100),
      String(body.item_name || "").slice(0, 300),
      String(body.vendor || "").slice(0, 300),
      String(body.purchase_unit || "").slice(0, 50),
      body.recommended_qty != null ? Number(body.recommended_qty) : "",
      body.approved_qty != null ? Number(body.approved_qty) : "",
      body.status,
      String(body.notes || "").slice(0, 1000),
    ]);
    return ContentService
      .createTextOutput(JSON.stringify({ok: true}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ok: false, error: String(err)}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet() {
  return ContentService
    .createTextOutput(JSON.stringify({ok: true, hint: "POST approvals here"}))
    .setMimeType(ContentService.MimeType.JSON);
}
