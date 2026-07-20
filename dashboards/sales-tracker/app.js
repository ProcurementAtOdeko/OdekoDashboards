/* Sales Tracker warehouse page. Loaded by the generic stub at
   dashboards/sales-tracker/<WAREHOUSE>/index.html — the warehouse code is
   the folder name. Data lives at ../data/<WAREHOUSE>.json. */

const WAREHOUSE = decodeURIComponent(
  location.pathname.replace(/\/+$/, "").split("/").pop()
).toUpperCase();

document.title = `Odeko · ${WAREHOUSE} Sales Tracker`;
document.body.innerHTML = `
<header>
  <div class="brand">
    <img class="logo" src="../../_shared/odeko-logo.png" alt="Odeko" width="40" height="40" />
    <div>
      <h1 id="page-title">${WAREHOUSE} — Sales Tracker (Trailing 90 Days)</h1>
      <div class="sub" id="meta">Loading…</div>
    </div>
  </div>
</header>

<main>
  <section class="kpis" id="kpis"></section>

  <section class="charts">
    <div class="card chart full">
      <h3>Units Sold by Week</h3>
      <div class="body"><canvas id="chart-weekly"></canvas></div>
    </div>
    <div class="card tall">
      <h3>Top 10 Items — Units Sold</h3>
      <div class="body"><canvas id="chart-items"></canvas></div>
    </div>
    <div class="card tall">
      <h3>Top 10 Customers — Units Sold</h3>
      <div class="body"><canvas id="chart-customers"></canvas></div>
    </div>
  </section>

  <section class="card">
    <div class="tabs" id="tabs">
      <button data-tab="items" class="active">Items</button>
      <button data-tab="customers">Customers</button>
      <button data-tab="placements">New Placements</button>
      <button data-tab="businessLines" id="tab-bl" hidden>Business Lines</button>
    </div>
    <div class="toolbar">
      <input id="search" type="search" placeholder="Search…" />
      <span class="count" id="table-count"></span>
      <span class="count muted" id="table-hint">Click a row to see its breakdown</span>
      <button id="dl-csv" class="dl-csv" type="button" title="Download the current view as CSV">⬇ CSV</button>
    </div>
    <div style="overflow-x:auto">
      <table id="grid">
        <thead id="grid-head"></thead>
        <tbody id="grid-body"></tbody>
      </table>
    </div>
  </section>
</main>`;

const fmtInt = (n) => n == null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: 0 });
const fmt = (n) =>
  n == null ? "—" :
  Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 })
                      : n.toLocaleString(undefined, { maximumFractionDigits: 1 });
const fmtDate = (s) => s == null ? "—" : s;

const PALETTE = {
  ink: "#40180B", muted: "#8A7B6A", line: "#E6DFCC",
  green: "#1F7A33", amber: "#B5660A", red: "#B42318", yellow: "#FFD100", neutral: "#A89684",
};

let DATA = null;
let tab = "items";
let sortKey = "units";
let sortDir = "desc";
let filterText = "";
let expanded = new Set();

