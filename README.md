# outlook-graph-mcp

A remote MCP server that gives Claude access to a **personal Microsoft account** (Outlook.com
mail and calendar today; OneDrive, OneNote and To Do planned) through Microsoft Graph.
Claude's built-in Microsoft 365 connector only works for work/school tenants; this fills the gap.

```
Claude ──OAuth (Entra, consumers tenant)──▶  FastMCP server on Azure Container Apps
                                               │  owner-only allowlist
                                               │  refresh token in Key Vault (managed identity)
                                               └──▶ Microsoft Graph  ◀── webhooks (change notifications)
```

**Current tools** — mail: list, search, read, thread, attachments, send, reply, forward, draft,
mark/flag/categorize, move, folders, server-side rules, auto-replies.
Calendar: calendars, view (expanded recurrences), get, search, create, update, delete/cancel,
accept/decline, free-slot finder. Meta: `whoami`, `events_recent` (webhook feed), `subscriptions_status`.

## How auth works (read this once)

Personal accounts have no unattended/service flow in Graph, so there are two layers:

1. **Claude → server.** Standard MCP OAuth. FastMCP proxies the flow to Microsoft Entra
   (`login.microsoftonline.com/consumers`). After login the server checks the account's object ID
   against the mailbox owner's and rejects everyone else. The URL can be public.
2. **Server → Graph.** You sign in **once** with `scripts/seed_token.py`; the refresh token is stored
   in Key Vault and silently rotated. Re-seed only if Microsoft revokes it (password change, ~90 days
   unused, security event).

One Entra app registration serves both layers.

---

## Deployment (≈20 minutes, once)

### 0. Prerequisites

- Azure subscription + [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) ≥ 2.60 (`az login`)
- Python 3.12+ locally (only for the seed script)
- This repo cloned

### 1. Register the Entra app (portal; cannot be scripted for personal-account apps)

Go to <https://entra.microsoft.com> → **App registrations** → **New registration**.

| Field | Value |
|---|---|
| Name | `Claude Graph MCP` |
| Supported account types | **Personal Microsoft accounts only** |
| Redirect URI | Web → `http://localhost:8400` (for the seed script) |

Then, on the new app:

1. **Overview** → copy **Application (client) ID** → this is `ENTRA_CLIENT_ID`.
2. **Certificates & secrets** → New client secret (24 months) → copy the *Value* → `ENTRA_CLIENT_SECRET`.
3. **API permissions** → Add → Microsoft Graph → Delegated → tick:
   `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `MailboxSettings.ReadWrite`, `Calendars.ReadWrite`,
   `Contacts.ReadWrite`, `Files.ReadWrite`, `Notes.ReadWrite`, `Tasks.ReadWrite`, `offline_access`.
   (No admin consent exists for personal accounts; you consent at sign-in.)
4. **Expose an API** → *Set* Application ID URI (accept default `api://<client-id>`) → **Add a scope**:
   name `mcp.access`, who can consent: *Admins and users*, display name/description "Access the MCP server".
5. **Manifest** → set `"requestedAccessTokenVersion": 2` → Save. (Required so tokens are v2 JWTs.)

You will come back to **Authentication** in step 3 to add the server's callback URL.

### 2. Deploy to Azure

```bash
cp deploy.env.example deploy.env   # fill in ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET
./deploy.sh                         # macOS/Linux/Git Bash
# or, on Windows PowerShell:
.\deploy.ps1
```

`deploy.sh` creates resource group `graph-mcp-rg` in Southeast Asia with: Container Apps
environment, Key Vault, Container Registry, Azure Files share, Log Analytics, managed identity,
and the Container App. The image is built in ACR (no local Docker). It prints the server URL.

Cost: roughly US$10–15/month (one always-on 0.25 vCPU replica dominates; everything else is cents).
Set `minReplicas: 0` in `infra/main.bicep` to scale to zero at the cost of ~5 s cold starts.

### 3. Register the callback URL

