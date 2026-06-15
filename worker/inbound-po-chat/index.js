// Cloudflare Worker: Inbound PO chat backend.
//
// Verifies a Google ID token (must be hd === "odeko.com"), then runs a
// Claude tool-use loop against the dashboard's data.json so users can ask
// natural-language questions about inbound POs.

const GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs";
const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";
const ANTHROPIC_VERSION = "2023-06-01";
const MAX_TOOL_ROUNDS = 6;
const DATA_TTL_MS = 10 * 60 * 1000; // 10 min in-memory cache

let dataCache = { at: 0, data: null };
let jwksCache = { at: 0, keys: null };

const TOOLS = [
  {
    name: "query_pos",
    description:
      "Search open PO lines. All filters are optional and AND-combined. " +
      "Strings are case-insensitive substring matches. Returns up to `limit` " +
      "matching rows plus the total count of matches.",
    input_schema: {
      type: "object",
      properties: {
        warehouse: { type: "string", description: "Warehouse code, e.g. DCA1, BNA1, PDX1." },
        vendor_contains: { type: "string" },
        item_contains: { type: "string" },
        po_number: { type: "string", description: "Exact PO number (e.g. PO110127)." },
        status: { type: "string", description: "PO status, e.g. 'Pending Receipt'." },
        eta_on_or_before: { type: "string", description: "ISO date YYYY-MM-DD." },
        eta_on_or_after: { type: "string", description: "ISO date YYYY-MM-DD." },
        only_outstanding: { type: "boolean", description: "If true, only lines with outstanding > 0." },
        limit: { type: "integer", default: 50, maximum: 200 },
      },
    },
  },
  {
    name: "summary",
    description:
      "Aggregate counts and outstanding-unit totals, grouped by `group_by` " +
      "(warehouse | vendor | status | eta_week). Same optional filters as query_pos.",
    input_schema: {
      type: "object",
      properties: {
        group_by: { type: "string", enum: ["warehouse", "vendor", "status", "eta_week"] },
        warehouse: { type: "string" },
        vendor_contains: { type: "string" },
        item_contains: { type: "string" },
        status: { type: "string" },
        eta_on_or_before: { type: "string" },
        eta_on_or_after: { type: "string" },
        only_outstanding: { type: "boolean" },
        limit: { type: "integer", default: 20, maximum: 100 },
      },
      required: ["group_by"],
    },
  },
  {
    name: "list_dimensions",
    description: "Return the distinct warehouses and statuses present in the dataset.",
    input_schema: { type: "object", properties: {} },
  },
];

// ---- Auth -----------------------------------------------------------------

async function getJwks() {
  if (jwksCache.keys && Date.now() - jwksCache.at < 60 * 60 * 1000) return jwksCache.keys;
  const r = await fetch(GOOGLE_JWKS_URL);
  const j = await r.json();
  jwksCache = { at: Date.now(), keys: j.keys };
  return j.keys;
}

function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function verifyGoogleIdToken(token, expectedAud) {
  const [hB64, pB64, sB64] = token.split(".");
  if (!hB64 || !pB64 || !sB64) throw new Error("Malformed token");
  const header = JSON.parse(new TextDecoder().decode(b64urlToBytes(hB64)));
  const payload = JSON.parse(new TextDecoder().decode(b64urlToBytes(pB64)));
  const keys = await getJwks();
  const jwk = keys.find((k) => k.kid === header.kid && k.alg === header.alg);
  if (!jwk) throw new Error("Signing key not found");
  const key = await crypto.subtle.importKey(
    "jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]
  );
  const data = new TextEncoder().encode(`${hB64}.${pB64}`);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlToBytes(sB64), data);
  if (!ok) throw new Error("Bad signature");
  const now = Math.floor(Date.now() / 1000);
  if (payload.exp <= now) throw new Error("Token expired");
  if (!["accounts.google.com", "https://accounts.google.com"].includes(payload.iss))
    throw new Error("Bad issuer");
  if (payload.aud !== expectedAud) throw new Error("Bad audience");
  if (payload.hd !== "odeko.com") throw new Error("Not an @odeko.com account");
  return payload;
}

// ---- Data -----------------------------------------------------------------

async function loadData(env) {
  if (dataCache.data && Date.now() - dataCache.at < DATA_TTL_MS) return dataCache.data;
  const r = await fetch(env.DATA_URL, { cf: { cacheTtl: 300 } });
  if (!r.ok) throw new Error(`data.json fetch failed: ${r.status}`);
  const data = await r.json();
  dataCache = { at: Date.now(), data };
  return data;
}

// ---- Tools ----------------------------------------------------------------

function applyFilters(lines, f) {
  const ci = (s) => (s || "").toLowerCase();
  const vendorQ = ci(f.vendor_contains);
  const itemQ = ci(f.item_contains);
  const statusQ = ci(f.status);
  const whQ = ci(f.warehouse);
  const poQ = ci(f.po_number);
  return lines.filter((l) => {
    if (whQ && ci(l.warehouse) !== whQ) return false;
    if (poQ && ci(l.po) !== poQ) return false;
    if (statusQ && ci(l.status) !== statusQ) return false;
    if (vendorQ && !ci(l.vendor).includes(vendorQ)) return false;
    if (itemQ && !ci(l.item).includes(itemQ)) return false;
    if (f.eta_on_or_before && l.eta && l.eta > f.eta_on_or_before) return false;
    if (f.eta_on_or_after && l.eta && l.eta < f.eta_on_or_after) return false;
    if (f.only_outstanding && !(l.outstanding > 0)) return false;
    return true;
  });
}