const TABS = {
  items: {
    columns: [
      { key: "name", label: "Item" },
      { key: "brand", label: "Brand", hideSm: true },
      { key: "units", label: "Units Sold", num: true },
      { key: "localShare", label: "Local / Ecomm", num: true, needsBL: true },
      { key: "trendDelta", label: "Trend (4w)", num: true },
      { key: "lines", label: "Order Lines", num: true, hideSm: true },
      { key: "customers", label: "Customers", num: true, hideSm: true },
      { key: "newLocations", label: "New Locs (14d)", num: true },
      { key: "firstOrder", label: "First Order", hideSm: true },
      { key: "lastOrder", label: "Last Order", hideSm: true },
    ],
    defaultSort: "units",
  },
  customers: {
    columns: [
      { key: "name", label: "Customer" },
      { key: "businessLine", label: "Business Line", needsBL: true },
      { key: "units", label: "Units Sold", num: true },
      { key: "trendDelta", label: "Trend (4w)", num: true },
      { key: "lines", label: "Order Lines", num: true, hideSm: true },
      { key: "items", label: "SKUs", num: true, hideSm: true },
      { key: "firstOrder", label: "First Order", hideSm: true },
      { key: "lastOrder", label: "Last Order", hideSm: true },
    ],
    defaultSort: "units",
  },
  businessLines: {
    columns: [
      { key: "name", label: "Business Line" },
      { key: "category", label: "Type", hideSm: true },
      { key: "units", label: "Units Sold", num: true },
      { key: "share", label: "% of Units", num: true },
      { key: "lines", label: "Order Lines", num: true, hideSm: true },
      { key: "customers", label: "Customers", num: true },
      { key: "items", label: "SKUs", num: true, hideSm: true },
    ],
    defaultSort: "units",
  },
  placements: {
    columns: [
      { key: "customerName", label: "Customer" },
      { key: "itemName", label: "Item" },
      { key: "minDate", label: "First Order" },
      { key: "units", label: "Units Since", num: true },
      { key: "lines", label: "Order Lines", num: true, hideSm: true },
      { key: "lastOrder", label: "Last Order", hideSm: true },
    ],
    defaultSort: "minDate",
  },
};

async function init() {
  const dataRes = await fetch(`../data/${WAREHOUSE}.json`, { cache: "no-store" });
  if (!dataRes.ok) {
    document.getElementById("meta").textContent = `No data available for ${WAREHOUSE}.`;
    return;
  }
  DATA = await dataRes.json();
  DATA.placements = DATA.pairs
    .filter(p => p.new)
    .map(p => ({
      ...p,
      customerName: DATA.customers[p.c].name,
      itemName: DATA.items[p.i].name,
      brand: DATA.items[p.i].brand,
    }));
  if (hasBL()) {
    DATA.items.forEach(i => {
      const t = (i.localUnits || 0) + (i.ecommUnits || 0);
      i.localShare = t ? i.localUnits / t : null;
    });
    const total = DATA.businessLines.reduce((s, b) => s + b.units, 0) || 1;
    DATA.businessLines.forEach(b => { b.share = b.units / total; });
    document.getElementById("tab-bl").hidden = false;
  }
  document.getElementById("meta").textContent =
    `Updated ${new Date(DATA.generatedAt).toLocaleString()} · ${DATA.dateRange.start} → ${DATA.dateRange.end}`;
  renderKpis();
  renderTable();
  try { renderCharts(); } catch (e) { console.error("Charts failed to render:", e); }
}

const hasBL = () => !!(DATA && DATA.businessLines && DATA.businessLines.length);
const activeColumns = (spec) => spec.columns.filter(c => !c.needsBL || hasBL());

const BL_COLOR = { local: "#1F7A33", ecomm: "#B5660A", other: "#A89684" };

function splitBar(r) {
  const local = r.localUnits || 0, ecomm = r.ecommUnits || 0, t = local + ecomm;
  if (!t) return '<span class="muted">—</span>';
  const pct = Math.round((local / t) * 100);
  const title = `Local ${fmt(local)} · Ecomm ${fmt(ecomm)}`;
  return `<span class="split" title="${title}">` +
    `<span class="split-bar"><span style="width:${pct}%;background:${BL_COLOR.local}"></span>` +
    `<span style="width:${100 - pct}%;background:${BL_COLOR.ecomm}"></span></span>` +
    `<span class="split-pct">${pct}% loc</span></span>`;
}

function blDot(cat) {
  return `<span class="bl-dot" style="background:${BL_COLOR[cat] || BL_COLOR.other}"></span>`;
}

function renderKpis() {
  const s = DATA.summary;
  const kpis = [
    { label: "Units Sold (90d)", value: fmt(s.totalUnits), cls: "accent" },
    { label: "Order Lines", value: fmtInt(s.orderLines), cls: "" },
    { label: "Active Customers", value: fmtInt(s.customerCount), cls: "" },
    { label: "Active SKUs", value: fmtInt(s.itemCount), cls: "" },
    { label: `New Placements (${DATA.newPlacementDays}d)`, value: fmtInt(s.newPlacements), cls: "green" },
    { label: `New Customers (${DATA.newPlacementDays}d)`, value: fmtInt(s.newCustomers), cls: "green" },
    { label: `New Locations (${DATA.newLocationDays}d)`, value: fmtInt(s.newLocations), cls: "green" },
  ];
  document.getElementById("kpis").innerHTML = kpis.map(k =>
    `<div class="kpi ${k.cls}"><div class="label">${k.label}</div><div class="value">${k.value}</div></div>`
  ).join("");
}

