/* Inventory Health Scorecard — front-end.
 *
 * Items arrive already joined and scored from scripts/build_data.py; this file
 * renders them, aggregates brands and categories on the fly (so a team
 * override ripples through immediately), and runs the sunset review loop.
 *
 * Decisions are written to an append-only Google Sheet through an Apps Script
 * Web App, the same mechanism the PO Approval dashboard uses. They are also
 * mirrored into localStorage so the UI reacts instantly rather than waiting
 * for the next scheduled build to fold them in.
 */

/* Set this to the /exec URL after deploying scripts/sunset_apps_script.gs.
 * Until then decisions persist locally only and the UI says so. */
const APPS_SCRIPT_URL = "";

const LOCAL_KEY = wh => `inventoryHealth.review.${wh}`;
const NAME_KEY = "inventoryHealth.reviewer";

const S = {
  manifest: null,
  wh: null,
  data: null,
  items: [],
  local: {},          // itemId -> {decision, by, note, at, synced}
  customers: null,    // sales-tracker index for the current warehouse
  customersFor: null,
  view: "dashboard",
  charts: {},
  sorts: {},
  pending: null,      // item awaiting a decision in the modal
};

/* ------------------------------------------------------------ formatting */

const nf = new Intl.NumberFormat("en-US");
const int = v => (v == null || Number.isNaN(v) ? "—" : nf.format(Math.round(v)));
const dec = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : v.toFixed(d));
const money = v => {
  if (v == null || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
};
const money2 = v => (v == null || Number.isNaN(v) ? "—" : `$${nf.format(Number(v.toFixed(2)))}`);
const percent = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(d)}%`);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const recTag = r => {
  if (!r) return '<span class="muted">—</span>';
  const cls = r === "KEEP" ? "keep" : r === "WATCH" ? "watch" : "sunset";
  return `<span class="tag ${cls}">${esc(r)}</span>`;
};
const statusTag = s => {
  if (s === "New To Market") return '<span class="tag new">NEW</span>';
  if (s === "No Data") return '<span class="tag nodata">NO DATA</span>';
  if (s === "Inactive") return '<span class="tag nodata">Inactive</span>';
  return esc(s || "—");
};
const classTag = c => (c ? `<span class="tag ${c.toLowerCase()}">${c}</span>` : '<span class="muted">—</span>');

/* DIO of exactly the cap means "stock on hand, no sales signal at all" rather
 * than a real 9,999-day figure, so label it instead of printing the number. */
const dioCell = v => (v >= 9999 ? '<span class="tag sunset" title="Stock on hand with no sales signal">DEAD</span>' : int(v));

/* -------------------------------------------------------------- review state */

function reviewOf(item) {
  return S.local[item.id] || item.review || null;
}

/* The recommendation actually shown: score-derived, then the team's override. */
function recOf(item) {
  if (item.status !== "Active") return "";
  const review = reviewOf(item);
  if (review && review.decision === "KEEP") return "WATCH";
  return item.scoredRecommendation || item.recommendation || "";
}

function isSunsetCandidate(item) {
  // Anything the engine flagged, plus anything already ruled on, so a kept
  // item doesn't vanish from the review list the moment it's kept.
  return item.status === "Active"
    && (item.scoredRecommendation === "SUNSET" || reviewOf(item));
}

function loadLocal(wh) {
  try { S.local = JSON.parse(localStorage.getItem(LOCAL_KEY(wh)) || "{}"); }
  catch { S.local = {}; }
}
function saveLocal() {
  try { localStorage.setItem(LOCAL_KEY(S.wh), JSON.stringify(S.local)); } catch { /* quota */ }
}

async function postDecisions(rows) {
  if (!APPS_SCRIPT_URL) return false;
  try {
    // text/plain dodges the CORS preflight Apps Script won't answer.
    const res = await fetch(APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({ decisions: rows }),
    });
    const out = await res.json();
    return out && out.ok === true;
  } catch {
    return false;
  }
}

async function recordDecision(items, decision, by, note) {
  const at = new Date().toISOString();
  const rows = items.map(item => ({
    warehouse: S.wh,
    item_id: item.id,
    item_name: item.name,
    decision,
    decided_by: by,
    notes: note,
  }));
  items.forEach(item => {
    S.local[item.id] = { decision, by, note, at, synced: false };
  });
  saveLocal();
  render();

  const ok = await postDecisions(rows);
  if (ok) {
    items.forEach(item => { if (S.local[item.id]) S.local[item.id].synced = true; });
    saveLocal();
    render();
  }
  return ok;
}

function clearDecision(item) {
  delete S.local[item.id];
  saveLocal();
  render();
}

/* ------------------------------------------------------------------ loading */

async function loadManifest() {
  const res = await fetch("data/manifest.json");
  if (!res.ok) throw new Error(`manifest: HTTP ${res.status}`);
  S.manifest = await res.json();
}

async function loadWarehouse(wh) {
  const res = await fetch(`data/${encodeURIComponent(wh)}.json`);
  if (!res.ok) throw new Error(`${wh}: HTTP ${res.status}`);
  S.data = await res.json();
  S.items = S.data.items;
  S.wh = wh;
  loadLocal(wh);
  S.customers = null;
  S.customersFor = null;
}

/* Named customers come from the Sales Tracker dashboard's exports, which are
 * already built per warehouse. Loaded lazily: it's only needed when someone
 * actually opens an item, and EWR1's file is several MB. */
async function loadCustomers(wh) {
  if (S.customersFor === wh) return S.customers;
  S.customersFor = wh;
  S.customers = null;
  try {
    const res = await fetch(`../sales-tracker/data/${encodeURIComponent(wh)}.json`);
    if (!res.ok) return null;
    const raw = await res.json();
    // `pairs` is one row per item x customer, with c/i as indices into the
    // customers and items arrays. Join on item NAME: the tracker keys items
    // by their own uuid, which the NetSuite-derived scorecard doesn't carry.
    const customers = raw.customers || [];
    const items = raw.items || [];
    const byItem = new Map();
    (raw.pairs || []).forEach(p => {
      const item = items[p.i];
      const customer = customers[p.c];
      if (!item || !customer) return;
      const key = String(item.name || "").trim().toLowerCase();
      if (!key) return;
      let list = byItem.get(key);
      if (!list) byItem.set(key, (list = []));
      list.push({
        name: customer.name || customer.uuid,
        enterprise: !!customer.enterprise,
        units: p.units || 0,
        lines: p.lines || 0,
        lastOrder: p.lastOrder || "",
      });
    });
    byItem.forEach(list => list.sort((a, b) => b.units - a.units));
    S.customers = byItem;
  } catch {
    S.customers = null;
  }
  return S.customers;
}

/* ------------------------------------------------------------- aggregation */

function activeItems() {
  return S.items.filter(i => i.status === "Active" || i.status === "New To Market");
}

function buildBrands() {
  // Keyed on brand alone. Keying on brand+category splits a brand that spans
  // categories into several thin rows, each of which then looks like a
  // one-SKU brand to the exit rules.
  const map = new Map();
  activeItems().forEach(item => {
    let b = map.get(item.brand);
    if (!b) {
      b = {
        brand: item.brand || "—", skus: 0, revenue: 0, onHandCost: 0,
        a: 0, b: 0, c: 0, x: 0, z: 0,
        sumNetRev: 0, sumCostAmt: 0, sumAvgInv: 0, sumOutput: 0,
        sunsetSkus: 0, sunsetRevenue: 0, sunsetCustomers: 0,
        categories: new Map(),
      };
      map.set(item.brand, b);
    }
    b.skus++;
    b.revenue += item.revenue12mo;
    b.onHandCost += item.onHandCost;
    if (item.catABC === "A") b.a++; else if (item.catABC === "B") b.b++; else if (item.catABC === "C") b.c++;
    if (item.xyzClass === "X") b.x++; else if (item.xyzClass === "Z") b.z++;
    if (item.netRevenue > 0) { b.sumNetRev += item.netRevenue; b.sumCostAmt += item.costAmount; }
    b.sumAvgInv += item.avgInventory;
    b.sumOutput += item.outputValue;
    b.categories.set(item.category, (b.categories.get(item.category) || 0) + 1);
    if (recOf(item) === "SUNSET") {
      b.sunsetSkus++;
      b.sunsetRevenue += item.revenue12mo;
      b.sunsetCustomers += item.customers;
    }
  });

  return [...map.values()].map(b => {
    const pctSunset = b.skus ? b.sunsetSkus / b.skus : 0;
    const retained = b.revenue - b.sunsetRevenue;
    const retainedPct = b.revenue > 0 ? retained / b.revenue : 0;
    // Blended, not an average of per-item margins: a low-volume outlier
    // otherwise moves the brand's margin as much as its best seller.
    const margin = b.sumNetRev > 0 ? (b.sumNetRev - b.sumCostAmt) / b.sumNetRev : null;
    const dio = b.sumAvgInv > 0
      ? (b.sumCostAmt > 0 ? b.sumAvgInv * 182 / b.sumCostAmt
        : b.sumOutput > 0 ? b.sumAvgInv * 30 / b.sumOutput : null)
      : null;
    const gmroi = b.sumAvgInv > 0 && b.sumCostAmt > 0
      ? (b.sumNetRev - b.sumCostAmt) / b.sumAvgInv : null;

    let categories = [...b.categories.entries()].sort((x, y) => y[1] - x[1]);
    const category = categories.length > 1 ? `${categories[0][0]} +${categories.length - 1}` : (categories[0]?.[0] || "—");

    const rec =
      b.revenue < 1000 ? "EXIT BRAND"
        : (pctSunset >= 0.6 && b.revenue < 50000) ? "EXIT BRAND"
          // A brand with no category-leading SKU is only an exit candidate if
          // it is also small; plenty of solid B-class brands never lead.
          : (b.a === 0 && b.revenue < 20000) ? "EXIT BRAND"
            : (retainedPct > 0 && dio != null && dio > 150) ? "MOQ REVIEW"
              : (pctSunset >= 0.25 && b.a > 0) ? "TRIM TAIL"
                : (b.revenue > 200000 && b.a > 0) ? "KEEP — Strategic"
                  : "MONITOR";

    return { ...b, category, pctSunset, retained, retainedPct, margin, dio, gmroi, recommendation: rec };
  }).sort((x, y) => y.revenue - x.revenue);
}

function buildCategories() {
  const map = new Map();
  activeItems().forEach(item => {
    let c = map.get(item.category);
    if (!c) {
      c = {
        category: item.category || "—", skus: 0, revenue: 0, onHandCost: 0,
        sumAvgInv: 0, sumCostAmt: 0, sumNetRev: 0, sumOutput: 0,
        keep: 0, watch: 0, sunset: 0,
      };
      map.set(item.category, c);
    }
    c.skus++;
    c.revenue += item.revenue12mo;
    c.onHandCost += item.onHandCost;
    c.sumAvgInv += item.avgInventory;
    c.sumCostAmt += item.costAmount;
    c.sumNetRev += item.netRevenue;
    c.sumOutput += item.outputValue;
    const r = recOf(item);
    if (r === "KEEP") c.keep++; else if (r === "WATCH") c.watch++; else if (r === "SUNSET") c.sunset++;
  });

  return [...map.values()].map(c => ({
    ...c,
    // Ratio of the sums, never the mean of per-item ratios — those are not
    // the same number and the mean is dominated by tiny SKUs.
    dio: c.sumAvgInv > 0
      ? (c.sumCostAmt > 0 ? c.sumAvgInv * 182 / c.sumCostAmt
        : c.sumOutput > 0 ? c.sumAvgInv * 30 / c.sumOutput : null)
      : null,
    ito: c.sumAvgInv > 0
      ? (c.sumCostAmt > 0 ? c.sumCostAmt * 2 / c.sumAvgInv
        : c.sumOutput > 0 ? c.sumOutput * 12 / c.sumAvgInv : null)
      : null,
    gmroi: c.sumAvgInv > 0 && c.sumCostAmt > 0
      ? (c.sumNetRev - c.sumCostAmt) / c.sumAvgInv : null,
    margin: c.sumNetRev > 0 ? (c.sumNetRev - c.sumCostAmt) / c.sumNetRev : null,
  })).sort((x, y) => y.revenue - x.revenue);
}

/* ------------------------------------------------------------------ tables */

function renderTable(tableId, cols, rows, sortKey, opts = {}) {
  const table = document.getElementById(tableId);
  const sort = S.sorts[sortKey] || (S.sorts[sortKey] = { col: opts.defaultSort ?? null, dir: opts.defaultDir || "desc" });

  if (sort.col != null && cols[sort.col]) {
    const val = cols[sort.col].value;
    const dir = sort.dir === "asc" ? 1 : -1;
    rows = [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;      // blanks last regardless of direction
      if (y == null) return -1;
      if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
      return String(x).localeCompare(String(y)) * dir;
    });
  }

  const head = cols.map((c, i) => {
    const cls = [c.num ? "num" : "", sort.col === i ? `sort-${sort.dir}` : ""].filter(Boolean).join(" ");
    return `<th data-col="${i}"${cls ? ` class="${cls}"` : ""}${c.tip ? ` title="${esc(c.tip)}"` : ""}>${esc(c.label)}</th>`;
  }).join("");

  const body = rows.length
    ? rows.map(r => `<tr${opts.rowClass ? ` class="${opts.rowClass}"` : ""}${opts.rowId ? ` data-id="${esc(opts.rowId(r))}"` : ""}>${cols.map(c => c.cell(r)).join("")}</tr>`).join("")
    : `<tr><td class="empty" colspan="${cols.length}">${esc(opts.emptyText || "Nothing to show.")}</td></tr>`;

  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;

  table.querySelectorAll("th").forEach(th => {
    th.onclick = () => {
      const i = Number(th.dataset.col);
      if (sort.col === i) sort.dir = sort.dir === "asc" ? "desc" : "asc";
      else { sort.col = i; sort.dir = cols[i].num ? "desc" : "asc"; }
      render();
    };
  });

  if (opts.onRowClick) {
    table.querySelectorAll("tbody tr[data-id]").forEach(tr => {
      tr.onclick = () => opts.onRowClick(tr.dataset.id);
    });
  }
  return rows;
}

const scoreCell = item => {
  if (item.score == null) return '<td class="num muted">—</td>';
  // Flag anything graded on a partial denominator so a normalised score
  // isn't mistaken for a like-for-like one.
  const partial = item.scoreMax < 100
    ? ` <span class="flag" title="Scored out of ${item.scoreMax} available points (no ${item.hasLedger ? "" : "inventory "}${!item.hasLedger && !item.hasGmroi ? "or " : ""}${item.hasGmroi ? "" : "margin "}data), then normalised to 100">*</span>`
    : "";
  return `<td class="num">${item.score}${partial}</td>`;
};

const reviewCell = item => {
  const r = reviewOf(item);
  if (!r) return '<span class="tag pending">Pending</span>';
  const label = r.decision === "CONFIRM" ? "Confirmed" : "Kept → WATCH";
  const cls = r.decision === "CONFIRM" ? "confirmed" : "kept";
  const who = [r.by, r.note].filter(Boolean).join(" — ");
  const unsynced = r.synced === false ? " •" : "";
  return `<span class="tag ${cls}"${who ? ` title="${esc(who)}"` : ""}>${label}${unsynced}</span>`;
};

/* Column sets. `tip` becomes the header's hover explanation. */
const COLS = {
  scoreEngine: [
    { label: "Item ID", tip: "NetSuite internal item ID", value: i => i.id, cell: i => `<td>${esc(i.id)}</td>` },
    { label: "Brand", tip: "Catalog brand", value: i => i.brand, cell: i => `<td>${esc(i.brand || "—")}</td>` },
    { label: "Item", tip: "Item name as it appears in the catalog", value: i => i.name, cell: i => `<td class="name">${esc(i.name)}</td>` },
    { label: "Category", tip: "Item category", value: i => i.category, cell: i => `<td>${esc(i.category || "—")}</td>` },
    { label: "Status", tip: "Active = live in the procurement model. New To Market = first sold under 90 days ago, excluded from scoring.", value: i => i.status, cell: i => `<td>${statusTag(i.status)}</td>` },
    { label: "ABC", tip: "Category revenue class: A = top 80% of cumulative category revenue, B = next 15%, C = tail", value: i => i.catABC, cell: i => `<td>${classTag(i.catABC)}</td>` },
    { label: "XYZ", tip: "Demand steadiness: X = steady, Y = variable, Z = erratic", value: i => i.xyzClass, cell: i => `<td>${classTag(i.xyzClass)}</td>` },
    { label: "DIO", num: true, tip: "Days Inventory Outstanding = avg inventory value × 182 ÷ trailing 6-month COGS. Higher = slower moving. DEAD = stock on hand with no sales signal.", value: i => i.dio, cell: i => `<td class="num">${dioCell(i.dio)}</td>` },
    { label: "ITO", num: true, tip: "Inventory turnover, annualised = 6-month COGS × 2 ÷ avg inventory value", value: i => i.ito, cell: i => `<td class="num">${i.hasLedger ? dec(i.ito, 2) : "—"}</td>` },
    { label: "GMROI", num: true, tip: "Gross margin return on inventory = (net revenue − COGS) ÷ avg inventory value. Below 1.0 means the item returns less margin than the cash tied up in it.", value: i => i.gmroi, cell: i => `<td class="num">${i.hasGmroi ? dec(i.gmroi, 2) : "—"}</td>` },
    { label: "Velocity", num: true, tip: "Recent 3-month monthly average ÷ prior 9-month monthly average. Below 1.0 = declining.", value: i => i.velocity, cell: i => `<td class="num">${i.velocity == null ? "—" : dec(i.velocity, 2)}</td>` },
    { label: "Cust", num: true, tip: "Distinct customers who ordered this item in the trailing window", value: i => i.customers, cell: i => `<td class="num">${int(i.customers)}</td>` },
    { label: "Margin %", num: true, tip: "Trailing 6-month blended margin", value: i => i.marginPct, cell: i => `<td class="num">${i.costAmount ? percent(i.marginPct) : "—"}</td>` },
    { label: "12-mo rev", num: true, tip: "Invoiced amount over the trailing 12 months", value: i => i.revenue12mo, cell: i => `<td class="num">${money(i.revenue12mo)}</td>` },
    { label: "OH cost", num: true, tip: "Value of stock currently on hand", value: i => i.onHandCost, cell: i => `<td class="num">${money(i.onHandCost)}</td>` },
    { label: "DIO pts", num: true, tip: ">365d = 30 · >180 = 25 · >90 = 18 · >60 = 10 · >30 = 5 · else 0", value: i => i.scoreDio, cell: i => `<td class="num muted">${i.scoreDio}</td>` },
    { label: "ABC pts", num: true, tip: "C = 25 · B = 10 · A = 0", value: i => i.scoreAbc, cell: i => `<td class="num muted">${i.scoreAbc}</td>` },
    { label: "Vel pts", num: true, tip: "No sales = 20 · ≤2 = 12 · ≤5 = 9 · ≤10 = 5 · ≤20 = 2 · else 0", value: i => i.scoreVelocity, cell: i => `<td class="num muted">${i.scoreVelocity}</td>` },
    { label: "Cust pts", num: true, tip: "0 customers = 15 · ≤2 = 12 · ≤5 = 9 · ≤10 = 5 · ≤20 = 2 · else 0", value: i => i.scoreCustomers, cell: i => `<td class="num muted">${i.scoreCustomers}</td>` },
    { label: "GMROI pts", num: true, tip: "<0.5 = 10 · <1 = 8 · <1.5 = 5 · <2 = 3 · <3 = 1 · else 0. Zero when there is no margin or inventory data — absent data is not evidence of a poor return.", value: i => i.scoreGmroi, cell: i => `<td class="num muted">${i.scoreGmroi}</td>` },
    { label: "Score", num: true, tip: "Sum of the five components, normalised to the points this item could actually earn. * marks a normalised score.", value: i => i.score, cell: scoreCell },
    { label: "Rec", tip: "KEEP under 40 · WATCH 40–54 · SUNSET 55 and above", value: i => recOf(i), cell: i => `<td>${recTag(recOf(i))}</td>` },
  ],

  onHand: [
    { label: "Item ID", tip: "NetSuite internal item ID", value: i => i.id, cell: i => `<td>${esc(i.id)}</td>` },
    { label: "Item", tip: "Item name", value: i => i.name, cell: i => `<td class="name">${esc(i.name)}</td>` },
    { label: "Brand", tip: "Catalog brand", value: i => i.brand, cell: i => `<td>${esc(i.brand || "—")}</td>` },
    { label: "Category", tip: "Item category", value: i => i.category, cell: i => `<td>${esc(i.category || "—")}</td>` },
    { label: "Sub-cat", tip: "Item sub-category", value: i => i.subCategory, cell: i => `<td>${esc(i.subCategory || "—")}</td>` },
    { label: "Sale unit", tip: "Purchase unit. Every quantity below is expressed in these, not eaches.", value: i => i.saleUnit, cell: i => `<td>${esc(i.saleUnit || "—")}</td>` },
    { label: "QOH", num: true, tip: "Quantity on hand, in purchase units", value: i => i.qoh, cell: i => `<td class="num">${int(i.qoh)}</td>` },
    { label: "On order", num: true, tip: "Quantity on order, in purchase units", value: i => i.onOrder, cell: i => `<td class="num">${int(i.onOrder)}</td>` },
    { label: "Inv + pipe", num: true, tip: "On hand plus on order, in purchase units", value: i => i.invPipe, cell: i => `<td class="num">${int(i.invPipe)}</td>` },
    { label: "Cons 30", num: true, tip: "Consumption over the last 30 days, in purchase units", value: i => i.consumption30, cell: i => `<td class="num">${int(i.consumption30)}</td>` },
    { label: "Cons 60", num: true, tip: "Consumption over the last 60 days, in purchase units", value: i => i.consumption60, cell: i => `<td class="num">${int(i.consumption60)}</td>` },
    { label: "Unit cost", num: true, tip: "Average cost per purchase unit", value: i => i.costPerUnit, cell: i => `<td class="num">${money2(i.costPerUnit)}</td>` },
    { label: "OH cost", num: true, tip: "Value of stock on hand", value: i => i.onHandCost, cell: i => `<td class="num">${money(i.onHandCost)}</td>` },
    { label: "Total value", num: true, tip: "On hand plus on order, valued at average cost", value: i => i.totalValue, cell: i => `<td class="num">${money(i.totalValue)}</td>` },
    { label: "DIO", num: true, tip: "Days Inventory Outstanding", value: i => i.dio, cell: i => `<td class="num">${dioCell(i.dio)}</td>` },
    { label: "Rec", tip: "Current recommendation", value: i => recOf(i), cell: i => `<td>${recTag(recOf(i))}</td>` },
  ],

  brands: [
    { label: "Brand", tip: "Catalog brand, aggregated across every category it appears in", value: b => b.brand, cell: b => `<td class="name"><strong>${esc(b.brand)}</strong></td>` },
    { label: "Category", tip: "Primary category, with a count of any others the brand spans", value: b => b.category, cell: b => `<td>${esc(b.category)}</td>` },
    { label: "SKUs", num: true, tip: "Active and New To Market SKUs", value: b => b.skus, cell: b => `<td class="num">${int(b.skus)}</td>` },
    { label: "12-mo rev", num: true, tip: "Combined trailing 12-month revenue", value: b => b.revenue, cell: b => `<td class="num">${money(b.revenue)}</td>` },
    { label: "OH cost", num: true, tip: "Combined stock on hand at cost", value: b => b.onHandCost, cell: b => `<td class="num">${money(b.onHandCost)}</td>` },
    { label: "A", num: true, tip: "Count of A-class SKUs", value: b => b.a, cell: b => `<td class="num">${b.a}</td>` },
    { label: "B", num: true, tip: "Count of B-class SKUs", value: b => b.b, cell: b => `<td class="num">${b.b}</td>` },
    { label: "C", num: true, tip: "Count of C-class SKUs", value: b => b.c, cell: b => `<td class="num">${b.c}</td>` },
    { label: "Blended margin", num: true, tip: "(total revenue − total COGS) ÷ total revenue. Blended, not an average of per-item margins.", value: b => b.margin, cell: b => `<td class="num">${percent(b.margin)}</td>` },
    { label: "DIO", num: true, tip: "Computed from the brand's combined inventory and COGS, not averaged across SKUs", value: b => b.dio, cell: b => `<td class="num">${b.dio == null ? "—" : int(b.dio)}</td>` },
    { label: "GMROI", num: true, tip: "Computed from the brand's combined totals", value: b => b.gmroi, cell: b => `<td class="num">${b.gmroi == null ? "—" : dec(b.gmroi, 2)}</td>` },
    { label: "% sunset", num: true, tip: "Share of the brand's SKUs currently recommended for sunset", value: b => b.pctSunset, cell: b => `<td class="num">${percent(b.pctSunset, 0)}</td>` },
    { label: "Revenue retained", num: true, tip: "Revenue that stays if every sunset SKU goes", value: b => b.retainedPct, cell: b => `<td class="num">${percent(b.retainedPct, 0)}</td>` },
    { label: "Customers hit", num: true, tip: "Sum of customer relationships attached to the sunset SKUs", value: b => b.sunsetCustomers, cell: b => `<td class="num">${int(b.sunsetCustomers)}</td>` },
    {
      label: "Rec",
      tip: "EXIT BRAND: revenue under $1K, or ≥60% sunset and under $50K, or no A-class SKU and under $20K · MOQ REVIEW: revenue retained but DIO over 150 · TRIM TAIL: ≥25% sunset with an A-class SKU · KEEP — Strategic: over $200K with an A-class SKU · MONITOR: everything else",
      value: b => b.recommendation,
      cell: b => {
        const r = b.recommendation;
        const cls = r === "KEEP — Strategic" ? "keep" : r === "EXIT BRAND" ? "sunset"
          : r === "MONITOR" ? "nodata" : "watch";
        return `<td><span class="tag ${cls}">${esc(r)}</span></td>`;
      },
    },
  ],

  summary: [
    { label: "Item ID", tip: "NetSuite internal item ID", value: i => i.id, cell: i => `<td>${esc(i.id)}</td>` },
    { label: "Brand", tip: "Catalog brand", value: i => i.brand, cell: i => `<td>${esc(i.brand || "—")}</td>` },
    { label: "Item", tip: "Item name", value: i => i.name, cell: i => `<td class="name">${esc(i.name)}</td>` },
    { label: "Category", tip: "Item category", value: i => i.category, cell: i => `<td>${esc(i.category || "—")}</td>` },
    { label: "Sub-cat", tip: "Item sub-category", value: i => i.subCategory, cell: i => `<td>${esc(i.subCategory || "—")}</td>` },
    { label: "Status", tip: "Scoring status", value: i => i.status, cell: i => `<td>${statusTag(i.status)}</td>` },
    { label: "Inv + pipe", num: true, tip: "On hand plus on order, in purchase units", value: i => i.invPipe, cell: i => `<td class="num">${int(i.invPipe)}</td>` },
    { label: "OH cost", num: true, tip: "Value of stock on hand", value: i => i.onHandCost, cell: i => `<td class="num">${money(i.onHandCost)}</td>` },
    { label: "12-mo rev", num: true, tip: "Invoiced amount over the trailing 12 months", value: i => i.revenue12mo, cell: i => `<td class="num">${money(i.revenue12mo)}</td>` },
    { label: "ABC", tip: "Category revenue class", value: i => i.catABC, cell: i => `<td>${classTag(i.catABC)}</td>` },
    { label: "Units 12-mo", num: true, tip: "Invoiced sales units over the trailing 12 months", value: i => i.units12mo, cell: i => `<td class="num">${int(i.units12mo)}</td>` },
    { label: "XYZ", tip: "Demand steadiness", value: i => i.xyzClass, cell: i => `<td>${classTag(i.xyzClass)}</td>` },
    { label: "Cust", num: true, tip: "Distinct ordering customers", value: i => i.customers, cell: i => `<td class="num">${int(i.customers)}</td>` },
    { label: "% of market", num: true, tip: "This item's customers as a share of every customer ordering from the warehouse", value: i => i.customerPct, cell: i => `<td class="num">${percent(i.customerPct, 1)}</td>` },
    { label: "% of category", num: true, tip: "This item's customers as a share of all customer relationships in its category", value: i => i.customerCategoryPct, cell: i => `<td class="num">${percent(i.customerCategoryPct, 1)}</td>` },
    { label: "Margin %", num: true, tip: "Trailing 6-month blended margin", value: i => i.marginPct, cell: i => `<td class="num">${i.costAmount ? percent(i.marginPct) : "—"}</td>` },
    { label: "ITO", num: true, tip: "Inventory turnover, annualised", value: i => i.ito, cell: i => `<td class="num">${i.hasLedger ? dec(i.ito, 2) : "—"}</td>` },
    { label: "DIO", num: true, tip: "Days Inventory Outstanding", value: i => i.dio, cell: i => `<td class="num">${dioCell(i.dio)}</td>` },
    { label: "GMROI", num: true, tip: "Gross margin return on inventory", value: i => i.gmroi, cell: i => `<td class="num">${i.hasGmroi ? dec(i.gmroi, 2) : "—"}</td>` },
  ],

  inactive: [
    { label: "Item ID", tip: "NetSuite internal item ID", value: i => i.id, cell: i => `<td>${esc(i.id)}</td>` },
    { label: "Item", tip: "Item name", value: i => i.name, cell: i => `<td class="name">${esc(i.name)}</td>` },
    { label: "Brand", tip: "Catalog brand", value: i => i.brand, cell: i => `<td>${esc(i.brand || "—")}</td>` },
    { label: "Category", tip: "Item category", value: i => i.category, cell: i => `<td>${esc(i.category || "—")}</td>` },
    { label: "Sub-cat", tip: "Item sub-category", value: i => i.subCategory, cell: i => `<td>${esc(i.subCategory || "—")}</td>` },
    { label: "Status", tip: "Inactive = not live in the procurement model. No Data = live but with no signal in any source.", value: i => i.status, cell: i => `<td>${statusTag(i.status)}</td>` },
    { label: "QOH", num: true, tip: "Quantity on hand, in purchase units", value: i => i.qoh, cell: i => `<td class="num">${int(i.qoh)}</td>` },
    { label: "On order", num: true, tip: "Quantity on order, in purchase units", value: i => i.onOrder, cell: i => `<td class="num">${int(i.onOrder)}</td>` },
    { label: "Unit cost", num: true, tip: "Average cost per purchase unit", value: i => i.costPerUnit, cell: i => `<td class="num">${money2(i.costPerUnit)}</td>` },
    { label: "OH cost", num: true, tip: "Stranded value: stock on hand for an item with no live position", value: i => i.onHandCost, cell: i => `<td class="num">${money(i.onHandCost)}</td>` },
    { label: "12-mo rev", num: true, tip: "Invoiced amount over the trailing 12 months", value: i => i.revenue12mo, cell: i => `<td class="num">${money(i.revenue12mo)}</td>` },
    { label: "Cust", num: true, tip: "Distinct ordering customers", value: i => i.customers, cell: i => `<td class="num">${int(i.customers)}</td>` },
  ],

  sunsetReview: [
    { label: "Item ID", tip: "NetSuite internal item ID", value: i => i.id, cell: i => `<td>${esc(i.id)}</td>` },
    { label: "Brand", tip: "Catalog brand", value: i => i.brand, cell: i => `<td>${esc(i.brand || "—")}</td>` },
    { label: "Item", tip: "Item name", value: i => i.name, cell: i => `<td class="name">${esc(i.name)}</td>` },
    { label: "Category", tip: "Item category", value: i => i.category, cell: i => `<td>${esc(i.category || "—")}</td>` },
    { label: "Score", num: true, tip: "Normalised health score. 55 and above is the sunset threshold.", value: i => i.score, cell: scoreCell },
    { label: "DIO", num: true, tip: "Days Inventory Outstanding", value: i => i.dio, cell: i => `<td class="num">${dioCell(i.dio)}</td>` },
    { label: "OH cost", num: true, tip: "Stock on hand at cost — what delisting would recover", value: i => i.onHandCost, cell: i => `<td class="num">${money(i.onHandCost)}</td>` },
    { label: "12-mo rev", num: true, tip: "Revenue at risk if the item is delisted", value: i => i.revenue12mo, cell: i => `<td class="num">${money(i.revenue12mo)}</td>` },
    { label: "Cust", num: true, tip: "Customers who would be affected", value: i => i.customers, cell: i => `<td class="num">${int(i.customers)}</td>` },
    { label: "Decision", tip: "The team's call. A dot means it is saved locally but not yet written to the shared log.", value: i => (reviewOf(i)?.decision || ""), cell: i => `<td>${reviewCell(i)}</td>` },
    { label: "Decided by", tip: "Who made the call", value: i => (reviewOf(i)?.by || ""), cell: i => `<td>${esc(reviewOf(i)?.by || "—")}</td>` },
    { label: "Note", tip: "Why", value: i => (reviewOf(i)?.note || ""), cell: i => `<td class="muted">${esc(reviewOf(i)?.note || "—")}</td>` },
  ],
};

/* ------------------------------------------------------------------ filters */

const val = id => (document.getElementById(id)?.value || "").trim();
const matches = (item, q) => !q ||
  `${item.id} ${item.brand} ${item.name}`.toLowerCase().includes(q.toLowerCase());

function scoreEngineRows() {
  const q = val("se-q"), rec = val("se-rec"), abc = val("se-abc"),
    xyz = val("se-xyz"), cat = val("se-cat"), status = val("se-status");
  return activeItems().filter(i =>
    matches(i, q)
    && (!rec || recOf(i) === rec)
    && (!abc || i.catABC === abc)
    && (!xyz || i.xyzClass === xyz)
    && (!cat || i.category === cat)
    && (!status || i.status === status));
}
function onHandRows() {
  const q = val("oh-q"), cat = val("oh-cat"), brand = val("oh-brand");
  return activeItems().filter(i => matches(i, q) && (!cat || i.category === cat) && (!brand || i.brand === brand));
}
function summaryRows() {
  const q = val("sum-q"), cat = val("sum-cat"), abc = val("sum-abc"), xyz = val("sum-xyz");
  return activeItems().filter(i =>
    matches(i, q) && (!cat || i.category === cat) && (!abc || i.catABC === abc) && (!xyz || i.xyzClass === xyz));
}
function inactiveRows() {
  const q = val("in-q"), status = val("in-status"), cat = val("in-cat");
  return S.items.filter(i => (i.status === "Inactive" || i.status === "No Data")
    && matches(i, q) && (!status || i.status === status) && (!cat || i.category === cat));
}
function brandRows() {
  const q = val("be-q").toLowerCase(), rec = val("be-rec");
  return buildBrands().filter(b => (!q || b.brand.toLowerCase().includes(q)) && (!rec || b.recommendation === rec));
}
function sunsetRows() {
  const q = val("sr-q"), state = val("sr-state");
  return S.items.filter(i => {
    if (!isSunsetCandidate(i) || !matches(i, q)) return false;
    if (!state) return true;
    const r = reviewOf(i);
    return state === "pending" ? !r : r?.decision === state;
  });
}

/* ------------------------------------------------------------------- views */

function renderDashboard() {
  const all = S.items;
  const active = activeItems();
  const scored = active.filter(i => i.status === "Active");
  const counts = { KEEP: 0, WATCH: 0, SUNSET: 0 };
  scored.forEach(i => { const r = recOf(i); if (counts[r] != null) counts[r]++; });

  const inventoryValue = all.reduce((s, i) => s + i.onHandCost, 0);
  const sunsetValue = scored.filter(i => recOf(i) === "SUNSET").reduce((s, i) => s + i.onHandCost, 0);
  const stranded = all.filter(i => i.status === "Inactive").reduce((s, i) => s + i.onHandCost, 0);

  document.getElementById("kpis").innerHTML = `
    <div class="kpi accent" title="Every SKU with a position in this warehouse, including inactive ones">
      <div class="label">Total SKUs</div><div class="value">${int(all.length)}</div>
      <div class="foot">${int(active.length)} active · ${int(all.length - active.length)} inactive</div></div>
    <div class="kpi" title="Value of all stock on hand">
      <div class="label">Inventory value</div><div class="value">${money(inventoryValue)}</div></div>
    <div class="kpi green" title="Active SKUs scoring under 40 — healthy, no action">
      <div class="label">Keep</div><div class="value">${int(counts.KEEP)}</div>
      <div class="foot">${percent(scored.length ? counts.KEEP / scored.length : 0, 0)} of scored</div></div>
    <div class="kpi amber" title="Active SKUs scoring 40–54 — revisit next cycle">
      <div class="label">Watch</div><div class="value">${int(counts.WATCH)}</div>
      <div class="foot">${percent(scored.length ? counts.WATCH / scored.length : 0, 0)} of scored</div></div>
    <div class="kpi red" title="Active SKUs scoring 55 or above — candidates for delisting">
      <div class="label">Sunset</div><div class="value">${int(counts.SUNSET)}</div>
      <div class="foot">${money(sunsetValue)} on hand</div></div>
    <div class="kpi" title="Stock still on hand for items with no live procurement position">
      <div class="label">Stranded cost</div><div class="value">${money(stranded)}</div>
      <div class="foot">inactive SKUs</div></div>`;

  const cats = buildCategories();

  // ABC × XYZ matrix
  const cells = {};
  scored.forEach(i => {
    if (!i.catABC || !i.xyzClass) return;
    const k = `${i.catABC}${i.xyzClass}`;
    (cells[k] || (cells[k] = { n: 0, v: 0 })).n++;
    cells[k].v += i.onHandCost;
  });
  const abcTip = {
    A: "A — top 80% of cumulative category revenue",
    B: "B — next 15%", C: "C — the tail beyond 95%",
  };
  const xyzTip = { X: "X — steady demand", Y: "Y — variable demand", Z: "Z — erratic demand" };
  document.getElementById("matrix").innerHTML = `<table class="matrix">
    <thead><tr><th></th>${["X", "Y", "Z"].map(x => `<th title="${esc(xyzTip[x])}">${x}</th>`).join("")}</tr></thead>
    <tbody>${["A", "B", "C"].map(a => `<tr><th title="${esc(abcTip[a])}">${a}</th>${["X", "Y", "Z"].map(x => {
    const c = cells[`${a}${x}`];
    return `<td class="cell"><div class="n">${c ? int(c.n) : "—"}</div><div class="v">${c ? money(c.v) : ""}</div></td>`;
  }).join("")}</tr>`).join("")}</tbody></table>`;

  renderTable("cat-tbl", [
    { label: "Category", value: c => c.category, cell: c => `<td class="name">${esc(c.category)}</td>` },
    { label: "SKUs", num: true, value: c => c.skus, cell: c => `<td class="num">${int(c.skus)}</td>` },
    { label: "12-mo rev", num: true, tip: "Combined trailing 12-month revenue", value: c => c.revenue, cell: c => `<td class="num">${money(c.revenue)}</td>` },
    { label: "OH cost", num: true, tip: "Combined stock on hand at cost", value: c => c.onHandCost, cell: c => `<td class="num">${money(c.onHandCost)}</td>` },
    { label: "Margin %", num: true, tip: "Blended across the category", value: c => c.margin, cell: c => `<td class="num">${percent(c.margin)}</td>` },
    { label: "DIO", num: true, tip: "From the category's combined inventory and COGS, not an average of item DIOs", value: c => c.dio, cell: c => `<td class="num">${c.dio == null ? "—" : int(c.dio)}</td>` },
    { label: "ITO", num: true, tip: "From the category's combined totals", value: c => c.ito, cell: c => `<td class="num">${c.ito == null ? "—" : dec(c.ito, 2)}</td>` },
    { label: "GMROI", num: true, tip: "From the category's combined totals", value: c => c.gmroi, cell: c => `<td class="num">${c.gmroi == null ? "—" : dec(c.gmroi, 2)}</td>` },
    { label: "Keep", num: true, value: c => c.keep, cell: c => `<td class="num">${int(c.keep)}</td>` },
    { label: "Watch", num: true, value: c => c.watch, cell: c => `<td class="num">${int(c.watch)}</td>` },
    { label: "Sunset", num: true, value: c => c.sunset, cell: c => `<td class="num">${int(c.sunset)}</td>` },
  ], cats, "cat", { defaultSort: 2 });

  // Charts last: they are the only CDN-dependent part of the page, so
  // everything above is already on screen if the script didn't load.
  chart("chart-rec", {
    type: "doughnut",
    data: {
      labels: ["Keep", "Watch", "Sunset"],
      datasets: [{
        data: [counts.KEEP, counts.WATCH, counts.SUNSET],
        backgroundColor: ["#1F7A33", "#FFD100", "#B42318"],
        borderColor: "#FFFFFF", borderWidth: 2,
      }],
    },
    options: { plugins: { legend: { position: "bottom" } }, maintainAspectRatio: false },
  });

  const top = cats.slice(0, 12);
  chart("chart-rev", {
    type: "bar",
    data: {
      labels: top.map(c => c.category),
      datasets: [{ data: top.map(c => c.revenue), backgroundColor: "#40180B", borderRadius: 3 }],
    },
    options: {
      indexAxis: "y", maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { callback: v => money(v) } } },
    },
  });
}

/* Charts are the one part that depends on a CDN. If Chart.js didn't load,
 * say so in the card and carry on -- the tables below are the substance and
 * shouldn't disappear because a script request failed. */
function chart(id, config) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  if (typeof Chart === "undefined") {
    canvas.closest(".body").innerHTML =
      '<div class="empty">Chart library unavailable — the figures are in the tables below.</div>';
    return;
  }
  try {
    Chart.defaults.color = "#8A7B6A";
    Chart.defaults.borderColor = "#E6DFCC";
    Chart.defaults.font.family = '"Inter", system-ui, sans-serif';
    if (S.charts[id]) S.charts[id].destroy();
    S.charts[id] = new Chart(canvas, config);
  } catch (err) {
    canvas.closest(".body").innerHTML = `<div class="empty">Chart failed to render (${esc(err.message)}).</div>`;
  }
}

function renderSunsetStatus() {
  const candidates = S.items.filter(isSunsetCandidate);
  const confirmed = candidates.filter(i => reviewOf(i)?.decision === "CONFIRM");
  const kept = candidates.filter(i => reviewOf(i)?.decision === "KEEP");
  const pending = candidates.length - confirmed.length - kept.length;
  const unsynced = candidates.filter(i => reviewOf(i)?.synced === false).length;
  const recover = confirmed.reduce((s, i) => s + i.onHandCost, 0);

  const el = document.getElementById("sr-status");
  el.classList.toggle("warn", !APPS_SCRIPT_URL || unsynced > 0);
  const sync = !APPS_SCRIPT_URL
    ? ` <strong>Decisions are saved in this browser only</strong> — the shared decision log is not connected yet (see SETUP.md).`
    : unsynced > 0
      ? ` <strong>${unsynced} decision${unsynced === 1 ? "" : "s"} not yet written to the shared log</strong> — they will retry next time you record one.`
      : ` Decisions are written to the shared log, so the whole team sees the same state.`;
  el.innerHTML = `<strong>${int(pending)}</strong> pending · <strong>${int(confirmed.length)}</strong> confirmed sunset
    (${money(recover)} on hand) · <strong>${int(kept.length)}</strong> kept → WATCH.${sync}
    Click any row to record a decision.`;
}

function fillOptions(id, values, keepValue = true) {
  const el = document.getElementById(id);
  if (!el) return;
  const current = keepValue ? el.value : "";
  const first = el.querySelector("option");
  el.innerHTML = "";
  el.appendChild(first);
  values.forEach(v => {
    const o = document.createElement("option");
    o.value = o.textContent = v;
    el.appendChild(o);
  });
  if (current && values.includes(current)) el.value = current;
}

function render() {
  if (!S.data) return;
  const setCount = (id, n, total) =>
    (document.getElementById(id).textContent = `${int(n)}${total != null ? ` of ${int(total)}` : ""}`);

  document.getElementById("tab-se").textContent = int(activeItems().length);
  document.getElementById("tab-in").textContent = int(S.items.filter(i => i.status === "Inactive" || i.status === "No Data").length);
  document.getElementById("tab-sr").textContent = int(S.items.filter(isSunsetCandidate).length);

  switch (S.view) {
    case "dashboard":
      renderDashboard();
      break;
    case "score-engine": {
      const rows = scoreEngineRows();
      renderTable("se-tbl", COLS.scoreEngine, rows, "se", {
        defaultSort: 20, rowClass: "clickable", rowId: i => i.id,
        onRowClick: openDrill,
        emptyText: "No items match these filters.",
      });
      setCount("se-count", rows.length, activeItems().length);
      break;
    }
    case "on-hand": {
      const rows = onHandRows();
      renderTable("oh-tbl", COLS.onHand, rows, "oh", {
        defaultSort: 12, rowClass: "clickable", rowId: i => i.id, onRowClick: openDrill,
      });
      setCount("oh-count", rows.length, activeItems().length);
      break;
    }
    case "brand-eval": {
      const rows = brandRows();
      renderTable("be-tbl", COLS.brands, rows, "be", { defaultSort: 3 });
      setCount("be-count", rows.length);
      break;
    }
    case "summary": {
      const rows = summaryRows();
      renderTable("sum-tbl", COLS.summary, rows, "sum", { defaultSort: 8 });
      setCount("sum-count", rows.length, activeItems().length);
      break;
    }
    case "inactive": {
      const rows = inactiveRows();
      renderTable("in-tbl", COLS.inactive, rows, "in", { defaultSort: 9 });
      setCount("in-count", rows.length);
      break;
    }
    case "sunset-review": {
      const rows = sunsetRows();
      renderTable("sr-tbl", COLS.sunsetReview, rows, "sr", {
        defaultSort: 4, rowClass: "clickable", rowId: i => i.id,
        onRowClick: openDecision,
        emptyText: "Nothing flagged for sunset in this warehouse.",
      });
      setCount("sr-count", rows.length);
      renderSunsetStatus();
      break;
    }
  }
}

/* --------------------------------------------------------------- drill-down */

async function openDrill(id) {
  const item = S.items.find(i => i.id === id);
  if (!item) return;
  document.getElementById("drill-title").textContent = item.name;
  document.getElementById("drill-sub").textContent =
    `${item.brand || "—"} · ${item.category || "—"} · item ${item.id} · ${S.wh}`;
  const body = document.getElementById("drill-body");
  body.innerHTML = `<div class="stat-grid">
    <div class="stat"><div class="label">Score</div><div class="value">${item.score ?? "—"}</div><div class="foot">${
    item.scoreMax === 100 ? "out of 100" : `normalised from ${item.scoreMax} earnable points`}</div></div>
    <div class="stat"><div class="label">Recommendation</div><div class="value" style="font-size:15px">${recOf(item) || "—"}</div></div>
    <div class="stat"><div class="label">DIO</div><div class="value">${item.dio >= 9999 ? "Dead" : int(item.dio)}</div><div class="foot">days of stock</div></div>
    <div class="stat"><div class="label">GMROI</div><div class="value">${item.hasGmroi ? dec(item.gmroi, 2) : "—"}</div></div>
    <div class="stat"><div class="label">Customers</div><div class="value">${int(item.customers)}</div><div class="foot">${percent(item.customerPct, 1)} of market</div></div>
    <div class="stat"><div class="label">On hand</div><div class="value">${money(item.onHandCost)}</div><div class="foot">${int(item.qoh)} ${esc(item.saleUnit || "units")}</div></div>
  </div><div id="drill-cust" class="muted">Loading customers…</div>`;
  document.getElementById("drill").hidden = false;

  const index = await loadCustomers(S.wh);
  const target = document.getElementById("drill-cust");
  if (!target) return;

  if (!index) {
    target.innerHTML = `<div class="notice">Named customers come from the Sales Tracker export for
      ${esc(S.wh)}, which isn't available. The customer count above still comes from the scorecard's own source.</div>`;
    return;
  }
  const rows = index.get(item.name.trim().toLowerCase());
  if (!rows || !rows.length) {
    target.innerHTML = `<div class="notice">No Sales Tracker orders matched this item. Its tracker window is
      shorter than the scorecard's trailing period, so low-volume items can legitimately have none.</div>`;
    return;
  }
  const total = rows.reduce((s, r) => s + r.units, 0);
  const top3 = rows.slice(0, 3).reduce((s, r) => s + r.units, 0);
  target.innerHTML = `<h3 style="font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8A7B6A;margin:0 0 8px">
      Ordering customers (${rows.length})${rows.length >= 3 ? ` — ${percent(top3 / total, 1)} of units from the top 3` : ""}
    </h3>
    <table><thead><tr><th>Customer</th><th class="num">Units</th><th class="num">Orders</th>
      <th>Last order</th><th class="num">Share</th><th></th></tr></thead>
    <tbody>${rows.slice(0, 60).map(r => `<tr>
      <td>${esc(r.name)}${r.enterprise ? ' <span class="tag nodata">ENT</span>' : ""}</td>
      <td class="num">${int(r.units)}</td><td class="num">${int(r.lines)}</td>
      <td class="muted">${esc(r.lastOrder || "—")}</td>
      <td class="num">${percent(r.units / total, 1)}</td>
      <td style="width:110px"><div class="share-bar" style="width:${Math.max(2, (r.units / rows[0].units) * 100)}%"></div></td>
    </tr>`).join("")}</tbody></table>
    ${rows.length > 60 ? `<div class="muted" style="margin-top:8px">Showing the top 60 of ${int(rows.length)}.</div>` : ""}
    <div class="muted" style="margin-top:10px">Units come from the Sales Tracker window, which differs from the
    scorecard's trailing period — this count won't always tie to the Customers figure above.</div>`;
}

