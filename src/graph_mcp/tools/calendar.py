"""Calendar tools. findMeetingTimes is not available for personal accounts, so free-slot
search is computed locally from calendarView."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..config import Settings
from ..graph import GraphClient
from ._common import addr, compact, html_to_text, recipients

EV_SELECT = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,isCancelled,isOnlineMeeting,"
    "onlineMeeting,webLink,bodyPreview,showAs,responseStatus,recurrence,seriesMasterId,categories"
)


def _ev(e: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": e.get("id"),
            "subject": e.get("subject"),
            "start": (e.get("start") or {}).get("dateTime"),
            "end": (e.get("end") or {}).get("dateTime"),
            "timeZone": (e.get("start") or {}).get("timeZone"),
            "allDay": e.get("isAllDay") or None,
            "location": (e.get("location") or {}).get("displayName"),
            "organizer": addr(e.get("organizer")),
            "attendees": [
                compact(
                    {
                        "who": addr(a),
                        "type": a.get("type"),
                        "response": (a.get("status") or {}).get("response"),
                    }
                )
                for a in e.get("attendees", [])
            ]
            or None,
            "myResponse": (e.get("responseStatus") or {}).get("response"),
            "showAs": e.get("showAs"),
            "cancelled": e.get("isCancelled") or None,
            "onlineJoinUrl": (e.get("onlineMeeting") or {}).get("joinUrl"),
            "recurring": bool(e.get("recurrence") or e.get("seriesMasterId")) or None,
            "seriesMasterId": e.get("seriesMasterId"),
            "categories": e.get("categories"),
            "preview": e.get("bodyPreview"),
            "webLink": e.get("webLink"),
        }
    )


def _parse(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def register(mcp: FastMCP, graph: GraphClient, settings: Settings) -> None:
    tz_header = {"Prefer": f'outlook.timezone="{settings.time_zone}"'}

    @mcp.tool(tags={"calendar"})
    async def calendar_list_calendars() -> list[dict[str, Any]]:
        """List the owner's calendars (primary, birthdays, shared, etc.)."""
        cals = await graph.list("/me/calendars", limit=50)
        return [
            compact(
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "default": c.get("isDefaultCalendar") or None,
                    "canEdit": c.get("canEdit"),
                    "owner": addr(c.get("owner")),
                }
            )
            for c in cals
        ]

    @mcp.tool(tags={"calendar"})
    async def calendar_view(
        start: Annotated[
            str, Field(description="ISO datetime (inclusive), e.g. 2026-10-05T00:00:00+08:00")
        ],
        end: Annotated[str, Field(description="ISO datetime (exclusive)")],
        calendar_id: str | None = None,
        top: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """Events in a time window, with recurring series expanded into occurrences. Times are
        returned in the server's configured time zone."""
        base = f"/me/calendars/{calendar_id}/calendarView" if calendar_id else "/me/calendarView"
        items = await graph.list(
            base,
            params={
                "startDateTime": start,
                "endDateTime": end,
                "$select": EV_SELECT,
                "$orderby": "start/dateTime",
                "$top": min(top, 100),
            },
            headers=tz_header,
            limit=top,
        )
        return [_ev(e) for e in items]

    @mcp.tool(tags={"calendar"})
    async def calendar_get_event(event_id: str) -> dict[str, Any]:
        """One event with its full description."""
        e = await graph.get(f"/me/events/{event_id}", headers=tz_header)
        out = _ev(e)
        body = e.get("body") or {}
        out["body"] = (
            html_to_text(body.get("content"))
            if body.get("contentType") == "html"
            else body.get("content", "")
        )
        return out

    @mcp.tool(tags={"calendar"})
    async def calendar_search(
        query: str, top: Annotated[int, Field(ge=1, le=100)] = 25
    ) -> list[dict[str, Any]]:
        """Search events by subject/body/attendee text across all time."""
        items = await graph.list(
            "/me/events",
            params={"$search": f'"{query}"', "$select": EV_SELECT, "$top": 25},
            headers=tz_header,
            limit=top,
        )
        return [_ev(e) for e in items]

    @mcp.tool(tags={"calendar", "write"})
    async def calendar_create_event(
        subject: str,
        start: Annotated[
            str, Field(description="ISO datetime without offset, interpreted in `time_zone`")
        ],
        end: str,
        time_zone: str | None = None,
        body: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        all_day: bool = False,
        show_as: Literal["free", "tentative", "busy", "oof"] = "busy",
        reminder_minutes: int | None = 15,
        recurrence: Annotated[
            dict[str, Any] | None,
            Field(description="Graph patternedRecurrence object, e.g. weekly on Mondays"),
        ] = None,
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an event. Attendees (if any) receive invitations."""
        tz = time_zone or settings.time_zone
        payload = compact(
            {
                "subject": subject,
                "body": {"contentType": "Text", "content": body} if body else None,
                "start": {"dateTime": start, "timeZone": tz},
                "end": {"dateTime": end, "timeZone": tz},
                "location": {"displayName": location} if location else None,
                "attendees": [dict(r, type="required") for r in recipients(attendees)] or None,
                "isAllDay": all_day or None,
                "showAs": show_as,
                "isReminderOn": reminder_minutes is not None,
                "reminderMinutesBeforeStart": reminder_minutes,
                "recurrence": recurrence,
            }
        )
        path = f"/me/calendars/{calendar_id}/events" if calendar_id else "/me/events"
        e = await graph.post(path, payload, headers=tz_header)
        return _ev(e)

    @mcp.tool(tags={"calendar", "write"})
    async def calendar_update_event(
        event_id: str,
        subject: str | None = None,
        start: str | None = None,
        end: str | None = None,
        time_zone: str | None = None,
        body: str | None = None,
        location: str | None = None,
        show_as: Literal["free", "tentative", "busy", "oof"] | None = None,
        add_attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change fields on an event. Omitted fields are left as they are."""
        tz = time_zone or settings.time_zone
        patch = compact(
            {
                "subject": subject,
                "start": {"dateTime": start, "timeZone": tz} if start else None,
                "end": {"dateTime": end, "timeZone": tz} if end else None,
                "body": {"contentType": "Text", "content": body} if body is not None else None,
                "location": {"displayName": location} if location is not None else None,
                "showAs": show_as,
            }
        )
        if add_attendees:
            cur = await graph.get(f"/me/events/{event_id}", params={"$select": "attendees"})
            patch["attendees"] = cur.get("attendees", []) + [
                dict(r, type="required") for r in recipients(add_attendees)
            ]
        e = await graph.patch(f"/me/events/{event_id}", patch, headers=tz_header)
        return _ev(e)

    @mcp.tool(tags={"calendar", "write"})
    async def calendar_delete_event(event_id: str, comment: str = "") -> str:
        """Delete an event. If you are the organizer and there are attendees, a cancellation is sent."""
        e = await graph.get(
            f"/me/events/{event_id}", params={"$select": "attendees,organizer,isOrganizer"}
        )
        if e.get("isOrganizer") and e.get("attendees"):
            await graph.post(f"/me/events/{event_id}/cancel", {"comment": comment})
            return f"Cancelled {event_id} and notified attendees"
        await graph.delete(f"/me/events/{event_id}")
        return f"Deleted {event_id}"

    @mcp.tool(tags={"calendar", "write"})
    async def calendar_respond(
        event_id: str,
        response: Literal["accept", "tentativelyAccept", "decline"],
        comment: str = "",
        send_response: bool = True,
    ) -> str:
        """Accept, tentatively accept or decline an invitation."""
        await graph.post(
            f"/me/events/{event_id}/{response}", {"comment": comment, "sendResponse": send_response}
        )
        return f"{response} on {event_id}"

    @mcp.tool(tags={"calendar"})
    async def calendar_find_free_slots(
        start: Annotated[str, Field(description="ISO datetime with offset, window start")],
        end: Annotated[str, Field(description="ISO datetime with offset, window end")],
        duration_minutes: Annotated[int, Field(ge=5, le=720)] = 30,
        day_start_hour: Annotated[
            int, Field(ge=0, le=23, description="Earliest local hour to propose")
        ] = 9,
        day_end_hour: Annotated[int, Field(ge=1, le=24)] = 18,
        max_results: Annotated[int, Field(ge=1, le=50)] = 10,
        ignore_tentative: bool = False,
    ) -> list[dict[str, str]]:
        """Free slots in the owner's primary calendar (computed locally; Graph's findMeetingTimes
        is not available for personal accounts). Hours are in the window's offset."""
        s, e = _parse(start), _parse(end)
        events = await graph.list(
            "/me/calendarView",
            params={
                "startDateTime": s.isoformat(),
                "endDateTime": e.isoformat(),
                "$select": "start,end,showAs,isCancelled,isAllDay",
                "$top": 100,
            },
            headers={"Prefer": 'outlook.timezone="UTC"'},
            limit=500,
        )
        busy: list[tuple[datetime, datetime]] = []
        for ev in events:
            if ev.get("isCancelled") or ev.get("showAs") == "free":
                continue
            if ignore_tentative and ev.get("showAs") == "tentative":
                continue
            busy.append((_parse(ev["start"]["dateTime"]), _parse(ev["end"]["dateTime"])))
        busy.sort()
        step, need = timedelta(minutes=15), timedelta(minutes=duration_minutes)
        tz = s.tzinfo
        slots: list[dict[str, str]] = []
        cur = s
        while cur + need <= e and len(slots) < max_results:
            local = cur.astimezone(tz)
            if not (
                day_start_hour <= local.hour
                and (local + need - timedelta(seconds=1)).hour < day_end_hour
                and local.date() == (local + need - timedelta(seconds=1)).date()
            ):
                cur += step
                continue
            clash = next((b for b in busy if b[0] < cur + need and b[1] > cur), None)
            if clash:
                cur = max(clash[1], cur + step)
                # realign to 15-minute grid
                cur = cur.replace(minute=(cur.minute // 15) * 15, second=0, microsecond=0)
                if cur < clash[1]:
                    cur += step
                continue
            slots.append({"start": local.isoformat(), "end": (local + need).isoformat()})
            cur += need
        return slots
