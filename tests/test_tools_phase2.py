"""Tests for drive, onenote, todo, contacts tools (Graph mocked)."""

from __future__ import annotations

import base64
import json

import respx
from fastmcp import Client
from httpx import Response

from graph_mcp.tools.onenote import _to_html


async def _call(app, name, **args):
    async with Client(app) as c:
        r = await c.call_tool(name, args)
    return json.loads(r.content[0].text)


# ---------------------------------------------------------------- drive
async def test_drive_list_path_addressing(app, graph_mock):
    route = graph_mock.get("/me/drive/root:/Documents/Taxes:/children").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "f1",
                        "name": "2025.pdf",
                        "size": 10,
                        "file": {"mimeType": "application/pdf"},
                        "parentReference": {"path": "/drive/root:/Documents/Taxes"},
                    },
                    {
                        "id": "d1",
                        "name": "Old",
                        "folder": {"childCount": 3},
                        "parentReference": {"path": "/drive/root:/Documents/Taxes"},
                    },
                ]
            },
        )
    )
    rows = await _call(app, "drive_list", path="/Documents/Taxes/")
    assert route.called
    assert [r["type"] for r in rows] == ["folder", "file"]  # folders first
    assert rows[1]["path"] == "Documents/Taxes/2025.pdf"


async def test_drive_read_text_file(app, graph_mock):
    graph_mock.get("/me/drive/root:/notes.md:").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "name": "notes.md",
                "size": 5,
                "file": {"mimeType": "application/octet-stream"},
            },
        )
    )
    graph_mock.get("/me/drive/items/x/content").mock(
        return_value=Response(
            200, content=b"hello", headers={"content-type": "application/octet-stream"}
        )
    )
    d = await _call(app, "drive_read_file", path="notes.md")
    assert d["text"] == "hello"  # .md extension wins over octet-stream mime


async def test_drive_upload_small_and_large(app, graph_mock):
    put = graph_mock.put("/me/drive/root:/a/b.txt:/content").mock(
        return_value=Response(
            201, json={"id": "n", "name": "b.txt", "parentReference": {"path": "/drive/root:/a"}}
        )
    )
    d = await _call(app, "drive_upload_text", path="/a/b.txt", content="hi")
    assert d["path"] == "a/b.txt" and put.calls[0].request.content == b"hi"
    assert "conflictBehavior=replace" in str(put.calls[0].request.url)

    big = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
    graph_mock.post("/me/drive/root:/big.bin:/createUploadSession").mock(
        return_value=Response(200, json={"uploadUrl": "https://up.example/sess"})
    )
    with respx.mock(assert_all_called=True) as up:
        up.put("https://up.example/sess").mock(
            side_effect=[
                Response(202, json={}),
                Response(
                    201,
                    json={
                        "id": "B",
                        "name": "big.bin",
                        "parentReference": {"path": "/drive/root:"},
                    },
                ),
            ]
        )
        d = await _call(app, "drive_upload_base64", path="big.bin", base64_content=big)
        assert d["name"] == "big.bin"
        ranges = [c.request.headers["Content-Range"] for c in up.calls]
        assert ranges[0].startswith("bytes 0-5242879/") and ranges[1].startswith(
            "bytes 5242880-6291455/"
        )


async def test_drive_share_link(app, graph_mock):
    graph_mock.post("/me/drive/items/x/createLink").mock(
        return_value=Response(201, json={"link": {"webUrl": "https://1drv.ms/abc"}})
    )
    d = await _call(app, "drive_share_link", item_id="x", link_type="edit")
    assert d["url"] == "https://1drv.ms/abc" and d["type"] == "edit"


# -------------------------------------------------------------- onenote
def test_markdown_to_html():
    html = _to_html("# Title\nSome **bold** text\n- one\n- two\n\nAfter")
    assert (
        html
        == "<h1>Title</h1>\n<p>Some <b>bold</b> text</p>\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>\n<p>After</p>"
    )
    assert _to_html("<p>raw</p>") == "<p>raw</p>"
    assert "&lt;script&gt;" in _to_html(
        "<script>alert(1)</script> hi"
    )  # escaped: not an HTML block tag