function renderCharts() {
  Chart.defaults.color = PALETTE.muted;
  Chart.defaults.borderColor = PALETTE.line;
  Chart.defaults.font.family = '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';

  const truncEnd = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
  const truncMid = (s, n) => s.length > n
    ? s.slice(0, Math.ceil((n - 1) / 2)) + "…" + s.slice(s.length - Math.floor((n - 1) / 2))
    : s;

  const wk = DATA.weeklyTrend;
  new Chart(document.getElementById("chart-weekly"), {
    type: "bar",
    data: {
      labels: wk.map(w => w.weekStart),
      datasets: [{
        data: wk.map(w => w.units),
        backgroundColor: PALETTE.ink,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: (c) => `Week of ${c[0].label}`,
          label: (c) => `${fmt(c.raw)} units · ${fmtInt(wk[c.dataIndex].lines)} lines`,
        } },
      },
      scales: { y: { beginAtZero: true, title: { display: true, text: "Units Sold" } } },
    },
  });

  const ti = DATA.items.slice(0, 10);
  new Chart(document.getElementById("chart-items"), {
    type: "bar",
    data: {
      labels: ti.map(i => i.name),
      datasets: [{ data: ti.map(i => i.units), backgroundColor: PALETTE.ink, borderRadius: 4 }],
    },
    options: {
      indexAxis: "y",
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: (c) => ti[c[0].dataIndex].name, label: (c) => `${fmt(c.raw)} units` } },
      },
      scales: {
        x: { beginAtZero: true },
        y: { ticks: { font: { size: 11 }, callback: function (v) { return truncEnd(this.getLabelForValue(v), 28); } } },
      },
    },
  });

  const tc = DATA.customers.slice(0, 10);
  new Chart(document.getElementById("chart-customers"), {
    type: "bar",
    data: {
      labels: tc.map(c => c.name),
      datasets: [{ data: tc.map(c => c.units), backgroundColor: PALETTE.neutral, borderRadius: 4 }],
    },
    options: {
      indexAxis: "y",
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: (c) => tc[c[0].dataIndex].name, label: (c) => `${fmt(c.raw)} units` } },
      },
      scales: {
        x: { beginAtZero: true },
        y: { ticks: { font: { size: 11 }, callback: function (v) { return truncMid(this.getLabelForValue(v), 28); } } },
      },
    },
  });
}

function rowsForTab() {
  if (tab === "items") return DATA.items;
  if (tab === "customers") return DATA.customers;
  if (tab === "businessLines") return DATA.businessLines;
  return DATA.placements;
}

function searchableText(row) {
  if (tab === "placements") return `${row.customerName} ${row.itemName} ${row.brand || ""}`;
  if (tab === "businessLines") return `${row.name} ${row.category}`;
  return `${row.name} ${row.brand || ""} ${row.uuid}${row.enterprise ? " enterprise" : ""}${row.businessLine ? " " + row.businessLine : ""}`;
}

function filteredSortedRows() {
  const q = filterText.toLowerCase();
  const rows = rowsForTab().filter(r => !q || searchableText(r).toLowerCase().includes(q));
  return rows.slice().sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
    return sortDir === "asc" ? av - bv : bv - av;
  });
}

