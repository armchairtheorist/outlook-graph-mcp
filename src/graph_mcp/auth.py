"""Who may talk to this server.

Claude connects over OAuth. FastMCP's AzureProvider proxies that flow to Microsoft Entra
(tenant "consumers" = personal accounts), validates the resulting token, and hands us its claims.
We then require that the signed-in account is the mailbox owner (the account that seeded the
Graph refresh token) or one of EXTRA_ALLOWED_OIDS. Anyone else gets 401 even with a valid
Microsoft login, so the server can sit on a public URL.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.auth.providers.azure import AzureProvider
from mcp.server.auth.provider import AccessToken

from .config import Settings
from .graph import GraphClient

log = logging.getLogger(__name__)

# Scope name defined under "Expose an API" in the app registration.
API_SCOPE = "mcp.access"


class OwnerOnlyAzureProvider(AzureProvider):
    def __init__(self, settings: Settings, graph: GraphClient, **kw: Any) -> None:
        self._settings = settings
        self._graph = graph
        super().__init__(
            client_id=settings.entra_client_id,
            client_secret=settings.entra_client_secret,
            tenant_id=settings.entra_tenant,
            required_scopes=[API_SCOPE],
            base_url=settings.base_url,
            jwt_signing_key=settings.jwt_signing_key,
            # Claude.ai / Claude desktop / Claude Code redirect targets.
            allowed_client_redirect_uris=[
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
                "http://localhost:*",
                "http://127.0.0.1:*",
            ],
            require_authorization_consent="remember",
            **kw,
        )

    def _allowed(self, claims: dict[str, Any] | None) -> bool:
        if not claims:
            return False
        oid = claims.get("oid")
        if not oid:
            return False
        allowed = set(self._settings.allowed_oids)
        owner = self._graph.owner
        if owner is not None:
            allowed.add(owner.oid)
        return oid in allowed

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        access = await super().load_access_token(token)
        if access is None:
            return None
        if not self._allowed(access.claims):
            who = (access.claims or {}).get("preferred_username", "?")
            log.warning("Rejected authenticated but unauthorized account: %s", who)
            return None
        return access