async def test_onenote_create_and_read(app, graph_mock):
    post = graph_mock.post("/me/onenote/sections/s1/pages").mock(
        return_value=Response(201, json={"id": "p1", "title": "T"})
    )
    d = await _call(app, "onenote_create_page", section_id="s1", title="T", body="hello **w**")
    assert d["id"] == "p1"
    body = post.calls[0].request.content.decode()
    assert "<title>T</title>" in body and "<p>hello <b>w</b></p>" in body
    assert post.calls[0].request.headers["content-type"] == "application/xhtml+xml"

    graph_mock.get("/me/onenote/pages/p1").mock(
        return_value=Response(200, json={"id": "p1", "title": "T"})
    )
    graph_mock.get("/me/onenote/pages/p1/content").mock(
        return_value=Response(
            200,
            content=b"<html><body><p>Hi</p><p>there</p></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    d = await _call(app, "onenote_get_page", page_id="p1")
    assert d["content"] == "Hi\nthere"


# ----------------------------------------------------------------- todo
async def test_todo_list_tasks_sorted_and_filtered(app, graph_mock):
    route = graph_mock.get("/me/todo/lists/L/tasks").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "a",
                        "title": "No date",
                        "status": "notStarted",
                        "createdDateTime": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "b",
                        "title": "Later",
                        "status": "notStarted",
                        "dueDateTime": {"dateTime": "2026-10-20T00:00:00.0000000"},
                    },
                    {
                        "id": "c",
                        "title": "Soon",
                        "status": "inProgress",
                        "dueDateTime": {"dateTime": "2026-10-06T00:00:00.0000000"},
                        "checklistItems": [{"id": "k", "displayName": "step", "isChecked": True}],
                    },
                ]
            },
        )
    )
    rows = await _call(app, "todo_list_tasks", list_id="L")
    assert route.calls[0].request.url.params["$filter"] == "status ne 'completed'"
    assert [r["id"] for r in rows] == ["c", "b", "a"]
    assert rows[0]["due"] == "2026-10-06" and rows[0]["checklist"][0]["done"] is True


async def test_todo_create_with_checklist(app, graph_mock):
    graph_mock.post("/me/todo/lists/L/tasks").mock(
        return_value=Response(201, json={"id": "t1", "title": "Buy"})
    )
    items = graph_mock.post("/me/todo/lists/L/tasks/t1/checklistItems").mock(
        return_value=Response(201, json={})
    )
    graph_mock.get("/me/todo/lists/L/tasks/t1").mock(
        return_value=Response(200, json={"id": "t1", "title": "Buy", "status": "notStarted"})
    )
    await _call(
        app,
        "todo_create_task",
        list_id="L",
        title="Buy",
        due="2026-10-10",
        checklist=["milk", "eggs"],
    )
    assert items.call_count == 2
    created = json.loads(graph_mock.calls[0].request.content)
    assert created["dueDateTime"] == {
        "dateTime": "2026-10-10T09:00:00",
        "timeZone": "Singapore Standard Time",
    }


# ------------------------------------------------------------- contacts
async def test_contacts_search_and_create(app, graph_mock):
    graph_mock.get("/me/contacts").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "id": "c1",
                        "displayName": "Ada Lovelace",
                        "emailAddresses": [{"address": "ada@x.org"}],
                        "businessPhones": ["+65 1234"],
                        "homeAddress": {"city": "London", "countryOrRegion": "UK"},
                    },
                ]
            },
        )
    )
    rows = await _call(app, "contacts_search", query="ada")
    assert rows[0]["emails"] == ["ada@x.org"] and rows[0]["homeAddress"] == "London, UK"

    post = graph_mock.post("/me/contacts").mock(
        return_value=Response(201, json={"id": "c2", "displayName": "Bob Ross"})
    )
    await _call(
        app,
        "contacts_create",
        given_name="Bob",
        surname="Ross",
        emails=["bob@x.org"],
        birthday="1942-10-29",
    )
    sent = json.loads(post.calls[0].request.content)
    assert sent["emailAddresses"] == [{"address": "bob@x.org", "name": "Bob Ross"}]
    assert sent["birthday"] == "1942-10-29T00:00:00Z"
