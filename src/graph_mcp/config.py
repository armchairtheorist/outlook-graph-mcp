"""Runtime configuration. Everything comes from environment variables (or a .env file locally)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Graph delegated scopes requested for the personal account. Keep this list in sync with
# the "API permissions" page of the Entra app registration.
GRAPH_SCOPES: list[str] = [
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "MailboxSettings.ReadWrite",
    "Calendars.ReadWrite",
    "Contacts.ReadWrite",
    "Files.ReadWrite",
    "Notes.ReadWrite",
    "Tasks.ReadWrite",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Entra app registration (one app serves both auth layers) ---
    entra_client_id: str = Field(alias="ENTRA_CLIENT_ID")
    entra_client_secret: str = Field(alias="ENTRA_CLIENT_SECRET")
    # "consumers" = personal Microsoft accounts only. Do not change for a live.com/outlook.com account.
    entra_tenant: str = Field(default="consumers", alias="ENTRA_TENANT")

    # --- Public identity of this server ---
    # e.g. https://graph-mcp.<hash>.southeastasia.azurecontainerapps.io
    base_url: str = Field(default="http://localhost:8000", alias="BASE_URL")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # Stable key used to sign the tokens this server issues to Claude and to encrypt its
    # on-disk OAuth state. Rotating it logs every client out. 32+ random bytes, base64 or hex.
    jwt_signing_key: str | None = Field(default=None, alias="JWT_SIGNING_KEY")

    # --- Where the Graph refresh token lives ---
    # "keyvault" in Azure, "file" for local development.
    token_store: str = Field(default="file", alias="TOKEN_STORE")
    token_file: str = Field(default=".graph_token.json", alias="TOKEN_FILE")
    keyvault_url: str | None = Field(default=None, alias="KEYVAULT_URL")
    keyvault_secret_name: str = Field(default="graph-refresh-token", alias="KEYVAULT_SECRET_NAME")

    # --- Access control ---
    # Extra Entra object IDs allowed to use this server, comma-separated. The mailbox owner
    # (whoever ran scripts/seed_token.py) is always allowed. Normally leave empty.
    extra_allowed_oids: str = Field(default="", alias="EXTRA_ALLOWED_OIDS")

    # --- Webhooks ---
    # Shared secret echoed back by Graph in every notification so we can trust it.
    webhook_client_state: str | None = Field(default=None, alias="WEBHOOK_CLIENT_STATE")
    # Comma-separated resources to subscribe to on startup, e.g. "/me/mailFolders('inbox')/messages,/me/events".
    webhook_resources: str = Field(default="", alias="WEBHOOK_RESOURCES")

    # Local time zone name (Windows or IANA) used when formatting calendar output.
    time_zone: str = Field(default="Singapore Standard Time", alias="TIME_ZONE")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def allowed_oids(self) -> set[str]:
        return {o.strip() for o in self.extra_allowed_oids.split(",") if o.strip()}

    @property
    def webhook_resource_list(self) -> list[str]:
        return [r.strip() for r in self.webhook_resources.split(",") if r.strip()]

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant}"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
