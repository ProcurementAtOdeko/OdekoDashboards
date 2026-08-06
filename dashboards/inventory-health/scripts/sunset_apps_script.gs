/**
 * Sunset Review Log — Web App backend.
 *
 * Deploy as a Web App (Extensions → Apps Script → Deploy → New deployment →
 * Type: Web app):
 *   - Execute as: Me
 *   - Who has access: Anyone with the link
 * Bind it to the "Sunset Review Log" spreadsheet. See SETUP.md.
 *
 * The log is append-only: every decision adds a row and nothing is ever
 * overwritten, so the sheet doubles as the audit trail. build_data.py reads
 * the whole log each refresh and keeps the newest entry per
 * (warehouse, item_id).
 *
 * Accepts a batch so importing a reviewed workbook is ONE request. Appending
 * a few hundred rows one call at a time takes minutes and routinely trips the
 * script timeout.
 *
 * POST body: {"decisions": [{warehouse, item_id, item_name, decision,
 *                            decided_by, notes}, ...]}
 * Returns:   {"ok": true, "written": n} or {"ok": false, "error": "..."}
 */

var SHEET_ID = "PASTE_THE_SUNSET_REVIEW_LOG_SPREADSHEET_ID_HERE";
var TAB_NAME = "Sunset Review Log";
var HEADERS = ["timestamp_utc", "decided_by", "warehouse", "item_id",
               "item_name", "decision", "notes"];
var MAX_BATCH = 2000;

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) throw new Error("empty request body");
    var body = JSON.parse(e.postData.contents);

    // Accept either a batch or a single decision.
    var incoming = body.decisions || [body];
    if (!incoming.length) throw new Error("no decisions supplied");
    if (incoming.length > MAX_BATCH) throw new Error("batch too large: " + incoming.length);

    var now = new Date().toISOString();
    var rows = incoming.map(function (d) {
      if (!d.warehouse) throw new Error("missing warehouse");
      if (!d.item_id) throw new Error("missing item_id");
      var decision = String(d.decision || "").toUpperCase();
      if (decision !== "CONFIRM" && decision !== "KEEP") {
        throw new Error("bad decision: " + d.decision);
      }
      return [
        now,
        String(d.decided_by || "").slice(0, 200),
        String(d.warehouse).slice(0, 50),
        String(d.item_id).slice(0, 100),
        String(d.item_name || "").slice(0, 300),
        decision,
        String(d.notes || "").slice(0, 1000),
      ];
    });

    var sheet = getSheet_();
    // One setValues call rather than appendRow per decision.
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, HEADERS.length)
         .setValues(rows);

    return json_({ ok: true, written: rows.length });
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message || err) });
  }
}

function doGet() {
  return json_({ ok: true, hint: "POST {\"decisions\":[...]} here" });
}

function getSheet_() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var sheet = ss.getSheetByName(TAB_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(TAB_NAME);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
