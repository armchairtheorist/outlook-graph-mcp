"""Tool-level tests with Graph mocked by respx. Auth is bypassed by calling tools in-process."""

from __future__ import annotations

import json
import os

import pytest
import respx
from fastmcp import Client
from httpx import Response

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


async def test_mail_list(app, graph_mock):
    graph_mock.get("/me/mailFolders/inbox/messages").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "m1",
                        "subject": "Hello",
                        "from": {"emailAddress": {"name": "Alice", "address": "a@x.com"}},
                        "toRecipients": [],
                        "receivedDateTime": "2026-10-04T01:00:00Z",
                        "isRead": False,
                        "importance": "normal",
                        "bodyPreview": "hi",
                        "hasAttachments": False,
                        "flag": {"flagStatus": "notFlagged"},
                    }
                ]
            },
        )
    )
    async with Client(app) as c:
        r = await c.call_tool("mail_list", {"unread_only": True})
    rows = json.loads(r.content[0].text)
    assert rows[0]["from"] == "Alice <a@x.com>" and rows[0]["isRead"] is False
    req = graph_mock.calls[0].request
    assert "isRead eq false" in req.url.params["$filter"]
    assert req.headers["authorization"] == "Bearer at"


async def test_mail_get_text_body(app, graph_mock):
    graph_mock.get("/me/messages/m1").mock(
        return_value=Response(
            200,
            json={
                "id": "m1",
                "subject": "S",
                "body": {"contentType": "html", "content": "<p>Hi <b>there</b></p><br>bye"},
                "attachments": [
                    {
                        "id": "a1",
                        "name": "f.pdf",
                        "contentType": "application/pdf",
                        "size": 10,
                        "isInline": False,
                    }
                ],
            },
        )
    )
    async with Client(app) as c:
        r = await c.call_tool("mail_get", {"message_id": "m1"})
    d = json.loads(r.content[0].text)
    assert d["body"] == "Hi there\n\nbye" and d["attachments"][0]["name"] == "f.pdf"


async def test_mail_send_payload(app, graph_mock):
    route = graph_mock.post("/me/sendMail").mock(return_value=Response(202))
    async with Client(app) as c:
        await c.call_tool(
            "mail_send", {"to": ["Bob <b@x.com>", "c@x.com"], "subject": "Yo", "body": "text"}
        )
    sent = json.loads(route.calls[0].request.content)
    assert sent["message"]["toRecipients"] == [
        {"emailAddress": {"name": "Bob", "address": "b@x.com"}},
        {"emailAddress": {"address": "c@x.com"}},
    ]
    assert sent["message"]["body"]["contentType"] == "Text"


async def test_free_slots(app, graph_mock):
    graph_mock.get("/me/calendarView").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "start": {"dateTime": "2026-10-05T02:00:00.0000000"},
                        "end": {"dateTime": "2026-10-05T03:00:00.0000000"},
                        "showAs": "busy",
                    },  # 10:00-11:00 SGT
                ]
            },
        )
    )
    async with Client(app) as c:
        r = await c.call_tool(
            "calendar_find_free_slots",
            {
                "start": "2026-10-05T09:00:00+08:00",
                "end": "2026-10-05T12:00:00+08:00",
                "duration_minutes": 60,
                "max_results": 5,
            },
        )
    slots = json.loads(r.content[0].text)
    starts = [s["start"] for s in slots]
    assert starts == ["2026-10-05T09:00:00+08:00", "2026-10-05T11:00:00+08:00"]


async def test_webhook_validation_and_notification(app):
    import httpx

    asgi = app.http_app(path="/mcp")
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://t") as hc,
        asgi.router.lifespan_context(asgi),
    ):
        r = await hc.post("/webhooks?validationToken=abc")
        assert r.status_code == 200 and r.text == "abc"
        r = await hc.post(
            "/webhooks",
            json={
                "value": [
                    {
                        "changeType": "created",
                        "resource": "me/messages/x",
                        "resourceData": {"id": "x"},
                    }
                ]
            },
        )
        assert r.status_code == 202
        r = await hc.get("/healthz")
        assert r.json()["owner"] == "me@live.com"
        # MCP endpoint must demand auth
        r = await hc.post("/mcp", json={})
        assert r.status_code == 401
