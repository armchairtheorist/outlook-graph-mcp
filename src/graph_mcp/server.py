"""Entry point. `python -m graph_mcp.server` or `graph-mcp`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import OwnerOnlyAzureProvider
from .config import Settings, get_settings
from .graph import GraphClient
from .token_store import build_token_store
from .tools import calendar as calendar_tools
from .tools import contacts as contacts_tools
from .tools import drive as drive_tools
from .tools import mail as mail_tools
from .tools import onenote as onenote_tools
from .tools import todo as todo_tools
from .webhooks import WebhookManager

log = logging.getLogger("graph_mcp")

INSTRUCTIONS = """\
You are connected to the owner's personal Microsoft account: Outlook.com mail, calendar and
contacts, OneDrive files, OneNote notebooks and Microsoft To Do.
Everything here acts as the owner. Before sending mail, replying, deleting, or cancelling
meetings, make sure that is what the owner asked for; prefer mail_create_draft when unsure.
Message and event ids are opaque strings from earlier results; never invent them.
Times without an offset are in the owner's time zone unless a tool says otherwise.
"""


def build_app(settings: Settings | None = None) -> tuple[FastMCP, GraphClient]:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    store = build_token_store(
        settings.token_store,
        file_path=settings.token_file,
        keyvault_url=settings.keyvault_url,
        secret_name=settings.keyvault_secret_name,
    )
    graph = GraphClient(settings, store)
    hooks = WebhookManager(settings, graph)

    @asynccontextmanager
    async def lifespan(_app: FastMCP):
        owner = graph.owner
        if owner:
            log.info("Bound to mailbox %s", owner.username)
        else:
            log.warning(
                "No Graph token stored yet; tools will fail until scripts/seed_token.py is run"
            )
        hooks.start()
        try:
            yield {}
        finally:
            await hooks.stop()
            await graph.aclose()

    auth = OwnerOnlyAzureProvider(settings, graph)
    mcp = FastMCP(
        "Outlook (personal)",
        instructions=INSTRUCTIONS,
        auth=auth,
        lifespan=lifespan,
        mask_error_details=False,
    )

    mail_tools.register(mcp, graph)
    calendar_tools.register(mcp, graph, settings)
    drive_tools.register(mcp, graph)
    onenote_tools.register(mcp, graph)
    todo_tools.register(mcp, graph, settings)
    contacts_tools.register(mcp, graph)

    @mcp.tool(tags={"meta"})
    async def whoami() -> dict[str, Any]:
        """The account this server is bound to, plus mailbox time zone."""
        me = await graph.get("/me", params={"$select": "displayName,mail,userPrincipalName,id"})
        tz = await graph.get("/me/mailboxSettings", params={"$select": "timeZone"})
        return {
            "name": me.get("displayName"),
            "email": me.get("mail") or me.get("userPrincipalName"),
            "timeZone": tz.get("timeZone"),
            "serverTimeZone": settings.time_zone,
        }

    @mcp.tool(tags={"meta"})
    async def events_recent(limit: int = 50) -> list[dict[str, Any]]:
        """Change notifications received from Graph since the server started (newest first).
        Useful for 'what arrived since I last checked' without re-listing the inbox."""
        return list(hooks.recent)[-limit:][::-1]

    @mcp.tool(tags={"meta"})
    async def subscriptions_status() -> list[dict[str, Any]]:
        """Active Graph webhook subscriptions and when they expire."""
        subs = await graph.list("/subscriptions", limit=100)
        return [
            {
                "id": s["id"],
                "resource": s["resource"],
                "expires": s["expirationDateTime"],
                "url": s["notificationUrl"],
            }
            for s in subs
        ]

    @mcp.custom_route("/webhooks", methods=["POST"], include_in_schema=False)
    async def webhooks(request: Request):
        return await hooks.handle(request)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request):
        return JSONResponse({"ok": True, "owner": (graph.owner.username if graph.owner else None)})

    return mcp, graph


def main() -> None:
    settings = get_settings()
    mcp, _ = build_app(settings)
    mcp.run(
        transport="http", host=settings.host, port=settings.port, path="/mcp", show_banner=False
    )


if __name__ == "__main__":
    main()