function renderTable() {
  const spec = TABS[tab];
  const rows = filteredSortedRows();

  document.getElementById("table-count").textContent =
    `${fmtInt(rows.length)}${rows.length !== rowsForTab().length ? ` of ${fmtInt(rowsForTab().length)}` : ""} rows`;
  document.getElementById("table-hint").style.display =
    tab === "placements" || tab === "businessLines" ? "none" : "";

  document.getElementById("grid-head").innerHTML = "<tr>" + activeColumns(spec).map(c =>
    `<th class="${c.num ? "num" : ""} ${c.hideSm ? "hide-sm" : ""} ${sortKey === c.key ? (sortDir === "asc" ? "sort-asc" : "sort-desc") : ""}" data-sort="${c.key}">${c.label}</th>`
  ).join("") + "</tr>";

  document.getElementById("grid-body").innerHTML = rows.map(r => renderRow(r, spec)).join("");

  document.querySelectorAll("#grid-head th").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
      else { sortKey = key; sortDir = key === "name" || key === "customerName" || key === "itemName" ? "asc" : "desc"; }
      renderTable();
    });
  });

  document.querySelectorAll("#grid-body tr.row").forEach(tr => {
    tr.addEventListener("click", () => {
      const id = tr.dataset.id;
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      renderTable();
    });
  });

  document.querySelectorAll("#grid-body .dl-detail").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const list = tab === "items" ? DATA.items : DATA.customers;
      const r = list.find(x => x.uuid === btn.dataset.id);
      if (r) exportDetailCsv(r);
    });
  });
}

function sparkline(r) {
  const pts = r.trend || [];
  if (pts.length < 2) return '<span class="muted">—</span>';
  const color = r.trendDir === "up" ? PALETTE.green : r.trendDir === "down" ? PALETTE.red : PALETTE.muted;
  const arrow = r.trendDir === "up" ? "▲" : r.trendDir === "down" ? "▼" : "–";
  const w = 64, h = 20, pad = 2;
  const max = Math.max(...pts), min = Math.min(...pts);
  const span = max - min || 1;
  const x = (i) => pad + i * ((w - 2 * pad) / (pts.length - 1));
  const y = (v) => h - pad - ((v - min) / span) * (h - 2 * pad);
  const poly = pts.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = pts.length - 1;
  const title = (DATA.trendWeeks || []).map((wk, i) => `w/o ${wk}: ${fmt(pts[i])}`).join(" · ");
  return `<span class="spark" title="${escapeHtml(title)}">` +
    `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-label="4 week trend ${r.trendDir}">` +
    `<polyline points="${poly}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>` +
    `<circle cx="${x(last).toFixed(1)}" cy="${y(pts[last]).toFixed(1)}" r="2.4" fill="${color}"/>` +
    `</svg><span class="spark-arrow" style="color:${color}">${arrow}</span></span>`;
}

