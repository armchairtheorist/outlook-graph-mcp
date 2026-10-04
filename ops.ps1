<#
.SYNOPSIS
  Day-to-day operations for the deployed server. Requires `az login`.

.USAGE
  .\ops.ps1 restart                      # restart the running revision (e.g. after seeding/rotating secrets)
  .\ops.ps1 status                       # revision, replica state, health endpoint
  .\ops.ps1 logs [-Tail 100] [-Follow]   # container logs
  .\ops.ps1 rotate-secret                # prompt for a new Entra client secret, store in Key Vault, restart
  .\ops.ps1 reseed                       # re-run the Graph sign-in (scripts/seed_token.ps1) and restart
#>
param(
  [Parameter(Position = 0)]
  [ValidateSet("restart", "status", "logs", "rotate-secret", "reseed")]
  [string]$Action = "status",
  [int]$Tail = 100,
  [switch]$Follow
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---- names (keep in sync with deploy.env / deploy.ps1) ----
if (Test-Path deploy.env) {
  Get-Content deploy.env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim()
  }
}
$RG   = if ($env:RG)   { $env:RG }   else { "graph-mcp-rg" }
$NAME = if ($env:NAME) { $env:NAME } else { "graphmcp" }

function Get-Vault { az keyvault list -g $RG --query "[0].name" -o tsv }
function Get-Url   { "https://" + (az containerapp show -g $RG -n $NAME --query properties.configuration.ingress.fqdn -o tsv) }

function Restart-App {
  $rev = az containerapp revision list -g $RG -n $NAME --query "[?properties.active].name | [0]" -o tsv
  Write-Host "==> Restarting revision $rev"
  az containerapp revision restart -g $RG -n $NAME --revision $rev -o none
  Start-Sleep -Seconds 8
  Show-Health
}

function Show-Health {
  $url = Get-Url
  try   { $h = Invoke-RestMethod "$url/healthz"; Write-Host "healthz: ok=$($h.ok) owner=$($h.owner)" }
  catch { Write-Warning "healthz not reachable yet: $($_.Exception.Message)" }
}

switch ($Action) {
  "restart" { Restart-App }

  "status" {
    az containerapp revision list -g $RG -n $NAME -o table `
      --query "[].{revision:name, active:properties.active, replicas:properties.replicas, state:properties.runningState, created:properties.createdTime}"
    Write-Host "URL: $(Get-Url)/mcp"
    Show-Health
  }

  "logs" {
    if ($Follow) { az containerapp logs show -g $RG -n $NAME --follow }
    else         { az containerapp logs show -g $RG -n $NAME --tail $Tail }
  }

  "rotate-secret" {
    $vault = Get-Vault
    $sec = Read-Host "Paste the NEW Entra client secret value" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringUni([Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($sec))
    az keyvault secret set --vault-name $vault -n entra-client-secret --value $plain -o none
    Write-Host "Stored in Key Vault '$vault'. Updating deploy.env ..."
    if (Test-Path deploy.env) {
      (Get-Content deploy.env) -replace '^ENTRA_CLIENT_SECRET=.*', "ENTRA_CLIENT_SECRET=$plain" | Set-Content deploy.env
    }
    Restart-App
    Write-Host "Done. Delete the old secret in Entra > Certificates & secrets."
  }

  "reseed" {
    $vault = Get-Vault
    & "$PSScriptRoot\scripts\seed_token.ps1" -KeyVaultUrl "https://$vault.vault.azure.net/"
    Restart-App
  }
}