function weekStart(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso + "T00:00:00Z");
  if (isNaN(d)) return "unknown";
  const day = d.getUTCDay(); // 0=Sun
  const diff = (day + 6) % 7; // back to Monday
  d.setUTCDate(d.getUTCDate() - diff);
  return d.toISOString().slice(0, 10);
}

function runTool(name, input, data) {
  if (name === "list_dimensions") {
    return { warehouses: data.warehouses, statuses: data.statuses };
  }
  const filtered = applyFilters(data.lines, input);
  if (name === "query_pos") {
    const limit = Math.min(input.limit ?? 50, 200);
    return {
      total: filtered.length,
      returned: Math.min(filtered.length, limit),
      rows: filtered.slice(0, limit),
    };
  }
  if (name === "summary") {
    const groups = new Map();
    const keyFn = {
      warehouse: (l) => l.warehouse || "(none)",
      vendor: (l) => l.vendor || "(none)",
      status: (l) => l.status || "(none)",
      eta_week: (l) => weekStart(l.eta),
    }[input.group_by];
    for (const l of filtered) {
      const k = keyFn(l);
      const g = groups.get(k) || { key: k, lines: 0, outstanding: 0, ordered: 0 };
      g.lines += 1;
      g.outstanding += l.outstanding;
      g.ordered += l.ordered;
      groups.set(k, g);
    }
    const rows = [...groups.values()].sort((a, b) => b.outstanding - a.outstanding);
    const limit = Math.min(input.limit ?? 20, 100);
    return { totalLines: filtered.length, groups: rows.slice(0, limit) };
  }
  return { error: `unknown tool: ${name}` };
}

// ---- Claude loop ----------------------------------------------------------

function systemPrompt(data) {
  return [
    "You are an internal Odeko assistant that answers questions about open inbound purchase orders.",
    `Today's date is ${new Date().toISOString().slice(0, 10)} (UTC). Data was generated at ${data.generatedAt}.`,
    `Dataset: ${data.lineCount.toLocaleString()} open PO lines across warehouses: ${data.warehouses.join(", ")}.`,
    `Statuses included: ${data.statuses.join(", ")}.`,
    "",
    "Use the provided tools to look up data — never invent PO numbers, vendors, or quantities.",
    "When summarizing, prefer the `summary` tool over fetching raw rows.",
    "Be concise. Use bullet lists or short tables when listing multiple POs.",
    "If a user's question is ambiguous (e.g. which warehouse), ask one clarifying question instead of guessing.",
  ].join("\n");
}

async function callClaude(env, messages, system) {
  const r = await fetch(ANTHROPIC_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": ANTHROPIC_VERSION,
    },
    body: JSON.stringify({
      model: env.ANTHROPIC_MODEL,
      max_tokens: 1024,
      system,
      tools: TOOLS,
      messages,
    }),
  });
  if (!r.ok) throw new Error(`Anthropic ${r.status}: ${(await r.text()).slice(0, 500)}`);
  return r.json();
}

async function chat(env, userMessages) {
  const data = await loadData(env);
  const system = systemPrompt(data);
  // Convert {role,content:string} -> Anthropic message shape (content can be string)
  const messages = userMessages.map((m) => ({ role: m.role, content: m.content }));
  const toolsUsed = [];
  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const resp = await callClaude(env, messages, system);
    messages.push({ role: "assistant", content: resp.content });
    if (resp.stop_reason !== "tool_use") {
      const text = resp.content.filter((b) => b.type === "text").map((b) => b.text).join("\n").trim();
      return { reply: text || "(no reply)", toolsUsed };
    }
    const toolResults = [];
    for (const block of resp.content) {
      if (block.type !== "tool_use") continue;
      toolsUsed.push(block.name);
      let result;
      try {
        result = runTool(block.name, block.input || {}, data);
      } catch (e) {
        result = { error: e.message };
      }
      toolResults.push({
        type: "tool_result",
        tool_use_id: block.id,
        content: JSON.stringify(result),
      });
    }
    messages.push({ role: "user", content: toolResults });
  }
  return { reply: "Stopped after too many tool rounds. Try rephrasing.", toolsUsed };
}

// ---- HTTP handler ---------------------------------------------------------

function cors(env, extra = {}) {
  return {
    "access-control-allow-origin": env.ALLOWED_ORIGIN || "*",
    "access-control-allow-methods": "POST, OPTIONS",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-max-age": "86400",
    ...extra,
  };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: cors(env) });
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/chat") {
      return new Response("Not found", { status: 404, headers: cors(env) });
    }
    const auth = request.headers.get("authorization") || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!token) return new Response("Missing bearer token", { status: 401, headers: cors(env) });
    try {
      await verifyGoogleIdToken(token, env.GOOGLE_OAUTH_CLIENT_ID);
    } catch (e) {
      return new Response(`Auth: ${e.message}`, { status: 401, headers: cors(env) });
    }
    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("Invalid JSON", { status: 400, headers: cors(env) });
    }
    const msgs = Array.isArray(body.messages) ? body.messages : null;
    if (!msgs || !msgs.length) return new Response("messages required", { status: 400, headers: cors(env) });
    try {
      const out = await chat(env, msgs);
      return new Response(JSON.stringify(out), {
        headers: cors(env, { "content-type": "application/json" }),
      });
    } catch (e) {
      return new Response(`Error: ${e.message}`, { status: 500, headers: cors(env) });
    }
  },
};