function closeDrill() { document.getElementById("drill").hidden = true; }

/* ---------------------------------------------------------- decision modal */

function openDecision(id) {
  const item = S.items.find(i => i.id === id);
  if (!item) return;
  S.pending = item;
  const existing = reviewOf(item);
  document.getElementById("decide-sub").textContent = `${item.name} · ${item.brand || "—"} · ${S.wh}`;
  document.getElementById("decide-by").value = existing?.by || localStorage.getItem(NAME_KEY) || "";
  document.getElementById("decide-note").value = existing?.note || "";
  const warn = document.getElementById("decide-warn");
  warn.hidden = !!APPS_SCRIPT_URL;
  warn.textContent = "The shared decision log isn't connected yet, so this saves in your browser only.";
  document.getElementById("decide-clear").hidden = !existing;
  document.getElementById("decide").hidden = false;
  document.getElementById("decide-by").focus();
}
function closeDecision() { document.getElementById("decide").hidden = true; S.pending = null; }

async function submitDecision(decision) {
  const item = S.pending;
  if (!item) return;
  const by = val("decide-by");
  if (!by) { document.getElementById("decide-by").focus(); return; }
  localStorage.setItem(NAME_KEY, by);
  const note = val("decide-note");
  closeDecision();
  await recordDecision([item], decision, by, note);
}

