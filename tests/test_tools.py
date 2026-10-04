"""Tool-level tests with Graph mocked by respx. Auth is bypassed by calling tools in-process."""

from __future__ import annotations

import json

from fastmcp import Client
from httpx import Response


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


def test_expected_issuer_pins_msa_tenant():
    from graph_mcp.auth import MSA_TENANT_ID, expected_issuer

    assert expected_issuer("consumers") == f"https://login.microsoftonline.com/{MSA_TENANT_ID}/v2.0"
    assert expected_issuer("abc-123") == "https://login.microsoftonline.com/abc-123/v2.0"


def test_provider_uses_pinned_issuer(app):
    from graph_mcp.auth import MSA_TENANT_ID

    assert MSA_TENANT_ID in str(app.auth._token_validator.issuer)


async def test_thread_has_no_orderby_and_sorts_locally(app, graph_mock):
    route = graph_mock.get("/me/messages").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "b",
                        "receivedDateTime": "2026-10-02T00:00:00Z",
                        "body": {"content": "second"},
                    },
                    {
                        "id": "a",
                        "receivedDateTime": "2026-10-01T00:00:00Z",
                        "body": {"content": "first"},
                    },
                ]
            },
        )
    )
    async with Client(app) as c:
        r = await c.call_tool("mail_get_thread", {"conversation_id": "conv1"})
    rows = json.loads(r.content[0].text)
    assert (
        "$orderby" not in route.calls[0].request.url.params
    )  # Graph rejects it here (InefficientFilter)
    assert [m["id"] for m in rows] == ["a", "b"]


async def test_calendar_search_uses_filter_and_local_scan(app, graph_mock):
    ev = graph_mock.get("/me/events").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "e1",
                        "subject": "Team meeting",
                        "start": {"dateTime": "2026-10-10T01:00:00"},
                    }
                ]
            },
        )
    )
    graph_mock.get("/me/calendarView").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "e1",
                        "subject": "Team meeting",
                        "start": {"dateTime": "2026-10-10T01:00:00"},
                    },
                    {
                        "id": "e2",
                        "subject": "Lunch",
                        "bodyPreview": "pre-meeting sync",
                        "start": {"dateTime": "2026-10-09T01:00:00"},
                    },
                    {
                        "id": "e3",
                        "subject": "Dentist",
                        "start": {"dateTime": "2026-10-08T01:00:00"},
                    },
                ]
            },
        )
    )
    async with Client(app) as c:
        r = await c.call_tool("calendar_search", {"query": "meeting"})
    rows = json.loads(r.content[0].text)
    assert ev.calls[0].request.url.params["$filter"] == "contains(subject,'meeting')"
    assert "$search" not in ev.calls[0].request.url.params
    assert [e["id"] for e in rows] == ["e2", "e1"]  # body match found locally, sorted by start
