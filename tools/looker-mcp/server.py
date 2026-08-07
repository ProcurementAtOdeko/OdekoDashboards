"""MCP server exposing Looker API 4.0 read tools via FastMCP.

Auth is Looker's standard API3 credential pair (Admin -> Users -> Edit ->
API Keys), read from the environment:

  LOOKER_BASE_URL       e.g. https://odeko.cloud.looker.com
  LOOKER_CLIENT_ID
  LOOKER_CLIENT_SECRET

The LOOKERSDK_* names used by Looker's own SDK are accepted as aliases, so a
machine already configured for looker_sdk works without extra setup.

Tools are read-only on purpose: this server is meant to feed dashboards under
dashboards/, not to mutate LookML or saved content.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

API_VERSION = "4.0"
DEFAULT_ROW_LIMIT = 500

mcp = FastMCP("looker")

_session: requests.Session | None = None
_token: str | None = None
_token_expires_at: float = 0.0


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def _base_url() -> str:
    url = _env("LOOKER_BASE_URL", "LOOKERSDK_BASE_URL")
    if not url:
        raise RuntimeError(
            "LOOKER_BASE_URL is not set. Point it at the Looker instance, "
            "e.g. https://odeko.cloud.looker.com"
        )
    return url.rstrip("/")


def _credentials() -> tuple[str, str]:
    client_id = _env("LOOKER_CLIENT_ID", "LOOKERSDK_CLIENT_ID")
    client_secret = _env("LOOKER_CLIENT_SECRET", "LOOKERSDK_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "LOOKER_CLIENT_ID / LOOKER_CLIENT_SECRET are not set. Generate an "
            "API key pair in Looker under Admin -> Users -> Edit -> API Keys."
        )
    return client_id, client_secret


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        ca_bundle = _env("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
        if ca_bundle:
            _session.verify = ca_bundle
    return _session


def _access_token() -> str:
    """Log in with the API3 pair, reusing the token until shortly before expiry."""
    global _token, _token_expires_at

    if _token and time.monotonic() < _token_expires_at:
        return _token

    client_id, client_secret = _credentials()
    res = _get_session().post(
        f"{_base_url()}/api/{API_VERSION}/login",
        data={"client_id": client_id, "client_secret": client_secret},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(
            f"Looker login failed ({res.status_code}). Check the base URL and "
            f"API credentials. Response: {res.text[:300]}"
        )

    payload = res.json()
    _token = payload["access_token"]
    # Renew a minute early so a long-running call never races the expiry.
    _token_expires_at = time.monotonic() + max(int(payload.get("expires_in", 3600)) - 60, 60)
    return _token


def _request(method: str, path: str, **kwargs: Any) -> requests.Response:
    res = _get_session().request(
        method,
        f"{_base_url()}/api/{API_VERSION}{path}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=kwargs.pop("timeout", 120),
        **kwargs,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Looker {method} {path} -> {res.status_code}: {res.text[:500]}")
    return res


def _json(method: str, path: str, **kwargs: Any) -> Any:
    return _request(method, path, **kwargs).json()


@mcp.tool()
def list_models() -> list[dict[str, Any]]:
    """List LookML models available to these credentials, with each model's explores."""
    models = _json("GET", "/lookml_models", params={"fields": "name,label,explores"})
    return [
        {
            "name": m.get("name"),
            "label": m.get("label"),
            "explores": [
                {"name": e.get("name"), "label": e.get("label")}
                for e in (m.get("explores") or [])
            ],
        }
        for m in models
    ]


@mcp.tool()
def get_explore(model: str, explore: str) -> dict[str, Any]:
    """Describe an explore's queryable fields.

    Returns dimensions and measures with the fully-qualified names that
    run_query expects (e.g. 'orders.created_date').
    """
    data = _json("GET", f"/lookml_models/{model}/explores/{explore}")
    fields = data.get("fields") or {}

    def summarize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": f.get("name"),
                "label": f.get("label_short") or f.get("label"),
                "type": f.get("type"),
                "description": f.get("description"),
            }
            for f in items
            if not f.get("hidden")
        ]

    return {
        "model": model,
        "explore": explore,
        "label": data.get("label"),
        "description": data.get("description"),
        "dimensions": summarize(fields.get("dimensions") or []),
        "measures": summarize(fields.get("measures") or []),
    }