/* --------------------------------------------------------------- workbooks */

const MOCHA = "FF40180B";
const CREAM = "FFFBF7EC";

function styleHeader(row) {
  row.height = 24;
  row.eachCell(cell => {
    cell.font = { name: "Arial", bold: true, size: 10, color: { argb: "FFFFFFFF" } };
    cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: MOCHA } };
    cell.alignment = { vertical: "middle", wrapText: true };
    cell.border = { bottom: { style: "thin", color: { argb: "FFE6DFCC" } } };
  });
}

function autoWidth(sheet) {
  sheet.columns.forEach(col => {
    let max = 10;
    col.eachCell({ includeEmpty: false }, cell => {
      max = Math.max(max, Math.min(46, String(cell.value ?? "").length + 2));
    });
    col.width = max;
  });
}

function addSheet(wb, name, headers, rows) {
  if (!rows.length) return;
  const ws = wb.addWorksheet(name.slice(0, 31));
  ws.views = [{ state: "frozen", ySplit: 1 }];
  ws.addRow(headers);
  rows.forEach(r => ws.addRow(r));
  styleHeader(ws.getRow(1));
  ws.eachRow((row, n) => {
    if (n === 1) return;
    row.eachCell(cell => {
      cell.font = { name: "Arial", size: 10 };
      if (n % 2 === 0) cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: CREAM } };
    });
  });
  autoWidth(ws);
  ws.autoFilter = { from: { row: 1, column: 1 }, to: { row: 1, column: headers.length } };
  return ws;
}

