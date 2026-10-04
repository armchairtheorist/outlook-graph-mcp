"""Mail tools (Outlook.com via Microsoft Graph)."""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..graph import GraphClient
from ._common import addr, addrs, compact, html_to_text, recipients

MSG_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,isRead,"
    "hasAttachments,importance,flag,bodyPreview,conversationId,parentFolderId,webLink,categories"
)


def _summarize(m: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": m.get("id"),
            "subject": m.get("subject"),
            "from": addr(m.get("from")),
            "to": addrs(m.get("toRecipients")),
            "cc": addrs(m.get("ccRecipients")),
            "received": m.get("receivedDateTime"),
            "isRead": m.get("isRead"),
            "hasAttachments": m.get("hasAttachments") or None,
            "importance": m.get("importance") if m.get("importance") != "normal" else None,
            "flagged": (m.get("flag") or {}).get("flagStatus") == "flagged" or None,
            "categories": m.get("categories"),
            "preview": m.get("bodyPreview"),
            "conversationId": m.get("conversationId"),
            "webLink": m.get("webLink"),
        }
    )


def register(mcp: FastMCP, graph: GraphClient) -> None:
    @mcp.tool(tags={"mail"})
    async def mail_list(
        folder: Annotated[
            str,
            Field(
                description="Well-known name (inbox, sentitems, drafts, deleteditems, junkemail, archive) or folder id"
            ),
        ] = "inbox",
        top: Annotated[int, Field(ge=1, le=100)] = 25,
        unread_only: bool = False,
        since: Annotated[
            str | None, Field(description="ISO-8601 datetime; only messages received after this")
        ] = None,
    ) -> list[dict[str, Any]]:
        """List recent messages in a folder, newest first. Returns summaries without full bodies."""
        filters = []
        if unread_only:
            filters.append("isRead eq false")
        if since:
            filters.append(f"receivedDateTime ge {since}")
        params: dict[str, Any] = {
            "$select": MSG_SELECT,
            "$top": min(top, 50),
            "$orderby": "receivedDateTime desc",
        }
        if filters:
            params["$filter"] = " and ".join(filters)
        items = await graph.list(f"/me/mailFolders/{folder}/messages", params=params, limit=top)
        return [_summarize(m) for m in items]

    @mcp.tool(tags={"mail"})
    async def mail_search(
        query: Annotated[
            str,
            Field(
                description='KQL, e.g. "from:alice subject:invoice received>=2026-09-01" or free text'
            ),
        ],
        top: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> list[dict[str, Any]]:
        """Search all mail folders. Results are ranked by relevance, not date."""
        items = await graph.list(
            "/me/messages",
            params={"$search": f'"{query}"', "$select": MSG_SELECT, "$top": min(top, 25)},
            limit=top,
        )
        return [_summarize(m) for m in items]

    @mcp.tool(tags={"mail"})
    async def mail_get(
        message_id: str,
        body_format: Literal["text", "html"] = "text",
        max_chars: Annotated[int, Field(ge=200, le=200_000)] = 20_000,
    ) -> dict[str, Any]:
        """Read one message in full, including the body and attachment list."""
        m = await graph.get(
            f"/me/messages/{message_id}",
            params={"$expand": "attachments($select=id,name,contentType,size,isInline)"},
            headers={"Prefer": f'outlook.body-content-type="{body_format}"'},
        )
        body = (m.get("body") or {}).get("content", "")
        if body_format == "text" and (m.get("body") or {}).get("contentType") == "html":
            body = html_to_text(body)
        out = _summarize(m)
        out["body"] = body[:max_chars] + ("\n…[truncated]" if len(body) > max_chars else "")
        out["attachments"] = [
            compact(
                {
                    "id": a["id"],
                    "name": a.get("name"),
                    "contentType": a.get("contentType"),
                    "size": a.get("size"),
                    "inline": a.get("isInline") or None,
                }
            )
            for a in m.get("attachments", [])
            if not a.get("isInline")
        ]
        return out

    @mcp.tool(tags={"mail"})
    async def mail_get_thread(
        conversation_id: str, top: Annotated[int, Field(ge=1, le=50)] = 20
    ) -> list[dict[str, Any]]:
        """All messages in a conversation, oldest first, with text bodies."""
        # Graph rejects $orderby on a property absent from $filter (InefficientFilter), so sort here.
        items = await graph.list(
            "/me/messages",
            params={
                "$filter": f"conversationId eq '{conversation_id}'",
                "$select": MSG_SELECT + ",body",
                "$top": top,
            },
            headers={"Prefer": 'outlook.body-content-type="text"'},
            limit=top,
        )
        items.sort(key=lambda x: x.get("receivedDateTime") or "")
        out = []
        for m in items:
            s = _summarize(m)
            s["body"] = (m.get("body") or {}).get("content", "")[:8000]
            out.append(s)
        return out

    @mcp.tool(tags={"mail"})
    async def mail_get_attachment(message_id: str, attachment_id: str) -> dict[str, Any]:
        """Download an attachment. Text-like content is returned as text; everything else base64."""
        a = await graph.get(f"/me/messages/{message_id}/attachments/{attachment_id}")
        raw = base64.b64decode(a.get("contentBytes", ""))
        ctype = a.get("contentType", "")
        out: dict[str, Any] = {"name": a.get("name"), "contentType": ctype, "size": len(raw)}
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            out["text"] = raw.decode("utf-8", errors="replace")[:200_000]
        else:
            out["base64"] = a.get("contentBytes")
        return out

    @mcp.tool(tags={"mail", "write"})
    async def mail_send(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_is_html: bool = False,
        importance: Literal["low", "normal", "high"] = "normal",
        save_to_sent: bool = True,
    ) -> str:
        """Send a new email from the owner's account. Addresses may be 'Name <a@b.com>' or bare."""
        msg = compact(
            {
                "subject": subject,
                "body": {"contentType": "HTML" if body_is_html else "Text", "content": body},
                "toRecipients": recipients(to),
                "ccRecipients": recipients(cc),
                "bccRecipients": recipients(bcc),
                "importance": importance,
            }
        )
        await graph.post("/me/sendMail", {"message": msg, "saveToSentItems": save_to_sent})
        return f"Sent '{subject}' to {', '.join(to)}"

    @mcp.tool(tags={"mail", "write"})
    async def mail_reply(
        message_id: str,
        body: str,
        reply_all: bool = False,
        body_is_html: bool = False,
    ) -> str:
        """Reply (or reply-all) to a message. Quotes the original automatically."""
        action = "replyAll" if reply_all else "reply"
        payload = (
            {
                "message": {
                    "body": {"contentType": "HTML" if body_is_html else "Text", "content": body}
                }
            }
            if body_is_html
            else {"comment": body}
        )
        await graph.post(f"/me/messages/{message_id}/{action}", payload)
        return f"{'Replied to all' if reply_all else 'Replied'} on message {message_id}"

    @mcp.tool(tags={"mail", "write"})
    async def mail_forward(message_id: str, to: list[str], comment: str = "") -> str:
        """Forward a message, optionally with a note on top."""
        await graph.post(
            f"/me/messages/{message_id}/forward",
            {"toRecipients": recipients(to), "comment": comment},
        )
        return f"Forwarded {message_id} to {', '.join(to)}"

    @mcp.tool(tags={"mail", "write"})
    async def mail_create_draft(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        body_is_html: bool = False,
    ) -> dict[str, Any]:
        """Create a draft without sending. Returns its id and webLink so the owner can review."""
        d = await graph.post(
            "/me/messages",
            compact(
                {
                    "subject": subject,
                    "body": {"contentType": "HTML" if body_is_html else "Text", "content": body},
                    "toRecipients": recipients(to),
                    "ccRecipients": recipients(cc),
                }
            ),
        )
        return {"id": d["id"], "webLink": d.get("webLink")}

    @mcp.tool(tags={"mail", "write"})
    async def mail_update(
        message_ids: list[str],
        is_read: bool | None = None,
        flag: Literal["flagged", "complete", "notFlagged"] | None = None,
        categories: list[str] | None = None,
        importance: Literal["low", "normal", "high"] | None = None,
    ) -> str:
        """Mark read/unread, flag, categorize or change importance on one or more messages."""
        patch = compact(
            {
                "isRead": is_read,
                "flag": {"flagStatus": flag} if flag else None,
                "categories": categories,
                "importance": importance,
            }
        )
        if not patch:
            return "Nothing to update"
        for mid in message_ids:
            await graph.patch(f"/me/messages/{mid}", patch)
        return f"Updated {len(message_ids)} message(s): {patch}"

    @mcp.tool(tags={"mail", "write"})
    async def mail_move(
        message_ids: list[str],
        destination_folder: Annotated[
            str,
            Field(
                description="Well-known name (archive, deleteditems, junkemail, inbox) or folder id"
            ),
        ],
    ) -> str:
        """Move messages to another folder. Use destination 'deleteditems' to delete (recoverable)."""
        for mid in message_ids:
            await graph.post(f"/me/messages/{mid}/move", {"destinationId": destination_folder})
        return f"Moved {len(message_ids)} message(s) to {destination_folder}"

    @mcp.tool(tags={"mail"})
    async def mail_list_folders(include_counts: bool = True) -> list[dict[str, Any]]:
        """List top-level mail folders (and one level of children) with unread/total counts."""
        folders = await graph.list(
            "/me/mailFolders", params={"$top": 100, "$expand": "childFolders($top=50)"}, limit=200
        )

        def f(x: dict[str, Any]) -> dict[str, Any]:
            return compact(
                {
                    "id": x["id"],
                    "name": x.get("displayName"),
                    "unread": x.get("unreadItemCount") if include_counts else None,
                    "total": x.get("totalItemCount") if include_counts else None,
                    "children": [f(c) for c in x.get("childFolders", [])] or None,
                }
            )

        return [f(x) for x in folders]

    @mcp.tool(tags={"mail", "write"})
    async def mail_create_folder(name: str, parent_folder_id: str | None = None) -> dict[str, Any]:
        """Create a mail folder, optionally under a parent."""
        path = (
            f"/me/mailFolders/{parent_folder_id}/childFolders"
            if parent_folder_id
            else "/me/mailFolders"
        )
        r = await graph.post(path, {"displayName": name})
        return {"id": r["id"], "name": r.get("displayName")}

    # ---------------------------------------------------------------- rules
    @mcp.tool(tags={"mail", "rules"})
    async def mail_list_rules() -> list[dict[str, Any]]:
        """List server-side inbox rules (these run even when Claude is offline)."""
        rules = await graph.list("/me/mailFolders/inbox/messageRules", limit=100)
        return [
            compact(
                {
                    "id": r["id"],
                    "name": r.get("displayName"),
                    "enabled": r.get("isEnabled"),
                    "sequence": r.get("sequence"),
                    "conditions": r.get("conditions"),
                    "actions": r.get("actions"),
                    "exceptions": r.get("exceptions"),
                }
            )
            for r in rules
        ]

    @mcp.tool(tags={"mail", "rules", "write"})
    async def mail_create_rule(
        name: str,
        conditions: Annotated[
            dict[str, Any],
            Field(
                description='Graph messageRulePredicates, e.g. {"senderContains": ["newsletter"]} or {"subjectContains": ["invoice"]}'
            ),
        ],
        actions: Annotated[
            dict[str, Any],
            Field(
                description='Graph messageRuleActions, e.g. {"moveToFolder": "<folderId>", "markAsRead": true} or {"forwardTo": [{"emailAddress": {"address": "x@y.com"}}]}'
            ),
        ],
        exceptions: dict[str, Any] | None = None,
        sequence: int = 1,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create an inbox rule that Outlook applies server-side to incoming mail."""
        r = await graph.post(
            "/me/mailFolders/inbox/messageRules",
            compact(
                {
                    "displayName": name,
                    "sequence": sequence,
                    "isEnabled": enabled,
                    "conditions": conditions,
                    "actions": actions,
                    "exceptions": exceptions,
                }
            ),
        )
        return {"id": r["id"], "name": r.get("displayName")}

    @mcp.tool(tags={"mail", "rules", "write"})
    async def mail_delete_rule(rule_id: str) -> str:
        """Delete an inbox rule."""
        await graph.delete(f"/me/mailFolders/inbox/messageRules/{rule_id}")
        return f"Deleted rule {rule_id}"

    # ------------------------------------------------------------- settings
    @mcp.tool(tags={"mail", "settings"})
    async def mailbox_settings_get() -> dict[str, Any]:
        """Automatic replies, time zone, working hours, language."""
        return await graph.get("/me/mailboxSettings")

    @mcp.tool(tags={"mail", "settings", "write"})
    async def mailbox_auto_reply_set(
        status: Literal["disabled", "alwaysEnabled", "scheduled"],
        internal_message: str = "",
        external_message: str | None = None,
        start: Annotated[
            str | None, Field(description="ISO datetime, required when scheduled")
        ] = None,
        end: str | None = None,
        time_zone: str = "UTC",
    ) -> str:
        """Set or clear out-of-office automatic replies."""
        ars: dict[str, Any] = {
            "status": status,
            "internalReplyMessage": internal_message,
            "externalReplyMessage": external_message
            if external_message is not None
            else internal_message,
            "externalAudience": "all",
        }
        if status == "scheduled":
            ars["scheduledStartDateTime"] = {"dateTime": start, "timeZone": time_zone}
            ars["scheduledEndDateTime"] = {"dateTime": end, "timeZone": time_zone}
        await graph.patch("/me/mailboxSettings", {"automaticRepliesSetting": ars})
        return f"Automatic replies: {status}"
