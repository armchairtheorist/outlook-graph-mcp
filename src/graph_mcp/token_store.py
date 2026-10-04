"""Persistence for the one Graph refresh token this server uses.

Personal Microsoft accounts have no unattended (client-credentials) flow, so the mailbox owner
signs in once with scripts/seed_token.py and we keep the resulting refresh token. MSAL rotates
refresh tokens, so every refresh writes the new one back.

Two backends:
  - FileTokenStore: local development only.
  - KeyVaultTokenStore: production. The container reads/writes via its managed identity.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class StoredToken:
    refresh_token: str
    # Entra object ID of the account that owns the mailbox. Used to restrict who may use the server.
    oid: str
    username: str
    home_account_id: str | None = None

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, raw: str) -> StoredToken:
        return cls(**json.loads(raw))


class TokenStore(Protocol):
    def load(self) -> StoredToken | None: ...
    def save(self, token: StoredToken) -> None: ...


class FileTokenStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> StoredToken | None:
        if not self.path.exists():
            return None
        return StoredToken.loads(self.path.read_text())

    def save(self, token: StoredToken) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(token.dumps())
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


class KeyVaultTokenStore:
    def __init__(self, vault_url: str, secret_name: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        self._client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
        self._name = secret_name

    def load(self) -> StoredToken | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            secret = self._client.get_secret(self._name)
        except ResourceNotFoundError:
            return None
        return StoredToken.loads(secret.value or "")

    def save(self, token: StoredToken) -> None:
        self._client.set_secret(self._name, token.dumps(), content_type="application/json")
        log.debug("Saved rotated refresh token to Key Vault")


def build_token_store(
    kind: str, *, file_path: str, keyvault_url: str | None, secret_name: str
) -> TokenStore:
    if kind == "keyvault":
        if not keyvault_url:
            raise RuntimeError("TOKEN_STORE=keyvault requires KEYVAULT_URL")
        return KeyVaultTokenStore(keyvault_url, secret_name)
    if kind == "file":
        return FileTokenStore(file_path)
    raise RuntimeError(f"Unknown TOKEN_STORE: {kind}")
