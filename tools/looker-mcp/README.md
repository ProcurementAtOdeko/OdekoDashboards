# looker-mcp

Read-only MCP server over the Looker API 4.0, registered as `looker` in
`.mcp.json`.

## Credentials

Looker uses an **API3 credential pair**, not a single API key. Generate one in
Looker: **Admin → Users → (your user) → Edit → API Keys → New API Key**. You get
a Client ID and a Client Secret; the secret is only shown once.

Three environment variables are needed:

| Variable | Example |
| --- | --- |
| `LOOKER_BASE_URL` | `https://odeko.cloud.looker.com` |
| `LOOKER_CLIENT_ID` | (from the API key pair) |
| `LOOKER_CLIENT_SECRET` | (from the API key pair) |

The `LOOKERSDK_BASE_URL` / `LOOKERSDK_CLIENT_ID` / `LOOKERSDK_CLIENT_SECRET`
names used by Looker's own SDK are accepted as aliases.

**Do not commit these or paste them into chat.** Set them where the consumer
runs:

- **Claude Code on the web** — the environment's variables settings, alongside
  the existing `GOOGLE_SERVICE_ACCOUNT_JSON`.
- **GitHub Actions** — as repo secrets, then map them into the workflow's `env:`
  the same way `GOOGLE_SERVICE_ACCOUNT_JSON` is today.
- **Local shell** — export them before starting Claude Code.

The API key inherits the permissions of the Looker user it belongs to, so a
dedicated service user with a read-only role is preferable to a personal key.

## Setup

`.venv/` is gitignored, so each machine builds its own:

```
python3 -m venv tools/looker-mcp/.venv
tools/looker-mcp/.venv/bin/pip install -r tools/looker-mcp/requirements.txt
```

Verify the credentials resolve:

```
tools/looker-mcp/.venv/bin/python -c "import server; print(server.whoami())"
```

> `mcp` is pinned to `<2` — version 2.0 removed `mcp.server.fastmcp`, which both
> servers in `tools/` are written against. Unpinning breaks them at import.

## Tools

| Tool | Purpose |
| --- | --- |
| `whoami` | Confirm which Looker user the credentials authenticate as |
| `list_models` | LookML models and their explores |
| `get_explore` | Dimensions and measures for one explore, with queryable field names |
| `run_query` | Ad-hoc query against an explore (`json`, `csv`, `md`, `txt`) |
| `list_connections` | Database connections available to `run_sql` |
| `run_sql` | Raw SQL against a connection (e.g. the warehouse) |
| `search_looks` / `run_look` | Find and run saved Looks |
| `search_dashboards` / `get_dashboard` | Find dashboards and inspect the query behind each tile |

Writes are deliberately not exposed — this server feeds dashboards under
`dashboards/`, it does not mutate LookML or saved content.

## Using it for a dashboard

`get_dashboard` is the useful starting point when porting an existing Looker
dashboard: it returns each tile's model, explore, fields, and filters, which map
directly onto a `run_query` call in a `build_data.py` script.