/* ExcelJS also comes from a CDN. Fail with a sentence rather than a
 * ReferenceError if it didn't load. */
function excelReady() {
  if (typeof ExcelJS !== "undefined") return true;
  alert("The Excel library didn't load, so exporting isn't available right now. "
    + "Check your connection and reload the page.");
  return false;
}

async function download(wb, filename) {
  const buf = await wb.xlsx.writeBuffer();
  const url = URL.createObjectURL(new Blob([buf], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  }));
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const stamp = () => new Date().toISOString().slice(0, 10);

async function exportWorkbook() {
  if (!excelReady()) return;
  const wb = new ExcelJS.Workbook();
  const active = activeItems();

  addSheet(wb, "Score Engine",
    ["Item ID", "Brand", "Item", "Category", "Status", "ABC", "XYZ", "DIO", "ITO", "GMROI",
      "Velocity", "Customers", "Margin %", "12-Mo Rev", "OH Cost", "Score", "Score Max", "Recommendation"],
    active.map(i => [i.id, i.brand, i.name, i.category, i.status, i.catABC, i.xyzClass,
      i.hasLedger ? i.dio : null, i.hasLedger ? i.ito : null, i.hasGmroi ? i.gmroi : null,
      i.velocity, i.customers, i.marginPct, i.revenue12mo, i.onHandCost, i.score, i.scoreMax, recOf(i)]));

  addSheet(wb, "On Hand",
    ["Item ID", "Item", "Brand", "Category", "Sub-Category", "Sale Unit", "QOH", "On Order",
      "Inv + Pipe", "Cons 30", "Cons 60", "Unit Cost", "OH Cost", "Total Value", "DIO"],
    active.map(i => [i.id, i.name, i.brand, i.category, i.subCategory, i.saleUnit, i.qoh, i.onOrder,
      i.invPipe, i.consumption30, i.consumption60, i.costPerUnit, i.onHandCost, i.totalValue,
      i.hasLedger ? i.dio : null]));

  addSheet(wb, "Brand Evaluation",
    ["Brand", "Category", "SKUs", "12-Mo Rev", "OH Cost", "A", "B", "C", "Blended Margin",
      "DIO", "GMROI", "% Sunset", "Revenue Retained", "Customers Impacted", "Recommendation"],
    buildBrands().map(b => [b.brand, b.category, b.skus, b.revenue, b.onHandCost, b.a, b.b, b.c,
      b.margin, b.dio, b.gmroi, b.pctSunset, b.retainedPct, b.sunsetCustomers, b.recommendation]));

  addSheet(wb, "Summary",
    ["Item ID", "Brand", "Item", "Category", "Sub-Category", "Status", "Inv + Pipe", "OH Cost",
      "12-Mo Rev", "ABC", "Units 12-Mo", "XYZ", "Customers", "% of Market", "% of Category",
      "Margin %", "ITO", "DIO", "GMROI"],
    active.map(i => [i.id, i.brand, i.name, i.category, i.subCategory, i.status, i.invPipe, i.onHandCost,
      i.revenue12mo, i.catABC, i.units12mo, i.xyzClass, i.customers, i.customerPct, i.customerCategoryPct,
      i.marginPct, i.hasLedger ? i.ito : null, i.hasLedger ? i.dio : null, i.hasGmroi ? i.gmroi : null]));

  addSheet(wb, "Inactive",
    ["Item ID", "Item", "Brand", "Category", "Sub-Category", "Status", "QOH", "On Order",
      "Unit Cost", "OH Cost", "12-Mo Rev", "Customers"],
    S.items.filter(i => i.status === "Inactive" || i.status === "No Data")
      .map(i => [i.id, i.name, i.brand, i.category, i.subCategory, i.status, i.qoh, i.onOrder,
        i.costPerUnit, i.onHandCost, i.revenue12mo, i.customers]));

  await download(wb, `Inventory_Health_${S.wh}_${stamp()}.xlsx`);
}

