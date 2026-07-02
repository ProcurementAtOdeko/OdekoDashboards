# Inbound PO chat — Cloudflare Worker

Backend for `dashboards/inbound-po-chat/`. Verifies Google ID tokens
(restricted to `@odeko.com`), then runs a Claude tool-use loop against the
dashboard's `data.json`.

## One-time setup

### 1. Google OAuth client ID

1. Open <https://console.cloud.google.com/apis/credentials>.
2. **Create Credentials → OAuth client ID → Web application**.
3. **Authorized JavaScript origins:** add the dashboard origin,
   e.g. `https://procurementatodeko.github.io`.
   (No "Authorized redirect URIs" needed — we use Google Identity Services in-page.)
4. Copy the **Client ID** (looks like `12345-abc.apps.googleusercontent.com`).
5. Paste it into `dashboards/inbound-po-chat/index.html`
   (`GOOGLE_CLIENT_ID` constant near the top of the `<script>` block).

### 2. Anthropic API key

Get one from <https://console.anthropic.com/>. Keep it ready for step 4.

### 3. Install Wrangler

```bash
npm install -g wrangler
wrangler login
```

### 4. Deploy

From the repo root:

```bash
cd worker/inbound-po-chat

# Set secrets (you'll be prompted to paste each value)
wrangler secret put ANTHROPIC_API_KEY
wrangler secret put GOOGLE_OAUTH_CLIENT_ID

# Deploy
wrangler deploy
```

Wrangler will print the worker URL,
e.g. `https://inbound-po-chat.<your-subdomain>.workers.dev`.

### 5. Wire the frontend

Edit `dashboards/inbound-po-chat/index.html` and set:

- `GOOGLE_CLIENT_ID` — from step 1
- `WORKER_URL` — `https://inbound-po-chat.<your-subdomain>.workers.dev/chat`

Commit + push. The dashboard is live at
`https://procurementatodeko.github.io/OdekoDashboards/inbound-po-chat/`
(or wherever GitHub Pages serves this repo).

### 6. Lock down CORS (optional but recommended)

Edit `wrangler.toml` and set `ALLOWED_ORIGIN` to the exact GitHub Pages
origin, then `wrangler deploy` again.

## Local dev

```bash
wrangler dev
# Worker runs at http://localhost:8787; point WORKER_URL there to test.
```

## How it works

- **Auth:** worker verifies the Google ID token signature against Google's
  JWKS, checks `aud` matches `GOOGLE_OAUTH_CLIENT_ID`, and rejects anything
  without `hd === "odeko.com"`.
- **Data:** worker fetches `data.json` from the deployed GitHub Pages site
  (cached for 10 min in worker memory) so refreshes flow through
  automatically.
- **Tools:** Claude is given `query_pos`, `summary`, and `list_dimensions`.
  The worker executes each tool call against the in-memory data and feeds
  results back until Claude produces a final answer.
