"""Thin async Microsoft Graph client: token refresh, paging, errors, retries on 429/503."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import msal

from .config import GRAPH_SCOPES, Settings
from .token_store import StoredToken, TokenStore

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Graph {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class GraphClient:
    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self._settings = settings
        self._store = store
        self._app_: msal.ConfidentialClientApplication | None = (
            None  # built lazily (does network I/O)
        )
        self._http = httpx.AsyncClient(base_url=GRAPH, timeout=30)
        self._access_token: str | None = None
        self._expires_at: float = 0
        self._lock = asyncio.Lock()
        self._owner: StoredToken | None = None

    # ------------------------------------------------------------------ auth
    def _app(self) -> msal.ConfidentialClientApplication:
        if self._app_ is None:
            self._app_ = msal.ConfidentialClientApplication(
                self._settings.entra_client_id,
                client_credential=self._settings.entra_client_secret,
                authority=self._settings.authority,
            )
        return self._app_

    @property
    def owner(self) -> StoredToken | None:
        """Identity of the account whose mailbox we're bound to (None until first token load)."""
        if self._owner is None:
            self._owner = self._store.load()
        return self._owner

    async def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            stored = self._store.load()
            if stored is None:
                raise RuntimeError(
                    "No Graph refresh token stored. Run `python scripts/seed_token.py` as the mailbox owner."
                )
            self._owner = stored
            # MSAL's refresh is sync; run it off the event loop.
            result = await asyncio.to_thread(
                self._app().acquire_token_by_refresh_token, stored.refresh_token, GRAPH_SCOPES
            )
            if "access_token" not in result:
                err = result.get("error")
                hint = (
                    "The Entra client secret is wrong or expired: rotate it (ops.ps1 rotate-secret) "
                    "and restart."
                    if err == "invalid_client"
                    else "The Graph refresh token was revoked: re-run the seed script (ops.ps1 reseed)."
                )
                raise RuntimeError(
                    f"Refresh failed: {err}: {result.get('error_description')} {hint}"
                )
            self._access_token = result["access_token"]
            self._expires_at = time.time() + int(result.get("expires_in", 3600))
            new_rt = result.get("refresh_token")
            if new_rt and new_rt != stored.refresh_token:
                stored.refresh_token = new_rt
                self._store.save(stored)
            return self._access_token

    # --------------------------------------------------------------- requests
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        absolute: bool = False,
    ) -> Any:
        """Perform one Graph call. Returns parsed JSON, bytes for binary, or None for 202/204."""
        url = path
        hdrs = {"Authorization": f"Bearer {await self._token()}", **(headers or {})}
        for attempt in range(4):
            resp = await self._http.request(
                method, url, params=params, json=json, headers=hdrs, content=content
            )
            if resp.status_code in (429, 503, 504) and attempt < 3:
                wait = float(resp.headers.get("Retry-After", 2**attempt))
                log.warning("Graph %s on %s; retrying in %.0fs", resp.status_code, path, wait)
                await asyncio.sleep(wait)
                continue
            break
        if resp.status_code == 401:
            # Token revoked or expired early: force refresh once.
            self._access_token = None
            hdrs["Authorization"] = f"Bearer {await self._token()}"
            resp = await self._http.request(
                method, url, params=params, json=json, headers=hdrs, content=content
            )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except Exception:
                err = {}
            raise GraphError(
                resp.status_code, err.get("code", "unknown"), err.get("message", resp.text[:500])
            )
        if resp.status_code in (202, 204) or not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            return resp.json()
        return resp.content

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json: Any | None = None, **kw: Any) -> Any:
        return await self.request("POST", path, json=json, **kw)

    async def patch(self, path: str, json: Any, **kw: Any) -> Any:
        return await self.request("PATCH", path, json=json, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def list(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = 50,
        headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Follow @odata.nextLink until `limit` items collected."""
        items: list[dict[str, Any]] = []
        page = await self.get(path, params=params, headers=headers)
        while True:
            items.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
            if not nxt or len(items) >= limit:
                break
            page = await self.request("GET", nxt, headers=headers, absolute=True)
        return items[:limit]

    async def aclose(self) -> None:
        await self._http.aclose()