async function exportReviewFile() {
  if (!excelReady()) return;
  const items = S.items.filter(isSunsetCandidate);
  if (!items.length) { alert("Nothing flagged for sunset in this warehouse."); return; }
  const wb = new ExcelJS.Workbook();
  const ws = addSheet(wb, "Sunset Review",
    ["Item ID", "Brand", "Item Name", "Category", "Score", "DIO", "OH Cost", "12-Mo Rev",
      "Customers", "Decision", "Decided By", "Notes"],
    items.map(i => {
      const r = reviewOf(i);
      return [i.id, i.brand, i.name, i.category, i.score, i.hasLedger ? i.dio : null,
        i.onHandCost, i.revenue12mo, i.customers, r?.decision || "", r?.by || "", r?.note || ""];
    }));

  // Constrain Decision to the two values the importer understands, so the
  // round-trip can't come back full of free text.
  for (let row = 2; row <= items.length + 1; row++) {
    ws.getCell(`J${row}`).dataValidation = {
      type: "list", allowBlank: true, formulae: ['"CONFIRM,KEEP"'],
      showErrorMessage: true, errorTitle: "Pick one",
      error: "CONFIRM to sunset the item, KEEP to retain it.",
    };
  }
  ws.views = [{ state: "frozen", xSplit: 3, ySplit: 1 }];
  await download(wb, `Sunset_Review_${S.wh}_${stamp()}.xlsx`);
}