@mcp.tool()
def run_query(
    model: str,
    explore: str,
    fields: list[str],
    filters: dict[str, str] | None = None,
    sorts: list[str] | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
    result_format: str = "json",
) -> Any:
    """Run an ad-hoc query against an explore and return the rows.

    `fields` and `sorts` use fully-qualified names ('orders.count',
    'orders.created_date desc'). `filters` maps a field name to a Looker
    filter expression, e.g. {'orders.created_date': 'last 30 days'}.
    `result_format` is one of json, csv, md, txt.
    """
    body = {
        "model": model,
        "view": explore,
        "fields": fields,
        "limit": str(limit),
    }
    if filters:
        body["filters"] = filters
    if sorts:
        body["sorts"] = sorts

    res = _request("POST", f"/queries/run/{result_format}", json=body)
    return res.json() if result_format == "json" else res.text


@mcp.tool()
def run_sql(connection: str, sql: str, result_format: str = "json") -> Any:
    """Run raw SQL against a Looker database connection (e.g. the warehouse).

    Two-step by design in Looker's API: the query is created, then run.
    """
    created = _json(
        "POST", "/sql_queries", json={"connection_name": connection, "sql": sql}
    )
    slug = created["slug"]
    res = _request("POST", f"/sql_queries/{slug}/run/{result_format}")
    return res.json() if result_format == "json" else res.text


@mcp.tool()
def list_connections() -> list[dict[str, Any]]:
    """List database connections available for run_sql."""
    connections = _json(
        "GET", "/connections", params={"fields": "name,dialect_name,database,host"}
    )
    return [
        {
            "name": c.get("name"),
            "dialect": c.get("dialect_name"),
            "database": c.get("database"),
        }
        for c in connections
    ]


@mcp.tool()
def search_looks(title: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Find saved Looks, optionally filtering by a title substring."""
    params: dict[str, Any] = {
        "fields": "id,title,description,model(id),updated_at",
        "limit": limit,
    }
    if title:
        params["title"] = f"%{title}%"
    looks = _json("GET", "/looks/search", params=params)
    return [
        {
            "id": look.get("id"),
            "title": look.get("title"),
            "description": look.get("description"),
            "model": (look.get("model") or {}).get("id"),
            "updated_at": look.get("updated_at"),
        }
        for look in looks
    ]


@mcp.tool()
def run_look(look_id: str, result_format: str = "json", limit: int | None = None) -> Any:
    """Run a saved Look by id and return its rows."""
    params = {"limit": limit} if limit else None
    res = _request("GET", f"/looks/{look_id}/run/{result_format}", params=params)
    return res.json() if result_format == "json" else res.text


@mcp.tool()
def search_dashboards(title: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Find dashboards, optionally filtering by a title substring."""
    params: dict[str, Any] = {
        "fields": "id,title,description,updated_at",
        "limit": limit,
    }
    if title:
        params["title"] = f"%{title}%"
    dashboards = _json("GET", "/dashboards/search", params=params)
    return [
        {
            "id": d.get("id"),
            "title": d.get("title"),
            "description": d.get("description"),
            "updated_at": d.get("updated_at"),
        }
        for d in dashboards
    ]


@mcp.tool()
def get_dashboard(dashboard_id: str) -> dict[str, Any]:
    """Describe a dashboard's tiles, including the query behind each one."""
    data = _json("GET", f"/dashboards/{dashboard_id}")
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "description": data.get("description"),
        "filters": [
            {"name": f.get("name"), "title": f.get("title"), "default": f.get("default_value")}
            for f in (data.get("dashboard_filters") or [])
        ],
        "tiles": [
            {
                "title": el.get("title"),
                "type": el.get("type"),
                "look_id": el.get("look_id"),
                "query": {
                    "model": (el.get("query") or {}).get("model"),
                    "explore": (el.get("query") or {}).get("view"),
                    "fields": (el.get("query") or {}).get("fields"),
                    "filters": (el.get("query") or {}).get("filters"),
                }
                if el.get("query")
                else None,
            }
            for el in (data.get("dashboard_elements") or [])
        ],
    }


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Return the Looker user these credentials authenticate as. Useful for checking setup."""
    me = _json("GET", "/user", params={"fields": "id,display_name,email,role_ids"})
    return {
        "base_url": _base_url(),
        "id": me.get("id"),
        "display_name": me.get("display_name"),
        "email": me.get("email"),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