function renderRow(r, spec) {
  if (tab === "placements") {
    return `<tr>
      <td>${escapeHtml(r.customerName)}</td>
      <td>${escapeHtml(r.itemName)}${r.brand ? `<div class="uuid">${escapeHtml(r.brand)}</div>` : ""}</td>
      <td>${fmtDate(r.minDate)}<span class="badge-new">New</span></td>
      <td class="num">${fmt(r.units)}</td>
      <td class="num hide-sm">${fmtInt(r.lines)}</td>
      <td class="hide-sm">${fmtDate(r.lastOrder)}</td>
    </tr>`;
  }

  if (tab === "businessLines") {
    return `<tr>
      <td>${blDot(r.category)}${escapeHtml(r.name)}</td>
      <td class="hide-sm" style="text-transform:capitalize">${escapeHtml(r.category)}</td>
      <td class="num">${fmt(r.units)}</td>
      <td class="num">${Math.round(r.share * 100)}%</td>
      <td class="num hide-sm">${fmtInt(r.lines)}</td>
      <td class="num">${fmtInt(r.customers)}</td>
      <td class="num hide-sm">${fmtInt(r.items)}</td>
    </tr>`;
  }

  const isItem = tab === "items";
  const main = isItem
    ? `<tr class="row" data-id="${r.uuid}">
        <td>${escapeHtml(r.name)}${/^[0-9a-f]{8}-/.test(r.uuid) ? `<div class="uuid">${r.uuid.slice(0, 8)}</div>` : ""}</td>
        <td class="hide-sm">${escapeHtml(r.brand || "")}</td>
        <td class="num">${fmt(r.units)}</td>
        ${hasBL() ? `<td class="num">${splitBar(r)}</td>` : ""}
        <td class="num">${sparkline(r)}</td>
        <td class="num hide-sm">${fmtInt(r.lines)}</td>
        <td class="num hide-sm">${fmtInt(r.customers)}</td>
        <td class="num${r.newLocations ? " new-locs" : ""}">${fmtInt(r.newLocations)}</td>
        <td class="hide-sm">${fmtDate(r.firstOrder)}</td>
        <td class="hide-sm">${fmtDate(r.lastOrder)}</td>
      </tr>`
    : `<tr class="row" data-id="${r.uuid}">
        <td>${escapeHtml(r.name)}${r.enterprise ? '<span class="badge-ent">Ent</span>' : ""}${/^[0-9a-f]{8}-/.test(r.uuid) ? `<div class="uuid">${r.uuid.slice(0, 8)}</div>` : ""}</td>
        ${hasBL() ? `<td>${r.businessLine ? blDot(lineCategory(r.businessLine)) + escapeHtml(r.businessLine) : '<span class="muted">—</span>'}</td>` : ""}
        <td class="num">${fmt(r.units)}</td>
        <td class="num">${sparkline(r)}</td>
        <td class="num hide-sm">${fmtInt(r.lines)}</td>
        <td class="num hide-sm">${fmtInt(r.items)}</td>
        <td class="hide-sm">${fmtDate(r.firstOrder)}</td>
        <td class="hide-sm">${fmtDate(r.lastOrder)}</td>
      </tr>`;

  if (!expanded.has(r.uuid)) return main;
  return main + renderDetail(r, activeColumns(spec).length);
}

const LOCAL_LINES = new Set(["metrobi", "local distribution", "roadie", "pickup"]);
const ECOMM_LINES = new Set(["shipping", "odeko shipping", "parcel - bulk", "drop ship"]);
function lineCategory(name) {
  const k = (name || "").trim().toLowerCase();
  if (LOCAL_LINES.has(k)) return "local";
  if (ECOMM_LINES.has(k)) return "ecomm";
  return "other";
}

// Related pairs for an expanded item/customer row, each joined to the "other"
// entity. Shared by the detail render and its CSV export.
function detailPairs(r) {
  const isItem = tab === "items";
  const idx = isItem
    ? DATA.items.findIndex(i => i.uuid === r.uuid)
    : DATA.customers.findIndex(c => c.uuid === r.uuid);
  return DATA.pairs
    .filter(p => (isItem ? p.i === idx : p.c === idx))
    .map(p => ({ p, other: isItem ? DATA.customers[p.c] : DATA.items[p.i] }))
    .sort((a, b) => b.p.units - a.p.units);
}

function renderDetail(r, colspan) {
  const otherLabel = tab === "items" ? "Customer" : "Item";
  const rows = detailPairs(r).map(({ p, other }) => `<tr>
      <td>${escapeHtml(other.name)}${p.new ? '<span class="badge-new">New</span>' : ""}</td>
      <td class="num">${fmt(p.units)}</td>
      <td class="num hide-sm">${fmtInt(p.lines)}</td>
      <td>${fmtDate(p.minDate)}</td>
      <td class="hide-sm">${fmtDate(p.lastOrder)}</td>
    </tr>`).join("");
  return `<tr class="detail"><td colspan="${colspan}">
    <div class="detail-head">
      <span class="detail-title">${escapeHtml(r.name)} · by ${otherLabel.toLowerCase()}</span>
      <button class="dl-csv dl-detail" type="button" data-id="${escapeHtml(r.uuid)}" title="Download this breakdown as CSV">⬇ CSV</button>
    </div>
    <table class="detail-table">
      <thead><tr>
        <th>${otherLabel}</th>
        <th class="num">Units</th>
        <th class="num hide-sm">Lines</th>
        <th>First Order</th>
        <th class="hide-sm">Last Order</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </td></tr>`;
}

