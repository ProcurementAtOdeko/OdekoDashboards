"""One-time helper: run the OAuth flow and cache a token so the MCP server starts headlessly."""

from server import _get_service, TOKEN_PATH

if __name__ == "__main__":
    _get_service()
    print(f"Authorized. Token cached at {TOKEN_PATH}")
