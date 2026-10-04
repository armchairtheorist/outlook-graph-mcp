"""Microsoft To Do tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..config import Settings
from ..graph import GraphClient
from ._common import compact, html_to_text

TODO = "/me/todo/lists"


def _task(t: dict[str, Any]) -> dict[str, Any]:
    body = t.get("body") or {}
    note = body.get("content", "")
    if body.get("contentType") == "html":
        note = html_to_text(note)
    return compact(
        {
            "id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "importance": t.get("importance") if t.get("importance") != "normal" else None,
            "due": (t.get("dueDateTime") or {}).get("dateTime", "")[:10] or None,
            "reminder": (t.get("reminderDateTime") or {}).get("dateTime"),
            "completed": (t.get("completedDateTime") or {}).get("dateTime"),
            "recurring": bool(t.get("recurrence")) or None,
            "categories": t.get("categories"),
            "note": note.strip() or None,
            "checklist": [
                compact(
                    {
                        "id": c["id"],
                        "text": c.get("displayName"),
                        "done": c.get("isChecked") or None,
                    }
                )
                for c in t.get("checklistItems", [])
            ]
            or None,
            "created": t.get("createdDateTime"),
        }
    )


def register(mcp: FastMCP, graph: GraphClient, settings: Settings) -> None:
    def _dt(date: str | None) -> dict[str, str] | None:
        if not date:
            return None
        return {
            "dateTime": date if "T" in date else f"{date}T09:00:00",
            "timeZone": settings.time_zone,
        }

    @mcp.tool(tags={"todo"})
    async def todo_list_lists() -> list[dict[str, Any]]:
        """All task lists (the default list is named 'Tasks')."""
        lists = await graph.list(TODO, limit=100)
        return [
            compact(
                {
                    "id": lst["id"],
                    "name": lst.get("displayName"),
                    "wellknown": lst.get("wellknownListName")
                    if lst.get("wellknownListName") != "none"
                    else None,
                    "shared": lst.get("isShared") or None,
                }
            )
            for lst in lists
        ]

    @mcp.tool(tags={"todo"})
    async def todo_list_tasks(
        list_id: str,
        status: Literal["open", "completed", "all"] = "open",
        top: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """Tasks in a list. Open tasks are sorted by due date (undated last)."""
        params: dict[str, Any] = {"$top": min(top, 100), "$expand": "checklistItems"}
        if status == "open":
            params["$filter"] = "status ne 'completed'"
        elif status == "completed":
            params["$filter"] = "status eq 'completed'"
        tasks = [
            _task(t) for t in await graph.list(f"{TODO}/{list_id}/tasks", params=params, limit=top)
        ]
        return sorted(
            tasks, key=lambda t: (t.get("due") is None, t.get("due") or "", t.get("created") or "")
        )

    @mcp.tool(tags={"todo"})
    async def todo_search(
        query: str, top: Annotated[int, Field(ge=1, le=200)] = 50
    ) -> list[dict[str, Any]]:
        """Find open tasks across all lists whose title or note contains the text (case-insensitive)."""
        q = query.lower()
        out: list[dict[str, Any]] = []
        for lst in await graph.list(TODO, limit=100):
            tasks = await graph.list(
                f"{TODO}/{lst['id']}/tasks",
                params={"$filter": "status ne 'completed'", "$top": 100},
                limit=500,
            )
            for t in tasks:
                tt = _task(t)
                if q in (tt.get("title") or "").lower() or q in (tt.get("note") or "").lower():
                    tt["list"] = lst.get("displayName")
                    tt["listId"] = lst["id"]
                    out.append(tt)
                    if len(out) >= top:
                        return out
        return out

    @mcp.tool(tags={"todo", "write"})
    async def todo_create_task(
        list_id: str,
        title: str,
        note: str = "",
        due: Annotated[
            str | None, Field(description="YYYY-MM-DD or ISO datetime (owner's time zone)")
        ] = None,
        reminder: Annotated[
            str | None, Field(description="ISO datetime for a reminder notification")
        ] = None,
        importance: Literal["low", "normal", "high"] = "normal",
        checklist: list[str] | None = None,
        recurrence: Annotated[
            dict[str, Any] | None, Field(description="Graph patternedRecurrence")
        ] = None,
    ) -> dict[str, Any]:
        """Create a task, optionally with due date, reminder and checklist items."""
        body = compact(
            {
                "title": title,
                "body": {"content": note, "contentType": "text"} if note else None,
                "dueDateTime": _dt(due),
                "reminderDateTime": _dt(reminder),
                "isReminderOn": bool(reminder) or None,
                "importance": importance,
                "recurrence": recurrence,
            }
        )
        t = await graph.post(f"{TODO}/{list_id}/tasks", body)
        for item in checklist or []:
            await graph.post(
                f"{TODO}/{list_id}/tasks/{t['id']}/checklistItems", {"displayName": item}
            )
        full = await graph.get(
            f"{TODO}/{list_id}/tasks/{t['id']}", params={"$expand": "checklistItems"}
        )
        return _task(full)

    @mcp.tool(tags={"todo", "write"})
    async def todo_update_task(
        list_id: str,
        task_id: str,
        title: str | None = None,
        note: str | None = None,
        due: Annotated[str | None, Field(description="YYYY-MM-DD, or '' to clear")] = None,
        reminder: str | None = None,
        importance: Literal["low", "normal", "high"] | None = None,
        status: Literal["notStarted", "inProgress", "completed"] | None = None,
    ) -> dict[str, Any]:
        """Edit a task; omitted fields are unchanged. Use status='completed' to tick it off."""
        patch: dict[str, Any] = compact(
            {
                "title": title,
                "body": {"content": note, "contentType": "text"} if note is not None else None,
                "reminderDateTime": _dt(reminder),
                "isReminderOn": True if reminder else None,
                "importance": importance,
                "status": status,
            }
        )
        if due == "":
            patch["dueDateTime"] = None
        elif due:
            patch["dueDateTime"] = _dt(due)
        t = await graph.patch(f"{TODO}/{list_id}/tasks/{task_id}", patch)
        return _task(t)

    @mcp.tool(tags={"todo", "write"})
    async def todo_complete_task(list_id: str, task_id: str) -> str:
        """Mark a task completed."""
        await graph.patch(f"{TODO}/{list_id}/tasks/{task_id}", {"status": "completed"})
        return f"Completed {task_id}"

    @mcp.tool(tags={"todo", "write"})
    async def todo_delete_task(list_id: str, task_id: str) -> str:
        """Delete a task permanently."""
        await graph.delete(f"{TODO}/{list_id}/tasks/{task_id}")
        return f"Deleted {task_id}"

    @mcp.tool(tags={"todo", "write"})
    async def todo_checklist_update(
        list_id: str, task_id: str, item_id: str, done: bool | None = None, text: str | None = None
    ) -> str:
        """Tick/untick or rename a checklist item."""
        await graph.patch(
            f"{TODO}/{list_id}/tasks/{task_id}/checklistItems/{item_id}",
            compact({"isChecked": done, "displayName": text}),
        )
        return "ok"

    @mcp.tool(tags={"todo", "write"})
    async def todo_create_list(name: str) -> dict[str, Any]:
        """Create a new task list."""
        lst = await graph.post(TODO, {"displayName": name})
        return {"id": lst["id"], "name": lst.get("displayName")}
