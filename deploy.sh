#!/usr/bin/env bash
# One-command deploy to Azure Container Apps. Idempotent: safe to re-run.
#
# Prereqs: az CLI (logged in), Docker NOT required (image is built in ACR).
# Required env (or a deploy.env file next to this script):
#   ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET
# Optional: RG (default graph-mcp-rg), LOCATION (default southeastasia), NAME (default graphmcp)
set -euo pipefail
cd "$(dirname "$0")"
[ -f deploy.env ] && set -a && . ./deploy.env && set +a

: "${ENTRA_CLIENT_ID:?set ENTRA_CLIENT_ID}"
: "${ENTRA_CLIENT_SECRET:?set ENTRA_CLIENT_SECRET}"
RG="${RG:-graph-mcp-rg}"
LOCATION="${LOCATION:-southeastasia}"
NAME="${NAME:-graphmcp}"
STATE=".deploy-state.env"   # keeps generated secrets stable across runs (git-ignored)

# Generated secrets persist in $STATE so re-deploys don't log Claude out.
if [ -f "$STATE" ]; then . "./$STATE"; fi
JWT_SIGNING_KEY="${JWT_SIGNING_KEY:-$(openssl rand -hex 32)}"
WEBHOOK_CLIENT_STATE="${WEBHOOK_CLIENT_STATE:-$(openssl rand -hex 16)}"
printf 'JWT_SIGNING_KEY=%s\nWEBHOOK_CLIENT_STATE=%s\n' "$JWT_SIGNING_KEY" "$WEBHOOK_CLIENT_STATE" > "$STATE"
chmod 600 "$STATE"

echo "==> Resource group $RG in $LOCATION"
az group create -n "$RG" -l "$LOCATION" -o none
DEPLOYER_OID=$(az ad signed-in-user show --query id -o tsv)

# Pass 1: infrastructure with a placeholder image (ACR must exist before we can push).
echo "==> Deploying infrastructure (pass 1)"
az deployment group create -g "$RG" -f infra/main.bicep -n graphmcp-infra -o none \
  -p name="$NAME" entraClientId="$ENTRA_CLIENT_ID" entraClientSecret="$ENTRA_CLIENT_SECRET" \
     jwtSigningKey="$JWT_SIGNING_KEY" webhookClientState="$WEBHOOK_CLIENT_STATE" deployerObjectId="$DEPLOYER_OID"
ACR=$(az deployment group show -g "$RG" -n graphmcp-infra --query properties.outputs.acrLoginServer.value -o tsv)
KV_URL=$(az deployment group show -g "$RG" -n graphmcp-infra --query properties.outputs.keyVaultUrl.value -o tsv)

# Build the image in the cloud (no local Docker needed).
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
IMAGE="$ACR/graph-mcp:$TAG"
echo "==> Building $IMAGE in ACR"
az acr build -r "${ACR%%.*}" -t "graph-mcp:$TAG" -t "graph-mcp:latest" . -o none

# Pass 2: point the app at the real image.
echo "==> Deploying app (pass 2)"
az deployment group create -g "$RG" -f infra/main.bicep -n graphmcp-infra -o none \
  -p name="$NAME" entraClientId="$ENTRA_CLIENT_ID" entraClientSecret="$ENTRA_CLIENT_SECRET" \
     jwtSigningKey="$JWT_SIGNING_KEY" webhookClientState="$WEBHOOK_CLIENT_STATE" deployerObjectId="$DEPLOYER_OID" \
     image="$IMAGE"
URL=$(az deployment group show -g "$RG" -n graphmcp-infra --query properties.outputs.url.value -o tsv)

cat <<EOF

================================================================
 Deployed.

 Server URL (for Claude):   $URL/mcp
 OAuth callback to register: $URL/auth/callback
 Key Vault:                 $KV_URL

 Next steps (see README):
   1. Add "$URL/auth/callback" as a Web redirect URI on the Entra app.
   2. Seed the Graph token into Key Vault:
        TOKEN_STORE=keyvault KEYVAULT_URL=$KV_URL \\
        ENTRA_CLIENT_ID=$ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET=... \\
        python scripts/seed_token.py
   3. Restart so the app picks up the token:
        az containerapp revision restart -g $RG -n $NAME --revision \$(az containerapp revision list -g $RG -n $NAME --query '[0].name' -o tsv)
   4. In Claude: Settings > Connectors > Add custom connector > $URL/mcp
================================================================
EOF
