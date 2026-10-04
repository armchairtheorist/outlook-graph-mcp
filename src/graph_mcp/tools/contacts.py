"""Outlook contacts tools."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from ..graph import GraphClient
from ._common import compact

C_SELECT = (
    "id,displayName,givenName,surname,emailAddresses,mobilePhone,businessPhones,homePhones,"
    "companyName,jobTitle,birthday,personalNotes,categories,homeAddress,businessAddress,lastModifiedDateTime"
)


def _addr(a: dict[str, Any] | None) -> str | None:
    if not a:
        return None
    parts = [
        a.get("street"),
        a.get("city"),
        a.get("state"),
        a.get("postalCode"),
        a.get("countryOrRegion"),
    ]
    return ", ".join(p for p in parts if p) or None


def _contact(c: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": c.get("id"),
            "name": c.get("displayName"),
            "emails": [e.get("address") for e in c.get("emailAddresses", []) if e.get("address")]
            or None,
            "mobile": c.get("mobilePhone"),
            "phones": (c.get("businessPhones") or []) + (c.get("homePhones") or []) or None,
            "company": c.get("companyName"),
            "jobTitle": c.get("jobTitle"),
            "birthday": (c.get("birthday") or "")[:10] or None,
            "homeAddress": _addr(c.get("homeAddress")),
            "businessAddress": _addr(c.get("businessAddress")),
            "notes": c.get("personalNotes"),
            "categories": c.get("categories"),
        }
    )


def register(mcp: FastMCP, graph: GraphClient) -> None:
    @mcp.tool(tags={"contacts"})
    async def contacts_list(
        top: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> list[dict[str, Any]]:
        """Contacts sorted by name."""
        items = await graph.list(
            "/me/contacts",
            params={"$select": C_SELECT, "$orderby": "displayName", "$top": min(top, 100)},
            limit=top,
        )
        return [_contact(c) for c in items]

    @mcp.tool(tags={"contacts"})
    async def contacts_search(
        query: Annotated[
            str,
            Field(
                description="Name, email or company. Matches whole words by prefix "
                "('Jas' finds Jasmine; a single letter finds nothing). Use contacts_list to browse."
            ),
        ],
        top: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> list[dict[str, Any]]:
        """Search contacts."""
        items = await graph.list(
            "/me/contacts",
            params={"$search": f'"{query}"', "$select": C_SELECT, "$top": min(top, 50)},
            limit=top,
        )
        return [_contact(c) for c in items]

    @mcp.tool(tags={"contacts"})
    async def contacts_get(contact_id: str) -> dict[str, Any]:
        """One contact in full."""
        return _contact(await graph.get(f"/me/contacts/{contact_id}", params={"$select": C_SELECT}))

    @mcp.tool(tags={"contacts", "write"})
    async def contacts_create(
        given_name: str,
        surname: str = "",
        emails: list[str] | None = None,
        mobile: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
        notes: str | None = None,
        birthday: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """Create a contact."""
        body = compact(
            {
                "givenName": given_name,
                "surname": surname or None,
                "emailAddresses": [
                    {"address": e, "name": f"{given_name} {surname}".strip()} for e in emails or []
                ]
                or None,
                "mobilePhone": mobile,
                "companyName": company,
                "jobTitle": job_title,
                "personalNotes": notes,
                "birthday": f"{birthday}T00:00:00Z" if birthday else None,
            }
        )
        return _contact(await graph.post("/me/contacts", body))

    @mcp.tool(tags={"contacts", "write"})
    async def contacts_update(
        contact_id: str,
        given_name: str | None = None,
        surname: str | None = None,
        emails: Annotated[
            list[str] | None, Field(description="Replaces the whole email list")
        ] = None,
        mobile: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
        notes: str | None = None,
        categories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Edit a contact; omitted fields are unchanged."""
        patch = compact(
            {
                "givenName": given_name,
                "surname": surname,
                "emailAddresses": [{"address": e} for e in emails] if emails is not None else None,
                "mobilePhone": mobile,
                "companyName": company,
                "jobTitle": job_title,
                "personalNotes": notes,
                "categories": categories,
            }
        )
        return _contact(await graph.patch(f"/me/contacts/{contact_id}", patch))

    @mcp.tool(tags={"contacts", "write"})
    async def contacts_delete(contact_id: str) -> str:
        """Delete a contact."""
        await graph.delete(f"/me/contacts/{contact_id}")
        return f"Deleted {contact_id}"