function exportDetailCsv(r) {
  const otherLabel = tab === "items" ? "Customer" : "Item";
  const header = [otherLabel, "New", "Units", "Order Lines", "First Order", "Last Order"];
  const lines = [header.map(csvCell).join(",")];
  for (const { p, other } of detailPairs(r)) {
    lines.push([
      other.name, p.new ? "TRUE" : "FALSE", p.units, p.lines, p.minDate || "", p.lastOrder || "",
    ].map(csvCell).join(","));
  }
  const kind = tab === "items" ? "item" : "customer";
  const slug = r.name.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || r.uuid.slice(0, 8);
  downloadCsv(lines, `${WAREHOUSE}-${kind}-${slug}-${DATA.dateRange.end}.csv`);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}

// --- CSV export -----------------------------------------------------------
// Column defs pull the underlying data values (not the rendered widgets), so
// the download is analysis-ready. BL columns are included only when present.
function csvColumns() {
  const localPct = (r) => {
    const t = (r.localUnits || 0) + (r.ecommUnits || 0);
    return t ? Math.round((r.localUnits / t) * 100) : "";
  };
  if (tab === "items") {
    return [
      ["Item", r => r.name],
      ["Brand", r => r.brand || ""],
      ["Item UUID", r => /^[0-9a-f]{8}-/.test(r.uuid) ? r.uuid : ""],
      ["Units Sold", r => r.units],
      ...(hasBL() ? [
        ["Local Units", r => r.localUnits],
        ["Ecomm Units", r => r.ecommUnits],
        ["Local %", localPct],
      ] : []),
      ["Trend 4w Delta", r => r.trendDelta],
      ["Trend Dir", r => r.trendDir],
      ["Order Lines", r => r.lines],
      ["Customers", r => r.customers],
      ["New Locations 14d", r => r.newLocations],
      ["First Order", r => r.firstOrder || ""],
      ["Last Order", r => r.lastOrder || ""],
    ];
  }
  if (tab === "customers") {
    return [
      ["Customer", r => r.name],
      ...(hasBL() ? [["Business Line", r => r.businessLine || ""]] : []),
      ["Enterprise", r => r.enterprise ? "TRUE" : "FALSE"],
      ["Account UUID", r => /^[0-9a-f]{8}-/.test(r.uuid) ? r.uuid : ""],
      ["Units Sold", r => r.units],
      ["Trend 4w Delta", r => r.trendDelta],
      ["Trend Dir", r => r.trendDir],
      ["Order Lines", r => r.lines],
      ["SKUs", r => r.items],
      ["First Order", r => r.firstOrder || ""],
      ["Last Order", r => r.lastOrder || ""],
    ];
  }
  if (tab === "businessLines") {
    return [
      ["Business Line", r => r.name],
      ["Type", r => r.category],
      ["Units Sold", r => r.units],
      ["% of Units", r => Math.round(r.share * 100)],
      ["Order Lines", r => r.lines],
      ["Customers", r => r.customers],
      ["SKUs", r => r.items],
    ];
  }
  return [ // placements
    ["Customer", r => r.customerName],
    ["Item", r => r.itemName],
    ["Brand", r => r.brand || ""],
    ["First Order", r => r.minDate || ""],
    ["Units Since", r => r.units],
    ["Order Lines", r => r.lines],
    ["Last Order", r => r.lastOrder || ""],
  ];
}

function csvCell(v) {
  const s = v == null ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(lines, filename) {
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportCsv() {
  const cols = csvColumns();
  const rows = filteredSortedRows();
  const lines = [cols.map(c => csvCell(c[0])).join(",")];
  for (const r of rows) lines.push(cols.map(c => csvCell(c[1](r))).join(","));
  downloadCsv(lines, `${WAREHOUSE}-sales-tracker-${tab}-${DATA.dateRange.end}.csv`);
}

document.getElementById("dl-csv").addEventListener("click", exportCsv);

document.getElementById("search").addEventListener("input", (e) => {
  filterText = e.target.value;
  renderTable();
});

document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    tab = btn.dataset.tab;
    document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b === btn));
    sortKey = TABS[tab].defaultSort;
    sortDir = "desc";
    expanded = new Set();
    renderTable();
  });
});

init();
