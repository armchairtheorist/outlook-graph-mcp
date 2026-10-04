"""Graph change notifications.

Graph POSTs to /webhooks when subscribed resources change. We validate the shared clientState,
record the notification in a small in-memory ring buffer that the `events_recent` tool exposes,
and keep subscriptions alive (Outlook subscriptions expire after < 7 days).

This is deliberately simple: the agent polls `events_recent` on its schedule rather than the
server pushing into Claude, which MCP does not support from a remote server today.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .config import Settings
from .graph import GraphClient, GraphError

log = logging.getLogger(__name__)

RENEW_EVERY = timedelta(hours=24)
LIFETIME = timedelta(days=6, hours=12)  # under Graph's 10,080-minute cap for Outlook resources


class WebhookManager:
    def __init__(self, settings: Settings, graph: GraphClient) -> None:
        self.settings = settings
        self.graph = graph
        self.recent: deque[dict[str, Any]] = deque(maxlen=500)
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ HTTP side
    async def handle(self, request: Request) -> Response:
        token = request.query_params.get("validationToken")
        if token:  # subscription validation handshake
            return PlainTextResponse(token, status_code=200)
        try:
            body = await request.json()
        except Exception:
            return Response(status_code=400)
        for n in body.get("value", []):
            if (
                self.settings.webhook_client_state
                and n.get("clientState") != self.settings.webhook_client_state
            ):
                log.warning("Dropping notification with bad clientState")
                continue
            self.recent.append(
                {
                    "receivedAt": datetime.now(UTC).isoformat(),
                    "changeType": n.get("changeType"),
                    "resource": n.get("resource"),
                    "resourceId": (n.get("resourceData") or {}).get("id"),
                    "odataType": (n.get("resourceData") or {}).get("@odata.type"),
                    "subscriptionId": n.get("subscriptionId"),
                }
            )
        # Graph wants a fast 202; lifecycle notifications also land here and are harmless.
        return JSONResponse({}, status_code=202)

    # ------------------------------------------------------- subscriptions
    async def ensure_subscriptions(self) -> list[dict[str, Any]]:
        wanted = self.settings.webhook_resource_list
        if not wanted or not self.settings.base_url.startswith("https://"):
            return []
        existing = await self.graph.list("/subscriptions", limit=100)
        by_resource = {s["resource"]: s for s in existing}
        url = f"{self.settings.base_url.rstrip('/')}/webhooks"
        expiry = (datetime.now(UTC) + LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[dict[str, Any]] = []
        for res in wanted:
            cur = by_resource.get(res.lstrip("/"))
            try:
                if cur and cur.get("notificationUrl") == url:
                    out.append(
                        await self.graph.patch(
                            f"/subscriptions/{cur['id']}", {"expirationDateTime": expiry}
                        )
                    )
                    continue
                if cur:
                    await self.graph.delete(f"/subscriptions/{cur['id']}")
                out.append(
                    await self.graph.post(
                        "/subscriptions",
                        {
                            "changeType": "created,updated",
                            "notificationUrl": url,
                            "resource": res.lstrip("/"),
                            "expirationDateTime": expiry,
                            "clientState": self.settings.webhook_client_state or "",
                        },
                    )
                )
                log.info("Subscribed to %s", res)
            except GraphError as e:
                log.error("Subscription for %s failed: %s", res, e)
        return out

    async def _loop(self) -> None:
        while True:
            try:
                await self.ensure_subscriptions()
            except Exception:  # keep the loop alive no matter what
                log.exception("Subscription renewal failed")
            await asyncio.sleep(RENEW_EVERY.total_seconds())

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