const CONFIRM_WORDS = new Set(["CONFIRM", "CONFIRMED", "SUNSET", "YES", "Y", "X", "KILL"]);
const KEEP_WORDS = new Set(["KEEP", "KEPT", "NO", "N", "WATCH", "RETAIN", "HOLD"]);

async function importReviewFile(file) {
  if (!excelReady()) return;
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(await file.arrayBuffer());
  const ws = wb.worksheets[0];
  if (!ws) { alert("That workbook has no sheets."); return; }

  const header = {};
  ws.getRow(1).eachCell((cell, i) => {
    header[String(cell.text || "").trim().toLowerCase()] = i;
  });
  const idCol = header["item id"];
  const decCol = header["decision"];
  if (!idCol || !decCol) {
    alert('That file needs at least an "Item ID" and a "Decision" column.');
    return;
  }
  const byCol = header["decided by"] ?? header["reviewer"] ?? header["name"];
  const noteCol = header["notes"] ?? header["note"];

  const byId = new Map(S.items.map(i => [i.id, i]));
  const at = new Date().toISOString();
  const batch = [];
  let confirmed = 0, kept = 0, skipped = 0, unknown = 0;

  ws.eachRow((row, n) => {
    if (n === 1) return;
    const id = String(row.getCell(idCol).text || "").trim();
    if (!id) return;
    const item = byId.get(id);
    if (!item) { unknown++; return; }
    const raw = String(row.getCell(decCol).text || "").trim().toUpperCase();
    const decision = CONFIRM_WORDS.has(raw) ? "CONFIRM" : KEEP_WORDS.has(raw) ? "KEEP" : null;
    // A blank decision means "not reviewed yet", which is information —
    // don't invent a call for it.
    if (!decision) { if (raw) skipped++; return; }
    const by = byCol ? String(row.getCell(byCol).text || "").trim() : "";
    const note = noteCol ? String(row.getCell(noteCol).text || "").trim() : "";
    S.local[id] = { decision, by, note, at, synced: false };
    batch.push({ warehouse: S.wh, item_id: id, item_name: item.name, decision, decided_by: by, notes: note });
    if (decision === "CONFIRM") confirmed++; else kept++;
  });

  saveLocal();
  render();

  let message = `Imported ${confirmed} confirmed sunset, ${kept} kept → WATCH.`;
  if (skipped) message += `\n${skipped} row(s) had an unrecognised decision and were left pending.`;
  if (unknown) message += `\n${unknown} row(s) referenced items not in ${S.wh}.`;

  if (batch.length && APPS_SCRIPT_URL) {
    const ok = await postDecisions(batch);
    if (ok) {
      batch.forEach(r => { if (S.local[r.item_id]) S.local[r.item_id].synced = true; });
      saveLocal();
      render();
      message += `\nWritten to the shared decision log.`;
    } else {
      message += `\nCouldn't reach the shared log — saved locally and will retry.`;
    }
  } else if (batch.length) {
    message += `\nSaved in this browser only; the shared log isn't connected yet.`;
  }
  alert(message);
}

