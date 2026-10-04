"""OneDrive (personal) tools.

Paths are OneDrive-relative, e.g. "Documents/Taxes/2025.pdf". Ids are Graph driveItem ids.
Every tool accepts either via `path` or `item_id`. Large uploads use an upload session.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..graph import GraphClient
from ._common import compact

ITEM_SELECT = (
    "id,name,size,lastModifiedDateTime,createdDateTime,webUrl,folder,file,parentReference,shared"
)
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript")
TEXT_EXT = (
    ".md",
    ".txt",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".py",
    ".js",
    ".ts",
    ".log",
)
CHUNK = 5 * 1024 * 1024  # upload-session chunk (multiple of 320 KiB as Graph requires)


def _ref(path: str | None, item_id: str | None, suffix: str = "") -> str:
    """Build /me/drive/... address from a path or id."""
    if item_id:
        return f"/me/drive/items/{item_id}{suffix}"
    if path is None or path.strip("/") == "":
        return f"/me/drive/root{suffix}"
    return f"/me/drive/root:/{path.strip('/')}:{suffix}"


def _item(i: dict[str, Any]) -> dict[str, Any]:
    parent = (i.get("parentReference") or {}).get("path", "")
    folder_path = parent.split("root:", 1)[1] if "root:" in parent else ""
    return compact(
        {
            "id": i.get("id"),
            "name": i.get("name"),
            "path": f"{folder_path}/{i.get('name')}".lstrip("/") if i.get("name") else None,
            "type": "folder" if "folder" in i else "file",
            "size": i.get("size"),
            "childCount": (i.get("folder") or {}).get("childCount"),
            "mimeType": (i.get("file") or {}).get("mimeType"),
            "modified": i.get("lastModifiedDateTime"),
            "shared": bool(i.get("shared")) or None,
            "webUrl": i.get("webUrl"),
        }
    )


def register(mcp: FastMCP, graph: GraphClient) -> None:
    @mcp.tool(tags={"drive"})
    async def drive_list(
        path: Annotated[str, Field(description="Folder path, '' for root")] = "",
        item_id: str | None = None,
        top: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List a folder's contents (folders first, then files)."""
        items = await graph.list(
            _ref(path, item_id, "/children"),
            params={"$select": ITEM_SELECT, "$top": min(top, 200), "$orderby": "name"},
            limit=top,
        )
        out = [_item(i) for i in items]
        return sorted(out, key=lambda x: (x["type"] != "folder", x["name"].lower()))

    @mcp.tool(tags={"drive"})
    async def drive_search(
        query: str, top: Annotated[int, Field(ge=1, le=100)] = 25
    ) -> list[dict[str, Any]]:
        """Search the whole drive by file name and (for Office/PDF/text) content."""
        items = await graph.list(
            f"/me/drive/root/search(q='{query.replace(chr(39), chr(39) * 2)}')",
            params={"$select": ITEM_SELECT, "$top": min(top, 50)},
            limit=top,
        )
        return [_item(i) for i in items]

    @mcp.tool(tags={"drive"})
    async def drive_get(path: str | None = None, item_id: str | None = None) -> dict[str, Any]:
        """Metadata for one file or folder."""
        return _item(await graph.get(_ref(path, item_id), params={"$select": ITEM_SELECT}))

    @mcp.tool(tags={"drive"})
    async def drive_read_file(
        path: str | None = None,
        item_id: str | None = None,
        max_bytes: Annotated[int, Field(ge=1024, le=20_000_000)] = 2_000_000,
        force_base64: bool = False,
    ) -> dict[str, Any]:
        """Download a file. Text-like files come back as text; others as base64 (keep max_bytes
        small for those). For Office docs prefer drive_file_as_pdf_text or ask for the webUrl."""
        meta = await graph.get(_ref(path, item_id), params={"$select": ITEM_SELECT})
        if "folder" in meta:
            raise ValueError("That item is a folder")
        size = meta.get("size", 0)
        if size > max_bytes:
            raise ValueError(f"File is {size} bytes; raise max_bytes or use drive_share_link")
        data: bytes = await graph.request("GET", _ref(None, meta["id"], "/content"))
        mime = (meta.get("file") or {}).get("mimeType", "")
        name = meta.get("name", "")
        out: dict[str, Any] = {
            "name": name,
            "mimeType": mime,
            "size": len(data),
            "webUrl": meta.get("webUrl"),
        }
        is_text = not force_base64 and (
            mime.startswith(TEXT_TYPES) or name.lower().endswith(TEXT_EXT)
        )
        if is_text:
            out["text"] = data.decode("utf-8", errors="replace")
        else:
            out["base64"] = base64.b64encode(data).decode()
        return out

    @mcp.tool(tags={"drive"})
    async def drive_file_as_pdf_text(
        path: str | None = None,
        item_id: str | None = None,
        max_chars: Annotated[int, Field(ge=500, le=500_000)] = 60_000,
    ) -> dict[str, Any]:
        """Convert an Office document (docx/pptx/xlsx) to PDF server-side and return its text.
        Good enough for reading; formatting is lost."""
        meta = await graph.get(_ref(path, item_id), params={"$select": "id,name"})
        pdf: bytes = await graph.request(
            "GET", _ref(None, meta["id"], "/content"), params={"format": "pdf"}
        )
        try:
            import io

            from pypdf import PdfReader  # optional dependency

            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
        except ImportError:
            return {
                "name": meta["name"],
                "error": "pypdf not installed on server; returning base64 PDF",
                "pdfBase64": base64.b64encode(pdf).decode()[:max_chars],
            }
        return {
            "name": meta["name"],
            "text": text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else ""),
        }

    @mcp.tool(tags={"drive", "write"})
    async def drive_upload_text(
        path: Annotated[
            str, Field(description="Destination path incl. file name, e.g. Notes/today.md")
        ],
        content: str,
        conflict: Literal["replace", "rename", "fail"] = "replace",
    ) -> dict[str, Any]:
        """Create or overwrite a text file (UTF-8). For binary content use drive_upload_base64."""
        return await _upload(path, content.encode("utf-8"), conflict)

    @mcp.tool(tags={"drive", "write"})
    async def drive_upload_base64(
        path: str, base64_content: str, conflict: Literal["replace", "rename", "fail"] = "replace"
    ) -> dict[str, Any]:
        """Create or overwrite a binary file from base64."""
        return await _upload(path, base64.b64decode(base64_content), conflict)

    async def _upload(path: str, data: bytes, conflict: str) -> dict[str, Any]:
        path = path.strip("/")
        if len(data) <= 4 * 1024 * 1024:
            r = await graph.request(
                "PUT",
                f"/me/drive/root:/{path}:/content",
                params={"@microsoft.graph.conflictBehavior": conflict},
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
            return _item(r)
        sess = await graph.post(
            f"/me/drive/root:/{path}:/createUploadSession",
            {"item": {"@microsoft.graph.conflictBehavior": conflict}},
        )
        url, total, r = sess["uploadUrl"], len(data), None
        import httpx

        async with httpx.AsyncClient(
            timeout=120
        ) as c:  # upload URL is pre-authenticated; no bearer
            for start in range(0, total, CHUNK):
                chunk = data[start : start + CHUNK]
                resp = await c.put(
                    url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}",
                    },
                )
                resp.raise_for_status()
                r = resp.json() if resp.content else None
        return _item(r or {})

    @mcp.tool(tags={"drive", "write"})
    async def drive_create_folder(parent_path: str, name: str) -> dict[str, Any]:
        """Create a folder (parent_path '' = root)."""
        r = await graph.post(
            _ref(parent_path, None, "/children"),
            {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )
        return _item(r)

    @mcp.tool(tags={"drive", "write"})
    async def drive_move(
        path: str | None = None,
        item_id: str | None = None,
        new_parent_path: str | None = None,
        new_name: str | None = None,
    ) -> dict[str, Any]:
        """Move and/or rename a file or folder."""
        patch: dict[str, Any] = {}
        if new_name:
            patch["name"] = new_name
        if new_parent_path is not None:
            parent = await graph.get(_ref(new_parent_path, None), params={"$select": "id"})
            patch["parentReference"] = {"id": parent["id"]}
        if not patch:
            return {"result": "nothing to do"}
        return _item(await graph.patch(_ref(path, item_id), patch))

    @mcp.tool(tags={"drive", "write"})
    async def drive_delete(path: str | None = None, item_id: str | None = None) -> str:
        """Move a file or folder to the OneDrive recycle bin (recoverable for 30 days)."""
        meta = await graph.get(_ref(path, item_id), params={"$select": "id,name"})
        await graph.delete(f"/me/drive/items/{meta['id']}")
        return f"Deleted '{meta['name']}' (in recycle bin)"

    @mcp.tool(tags={"drive", "write"})
    async def drive_share_link(
        path: str | None = None,
        item_id: str | None = None,
        link_type: Literal["view", "edit", "embed"] = "view",
        scope: Literal["anonymous", "users"] = "anonymous",
        expiration: Annotated[
            str | None, Field(description="ISO datetime; anonymous links only")
        ] = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Create a sharing link. 'anonymous' = anyone with the link; 'users' = specific people (then share via Outlook)."""
        body = compact(
            {
                "type": link_type,
                "scope": scope,
                "expirationDateTime": expiration,
                "password": password,
            }
        )
        r = await graph.post(_ref(path, item_id, "/createLink"), body)
        return {
            "url": (r.get("link") or {}).get("webUrl"),
            "type": link_type,
            "scope": scope,
            "expires": r.get("expirationDateTime"),
        }

    @mcp.tool(tags={"drive"})
    async def drive_recent(top: Annotated[int, Field(ge=1, le=100)] = 25) -> list[dict[str, Any]]:
        """Files recently used by the owner."""
        items = await graph.list("/me/drive/recent", params={"$top": min(top, 50)}, limit=top)
        return [_item(i) for i in items]

    @mcp.tool(tags={"drive"})
    async def drive_quota() -> dict[str, Any]:
        """Storage used/remaining."""
        d = await graph.get("/me/drive", params={"$select": "quota"})
        q = d.get("quota", {})
        gb = 1024**3
        return {
            "usedGB": round(q.get("used", 0) / gb, 2),
            "totalGB": round(q.get("total", 0) / gb, 2),
            "remainingGB": round(q.get("remaining", 0) / gb, 2),
            "state": q.get("state"),
        }
