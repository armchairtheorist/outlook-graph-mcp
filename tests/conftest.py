"""Shared fixtures: an app wired to a fake token file and a respx mock of Graph."""

from __future__ import annotations

import json
import os

import pytest
import respx

os.environ.update(
    {
        "ENTRA_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
        "ENTRA_CLIENT_SECRET": "secret",
        "JWT_SIGNING_KEY": "0123456789abcdef0123456789abcdef",
        "TOKEN_STORE": "file",
        "BASE_URL": "http://localhost:8000",
    }
)

from graph_mcp.config import Settings  # noqa: E402
from graph_mcp.server import build_app  # noqa: E402

GRAPH = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def app(tmp_path, monkeypatch):
    token_file = tmp_path / "tok.json"
    token_file.write_text(
        json.dumps({"refresh_token": "rt", "oid": "owner-oid", "username": "me@live.com"})
    )
    settings = Settings(TOKEN_FILE=str(token_file))  # type: ignore[call-arg]
    mcp, graph = build_app(settings)
    # Skip MSAL: pretend we already hold a fresh access token.
    graph._access_token, graph._expires_at = "at", 10**12
    return mcp


@pytest.fixture
def graph_mock():
    with respx.mock(base_url=GRAPH, assert_all_called=False) as m:
        yield m