Entra → your app → **Authentication** → Web → **Add URI**:
`https://<printed-url>/auth/callback` → Save.

### 4. Seed the Graph token

```bash
pip install -e .
TOKEN_STORE=keyvault KEYVAULT_URL=https://<vault>.vault.azure.net \
ENTRA_CLIENT_ID=... ENTRA_CLIENT_SECRET=... \
python scripts/seed_token.py
```

A browser opens; sign in with the personal account whose mailbox Claude should manage and accept
the permissions. The script writes the token to Key Vault and prints the account's object ID.
Then restart the app so it loads the token (command printed by `deploy.sh`).

### 5. Connect Claude

Claude → **Settings → Connectors → Add custom connector** → URL `https://<printed-url>/mcp`.
Leave client ID/secret blank (the server supports dynamic client registration). Sign in with the
**same** Microsoft account. Any other account is rejected with 401 even after a successful login.

Verify: ask Claude *"run whoami"*.

### 6. (Optional) Continuous deployment from GitHub

Pushes to `main` run tests and roll out a new image via OIDC. One-time setup:

```bash
SUB=$(az account show --query id -o tsv); TENANT=$(az account show --query tenantId -o tsv)
APP_ID=$(az ad app create --display-name graph-mcp-deployer --query appId -o tsv)
az ad sp create --id $APP_ID -o none
az role assignment create --assignee $APP_ID --role Contributor --scope /subscriptions/$SUB/resourceGroups/graph-mcp-rg -o none
az ad app federated-credential create --id $APP_ID --parameters '{
  "name":"github-main","issuer":"https://token.actions.githubusercontent.com",
  "subject":"repo:<owner>/<repo>:environment:production","audiences":["api://AzureADTokenExchange"]}'
```

Then in GitHub → repo **Settings → Secrets and variables → Actions → Variables** add:
`AZURE_CLIENT_ID=$APP_ID`, `AZURE_TENANT_ID=$TENANT`, `AZURE_SUBSCRIPTION_ID=$SUB`,
`AZURE_RG=graph-mcp-rg`, `AZURE_APP_NAME=graphmcp`. Create an environment named `production`.

---

## Local development

```bash
pip install -e ".[dev]"
cp .env.example .env            # fill in client id/secret
python scripts/seed_token.py    # writes .graph_token.json
graph-mcp                       # http://localhost:8000/mcp
pytest -q && ruff check .
```

To test from Claude locally, expose it with `ngrok http 8000` (or Cloudflare Tunnel), set
`BASE_URL` to the tunnel URL and add `<tunnel>/auth/callback` to the Entra app.
With `FASTMCP_SERVER_AUTH` unset and no HTTPS, FastMCP logs a non-secure-cookie warning; that's expected.

## Operations

| Task | How |
|---|---|
| Logs | `az containerapp logs show -g graph-mcp-rg -n graphmcp --follow` |
| Health | `curl https://<url>/healthz` → `{"ok":true,"owner":"you@live.com"}` |
| Rotate client secret | New secret in Entra → `az keyvault secret set --vault-name <kv> -n entra-client-secret --value ...` → restart |
| Re-seed Graph token | Step 4 again |
| Webhook subscriptions | Ask Claude to run `subscriptions_status`; renewed daily, 6.5-day lifetime |
| Log everyone out | Change `jwt-signing-key` in Key Vault and restart |

## Known Graph limits for personal accounts

- No unattended auth: the owner must sign in once (step 4).
- `findMeetingTimes` / `getSchedule` unavailable → `calendar_find_free_slots` computes locally.
- Excel workbook API unsupported on consumer OneDrive (download/edit/upload instead).
- Teams online meetings cannot be created.
- Unified `/search/query` unsupported; per-resource `$search` is used instead.

## Roadmap

- [ ] OneDrive tools (browse, upload/download, share links, delta)
- [ ] OneNote tools (notebooks, sections, create/append pages)
- [ ] Microsoft To Do tools
- [ ] Contacts
- [ ] Persist webhook feed to Azure Table so it survives restarts