async function exportConfirmed() {
  if (!excelReady()) return;
  const items = S.items.filter(i => reviewOf(i)?.decision === "CONFIRM");
  if (!items.length) { alert("No confirmed sunset items yet."); return; }
  const wb = new ExcelJS.Workbook();
  addSheet(wb, "Confirmed Sunset",
    ["Item ID", "Brand", "Item Name", "Category", "Sub-Category", "QOH", "On Order", "OH Cost",
      "12-Mo Rev", "Customers", "Score", "Decided By", "Review Note", "Decided At"],
    items.map(i => {
      const r = reviewOf(i);
      return [i.id, i.brand, i.name, i.category, i.subCategory, i.qoh, i.onOrder, i.onHandCost,
        i.revenue12mo, i.customers, i.score, r?.by || "", r?.note || "", (r?.at || "").slice(0, 10)];
    }));
  await download(wb, `Sunset_Confirmed_${S.wh}_${stamp()}.xlsx`);
}

/* -------------------------------------------------------------------- init */

function switchView(view) {
  S.view = view;
  document.querySelectorAll("nav.tabs button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.view === view)));
  document.querySelectorAll("main .view").forEach(s =>
    (s.hidden = s.id !== `view-${view}`));
  render();
}

async function selectWarehouse(wh) {
  document.getElementById("meta").textContent = `Loading ${wh}…`;
  await loadWarehouse(wh);

  const cats = [...new Set(S.items.map(i => i.category).filter(Boolean))].sort();
  const brands = [...new Set(S.items.map(i => i.brand).filter(Boolean))].sort();
  ["se-cat", "oh-cat", "sum-cat", "in-cat"].forEach(id => fillOptions(id, cats, false));
  fillOptions("oh-brand", brands, false);
  fillOptions("be-rec",
    ["EXIT BRAND", "MOQ REVIEW", "TRIM TAIL", "KEEP — Strategic", "MONITOR"], false);

  const built = S.manifest.generatedAt
    ? new Date(S.manifest.generatedAt).toLocaleString("en-US",
      { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : "unknown";
  const gaps = [];
  if (!S.manifest.sources?.daysActiveAvailable) gaps.push("first-fulfilment dates unavailable, so New To Market can't be flagged");
  document.getElementById("meta").innerHTML =
    `${esc(S.wh)} · ${int(S.items.length)} SKUs · data built ${esc(built)}`
    + (gaps.length ? ` · <span title="${esc(gaps.join("; "))}">⚠︎ ${esc(gaps.length)} data gap</span>` : "");

  try { history.replaceState(null, "", `?wh=${encodeURIComponent(wh)}`); } catch { /* file:// */ }
  render();
}

async function init() {
  try {
    await loadManifest();
  } catch (err) {
    document.getElementById("meta").textContent = `Could not load data (${err.message}).`;
    return;
  }

  const picker = document.getElementById("wh-picker");
  const codes = S.manifest.warehouses.map(w => w.code);
  picker.innerHTML = codes.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  picker.onchange = () => selectWarehouse(picker.value);

  document.querySelectorAll("nav.tabs button").forEach(b =>
    (b.onclick = () => switchView(b.dataset.view)));

  // Re-render on any filter change; each view reads its own controls.
  // Text inputs listen on `input` ONLY. Binding `change` as well means that
  // after typing, the blur caused by clicking a row fires change -> render ->
  // the row is replaced between mousedown and mouseup, so the click never
  // reaches its handler and the first click on a search result does nothing.
  ["se-q", "se-rec", "se-abc", "se-xyz", "se-cat", "se-status",
    "oh-q", "oh-cat", "oh-brand", "be-q", "be-rec",
    "sum-q", "sum-cat", "sum-abc", "sum-xyz",
    "in-q", "in-status", "in-cat", "sr-q", "sr-state"].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.tagName === "SELECT") el.onchange = render;
      else el.oninput = render;
    });
  [["se-clear", ["se-q", "se-rec", "se-abc", "se-xyz", "se-cat", "se-status"]],
  ["oh-clear", ["oh-q", "oh-cat", "oh-brand"]],
  ["be-clear", ["be-q", "be-rec"]],
  ["sum-clear", ["sum-q", "sum-cat", "sum-abc", "sum-xyz"]],
  ["in-clear", ["in-q", "in-status", "in-cat"]]].forEach(([btn, ids]) => {
    document.getElementById(btn).onclick = () => {
      ids.forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
      render();
    };
  });

  document.getElementById("btn-export").onclick = exportWorkbook;
  document.getElementById("sr-export").onclick = exportReviewFile;
  document.getElementById("sr-export-confirmed").onclick = exportConfirmed;
  document.getElementById("sr-import-btn").onclick = () => document.getElementById("sr-import").click();
  document.getElementById("sr-import").onchange = async e => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await importReviewFile(file);
  };

  document.getElementById("drill-close").onclick = closeDrill;
  document.getElementById("drill").onclick = e => { if (e.target.id === "drill") closeDrill(); };
  document.getElementById("decide-close").onclick = closeDecision;
  document.getElementById("decide").onclick = e => { if (e.target.id === "decide") closeDecision(); };
  document.getElementById("decide-confirm").onclick = () => submitDecision("CONFIRM");
  document.getElementById("decide-keep").onclick = () => submitDecision("KEEP");
  document.getElementById("decide-clear").onclick = () => {
    if (S.pending) clearDecision(S.pending);
    closeDecision();
  };
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeDrill(); closeDecision(); }
  });

  const wanted = new URLSearchParams(location.search).get("wh");
  const start = codes.includes(wanted) ? wanted : codes[0];
  picker.value = start;
  await selectWarehouse(start);
}

init();
