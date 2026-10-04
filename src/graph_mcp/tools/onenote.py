"""OneNote tools. Pages are HTML; we return them as text and accept Markdown-ish/HTML on write."""

from __future__ import annotations

import html
import re
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from ..graph import GraphClient
from ._common import compact, html_to_text

ON = "/me/onenote"
_BULLET = re.compile(r"^[-*]\s+")


def _nb(n: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": n.get("id"),
            "name": n.get("displayName"),
            "isDefault": n.get("isDefault") or None,
            "modified": n.get("lastModifiedDateTime"),
            "webUrl": (n.get("links") or {}).get("oneNoteWebUrl", {}).get("href"),
        }
    )


def _sec(s: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": s.get("id"),
            "name": s.get("displayName"),
            "modified": s.get("lastModifiedDateTime"),
            "notebook": (s.get("parentNotebook") or {}).get("displayName"),
            "notebookId": (s.get("parentNotebook") or {}).get("id"),
        }
    )


def _page(p: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "created": p.get("createdDateTime"),
            "modified": p.get("lastModifiedDateTime"),
            "section": (p.get("parentSection") or {}).get("displayName"),
            "sectionId": (p.get("parentSection") or {}).get("id"),
            "webUrl": (p.get("links") or {}).get("oneNoteWebUrl", {}).get("href"),
        }
    )


def _to_html(body: str) -> str:
    """Accept plain text, light Markdown (#, -, *, **bold**, `code`) or raw HTML."""
    if re.search(r"<(p|div|h[1-6]|ul|ol|table|br)\b", body, re.I):
        return body
    out: list[str] = []
    in_list = False
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        esc = html.escape(line.strip())
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`(.+?)`", r"<code>\1</code>", esc)
        m = re.match(r"^(#{1,3})\s+(.*)", esc)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>")
        elif re.match(r"^[-*]\s+", esc):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_BULLET.sub('', esc)}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def register(mcp: FastMCP, graph: GraphClient) -> None:
    @mcp.tool(tags={"notes"})
    async def onenote_list_notebooks() -> list[dict[str, Any]]:
        """All notebooks."""
        return [_nb(n) for n in await graph.list(f"{ON}/notebooks", limit=100)]

    @mcp.tool(tags={"notes"})
    async def onenote_list_sections(notebook_id: str | None = None) -> list[dict[str, Any]]:
        """Sections, in one notebook or across all."""
        path = f"{ON}/notebooks/{notebook_id}/sections" if notebook_id else f"{ON}/sections"
        return [
            _sec(s)
            for s in await graph.list(
                path, params={"$expand": "parentNotebook($select=id,displayName)"}, limit=200
            )
        ]

    @mcp.tool(tags={"notes"})
    async def onenote_list_pages(
        section_id: str | None = None,
        top: Annotated[int, Field(ge=1, le=200)] = 50,
        search: Annotated[
            str | None, Field(description="Full-text search across pages (ignores section_id)")
        ] = None,
    ) -> list[dict[str, Any]]:
        """Pages in a section, newest first; or search all pages."""
        params: dict[str, Any] = {
            "$top": min(top, 100),
            "$orderby": "lastModifiedDateTime desc",
            "$expand": "parentSection($select=id,displayName)",
        }
        if search:
            params["$search"] = search
            path = f"{ON}/pages"
        else:
            path = f"{ON}/sections/{section_id}/pages" if section_id else f"{ON}/pages"
        return [_page(p) for p in await graph.list(path, params=params, limit=top)]

    @mcp.tool(tags={"notes"})
    async def onenote_get_page(
        page_id: str,
        as_html: bool = False,
        max_chars: Annotated[int, Field(ge=500, le=200_000)] = 40_000,
    ) -> dict[str, Any]:
        """Read a page's content (text by default)."""
        meta = await graph.get(
            f"{ON}/pages/{page_id}", params={"$expand": "parentSection($select=id,displayName)"}
        )
        raw: bytes = await graph.request(
            "GET", f"{ON}/pages/{page_id}/content", params={"includeIDs": "true"}
        )
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        body = content if as_html else html_to_text(content)
        out = _page(meta)
        out["content"] = body[:max_chars] + ("\n…[truncated]" if len(body) > max_chars else "")
        return out

    @mcp.tool(tags={"notes", "write"})
    async def onenote_create_page(
        section_id: str,
        title: str,
        body: Annotated[
            str, Field(description="Plain text, light Markdown (#, -, **bold**, `code`) or HTML")
        ],
    ) -> dict[str, Any]:
        """Create a page in a section."""
        page_html = f"<!DOCTYPE html><html><head><title>{html.escape(title)}</title></head><body>{_to_html(body)}</body></html>"
        r = await graph.request(
            "POST",
            f"{ON}/sections/{section_id}/pages",
            content=page_html.encode("utf-8"),
            headers={"Content-Type": "application/xhtml+xml"},
        )
        return _page(r)

    @mcp.tool(tags={"notes", "write"})
    async def onenote_append_to_page(
        page_id: str,
        body: Annotated[str, Field(description="Plain text, light Markdown or HTML to add")],
        position: Annotated[
            str, Field(description="'append' (end of page) or 'prepend' (top)")
        ] = "append",
    ) -> str:
        """Add content to the end (or top) of an existing page."""
        await graph.patch(
            f"{ON}/pages/{page_id}/content",
            [{"target": "body", "action": position, "content": _to_html(body)}],
        )
        return f"Updated page {page_id}"

    @mcp.tool(tags={"notes", "write"})
    async def onenote_create_section(notebook_id: str, name: str) -> dict[str, Any]:
        """Create a section in a notebook."""
        return _sec(
            await graph.post(f"{ON}/notebooks/{notebook_id}/sections", {"displayName": name})
        )

    @mcp.tool(tags={"notes", "write"})
    async def onenote_delete_page(page_id: str) -> str:
        """Delete a page (OneNote keeps it in the notebook's recycle bin for 60 days)."""
        await graph.delete(f"{ON}/pages/{page_id}")
        return f"Deleted page {page_id}"
