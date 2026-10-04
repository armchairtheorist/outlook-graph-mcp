<#
.SYNOPSIS
  One-command deploy to Azure Container Apps (Windows / PowerShell equivalent of deploy.sh).
  Idempotent: safe to re-run.

.PREREQS
  - Azure CLI installed and `az login` done
  - deploy.env next to this script with ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET
    (copy deploy.env.example). Optional: RG, LOCATION, NAME.
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---- load deploy.env (KEY=VALUE lines) ----
if (Test-Path deploy.env) {
  Get-Content deploy.env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim()
  }
}
if (-not $env:ENTRA_CLIENT_ID)     { throw "ENTRA_CLIENT_ID not set (deploy.env)" }
if (-not $env:ENTRA_CLIENT_SECRET) { throw "ENTRA_CLIENT_SECRET not set (deploy.env)" }
$RG       = if ($env:RG)       { $env:RG }       else { "graph-mcp-rg" }
$LOCATION = if ($env:LOCATION) { $env:LOCATION } else { "southeastasia" }
$NAME     = if ($env:NAME)     { $env:NAME }     else { "graphmcp" }
$STATE    = ".deploy-state.env"

# ---- generated secrets persist across runs so re-deploys don't log Claude out ----
if (Test-Path $STATE) {
  Get-Content $STATE | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item -Path "Env:$k" -Value $v }
}
function New-Hex([int]$bytes) { -join ((1..$bytes) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) }) }
if (-not $env:JWT_SIGNING_KEY)      { $env:JWT_SIGNING_KEY      = New-Hex 32 }
if (-not $env:WEBHOOK_CLIENT_STATE) { $env:WEBHOOK_CLIENT_STATE = New-Hex 16 }
"JWT_SIGNING_KEY=$($env:JWT_SIGNING_KEY)`nWEBHOOK_CLIENT_STATE=$($env:WEBHOOK_CLIENT_STATE)" | Set-Content $STATE

Write-Host "==> Resource group $RG in $LOCATION"
az group create -n $RG -l $LOCATION -o none
$DEPLOYER_OID = az ad signed-in-user show --query id -o tsv

$common = @(
  "-g", $RG, "-f", "infra/main.bicep", "-n", "graphmcp-infra", "-o", "none",
  "-p", "name=$NAME", "entraClientId=$($env:ENTRA_CLIENT_ID)", "entraClientSecret=$($env:ENTRA_CLIENT_SECRET)",
  "jwtSigningKey=$($env:JWT_SIGNING_KEY)", "webhookClientState=$($env:WEBHOOK_CLIENT_STATE)", "deployerObjectId=$DEPLOYER_OID"
)

Write-Host "==> Deploying infrastructure (pass 1)"
az deployment group create @common
$ACR    = az deployment group show -g $RG -n graphmcp-infra --query properties.outputs.acrLoginServer.value -o tsv
$KV_URL = az deployment group show -g $RG -n graphmcp-infra --query properties.outputs.keyVaultUrl.value -o tsv

$TAG = try { (git rev-parse --short HEAD 2>$null) } catch { $null }
if (-not $TAG) { $TAG = [int][double]::Parse((Get-Date -UFormat %s)) }
$IMAGE = "$ACR/graph-mcp:$TAG"
Write-Host "==> Building $IMAGE in ACR (cloud build, no local Docker)"
az acr build -r ($ACR.Split('.')[0]) -t "graph-mcp:$TAG" -t "graph-mcp:latest" . -o none

Write-Host "==> Deploying app (pass 2)"
az deployment group create @common "image=$IMAGE"
$URL = az deployment group show -g $RG -n graphmcp-infra --query properties.outputs.url.value -o tsv

Write-Host @"

================================================================
 Deployed.

 Server URL (for Claude):    $URL/mcp
 OAuth callback to register: $URL/auth/callback
 Key Vault:                  $KV_URL

 Next steps (see README):
   1. Entra app > Authentication > add Web redirect URI: $URL/auth/callback
   2. Seed the Graph token into Key Vault:
        `$env:TOKEN_STORE="keyvault"; `$env:KEYVAULT_URL="$KV_URL"
        `$env:ENTRA_CLIENT_ID="$($env:ENTRA_CLIENT_ID)"; `$env:ENTRA_CLIENT_SECRET="<secret>"
        python scripts/seed_token.py
   3. Restart so the app picks up the token:
        az containerapp revision restart -g $RG -n $NAME --revision (az containerapp revision list -g $RG -n $NAME --query "[0].name" -o tsv)
   4. Claude > Settings > Connectors > Add custom connector > $URL/mcp
================================================================
"@
