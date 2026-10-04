#!/usr/bin/env python3
"""One-time interactive sign-in as the mailbox owner. Stores the Graph refresh token.

Usage (from the repo root, with .env populated or env vars exported):

    # local dev: writes .graph_token.json
    python scripts/seed_token.py

    # production: writes straight into Key Vault (you must have `az login` + Key Vault
    # Secrets Officer on the vault)
    TOKEN_STORE=keyvault KEYVAULT_URL=https://<vault>.vault.azure.net python scripts/seed_token.py

A browser window opens for the Microsoft login. Sign in with the personal account whose
mailbox Claude should manage. The redirect URI http://localhost:8400 must be registered on
the Entra app (platform: Web).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import msal  # noqa: E402

from graph_mcp.config import GRAPH_SCOPES, get_settings  # noqa: E402
from graph_mcp.token_store import StoredToken, build_token_store  # noqa: E402

REDIRECT_PORT = 8400


def main() -> int:
    s = get_settings()
    store = build_token_store(
        s.token_store,
        file_path=s.token_file,
        keyvault_url=s.keyvault_url,
        secret_name=s.keyvault_secret_name,
    )
    app = msal.ConfidentialClientApplication(
        s.entra_client_id, client_credential=s.entra_client_secret, authority=s.authority
    )
    print(f"Opening browser for sign-in ({s.authority}) ...")
    result = _auth_code_flow(app)
    if "refresh_token" not in result:
        print("Sign-in failed:", result.get("error"), result.get("error_description"))
        return 1
    claims = result.get("id_token_claims", {})
    token = StoredToken(
        refresh_token=result["refresh_token"],
        oid=claims.get("oid", ""),
        username=claims.get("preferred_username", ""),
        home_account_id=(result.get("account") or {}).get("home_account_id"),
    )
    store.save(token)
    print(f"Stored refresh token for {token.username} (oid {token.oid}) in {s.token_store} store.")
    print("This account is now the only one allowed to use the MCP server.")
    return 0


def _auth_code_flow(app: msal.ConfidentialClientApplication) -> dict:
    """Run the auth-code flow with a minimal local redirect listener."""
    import http.server
    import urllib.parse
    import webbrowser

    flow = app.initiate_auth_code_flow(
        GRAPH_SCOPES, redirect_uri=f"http://localhost:{REDIRECT_PORT}", prompt="select_account"
    )
    webbrowser.open(flow["auth_uri"])
    print("If no browser opened, visit:\n", flow["auth_uri"])
    captured: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            captured.update(
                {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(
                        urllib.parse.urlparse(self.path).query
                    ).items()
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *_):  # silence
            pass

    with http.server.HTTPServer(("localhost", REDIRECT_PORT), H) as srv:
        while not captured:
            srv.handle_request()
    return app.acquire_token_by_auth_code_flow(flow, captured)


if __name__ == "__main__":
    raise SystemExit(main())
