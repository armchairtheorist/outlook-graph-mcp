<#
.SYNOPSIS
  One-time interactive sign-in as the mailbox owner; stores the Graph refresh token.
  PowerShell equivalent of scripts/seed_token.py (no Python needed).

.USAGE
  From the repo root, after `az login`:
    .\scripts\seed_token.ps1 -KeyVaultUrl https://<vault>.vault.azure.net/
  Client ID/secret are read from deploy.env (or pass -ClientId/-ClientSecret).

  A browser opens for the Microsoft login. Sign in with the personal account whose mailbox
  Claude should manage. http://localhost:8400 must be a Web redirect URI on the Entra app.
#>
param(
  [string]$KeyVaultUrl,
  [string]$ClientId,
  [string]$ClientSecret,
  [string]$SecretName = "graph-refresh-token",
  [string]$Tenant = "consumers",
  [int]$Port = 8400
)
$ErrorActionPreference = "Stop"

# ---- config from deploy.env if not passed ----
$envFile = Join-Path $PSScriptRoot "..\deploy.env"
if (Test-Path $envFile) {
  Get-Content $envFile | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    if ($k.Trim() -eq "ENTRA_CLIENT_ID"     -and -not $ClientId)     { $ClientId = $v.Trim() }
    if ($k.Trim() -eq "ENTRA_CLIENT_SECRET" -and -not $ClientSecret) { $ClientSecret = $v.Trim() }
  }
}
if (-not $ClientId -or -not $ClientSecret) { throw "Need ClientId and ClientSecret (deploy.env or parameters)" }
if (-not $KeyVaultUrl) { throw "Pass -KeyVaultUrl https://<vault>.vault.azure.net/" }

$scopes = "openid profile offline_access User.Read Mail.ReadWrite Mail.Send MailboxSettings.ReadWrite Calendars.ReadWrite Contacts.ReadWrite Files.ReadWrite Notes.ReadWrite Tasks.ReadWrite"
$redirect = "http://localhost:$Port/"
$authority = "https://login.microsoftonline.com/$Tenant/oauth2/v2.0"

# PKCE
$verifierBytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($verifierBytes)
$verifier = [Convert]::ToBase64String($verifierBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
$sha = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::ASCII.GetBytes($verifier))
$challenge = [Convert]::ToBase64String($sha).TrimEnd('=').Replace('+','-').Replace('/','_')
$state = [Guid]::NewGuid().ToString("N")

$authUrl = "$authority/authorize?client_id=$ClientId&response_type=code&redirect_uri=$([Uri]::EscapeDataString($redirect))" +
           "&response_mode=query&scope=$([Uri]::EscapeDataString($scopes))&state=$state&prompt=select_account" +
           "&code_challenge=$challenge&code_challenge_method=S256"

# ---- local listener for the redirect (raw TCP: no admin/URL-ACL needed) ----
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
$listener.Start()
Write-Host "Opening browser for sign-in ($Tenant)..."
Start-Process $authUrl
Write-Host "If no browser opened, visit:`n$authUrl`n"

$client = $listener.AcceptTcpClient()
$stream = $client.GetStream()
$reader = New-Object IO.StreamReader($stream)
$requestLine = $reader.ReadLine()                       # e.g. GET /?code=...&state=... HTTP/1.1
while (($line = $reader.ReadLine()) -ne "") { }          # drain headers
$html = "<html><body style='font-family:sans-serif'>Signed in. You can close this tab.</body></html>"
$resp = "HTTP/1.1 200 OK`r`nContent-Type: text/html`r`nContent-Length: $($html.Length)`r`nConnection: close`r`n`r`n$html"
$bytes = [Text.Encoding]::ASCII.GetBytes($resp); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush()
$client.Close(); $listener.Stop()

$path = ($requestLine -split ' ')[1]
$q = @{}
if ($path -match '\?(.*)$') {
  foreach ($kv in $Matches[1] -split '&') { $k, $v = $kv -split '=', 2; $q[[Uri]::UnescapeDataString($k)] = [Uri]::UnescapeDataString($v) }
}

if ($q["error"]) { throw "Sign-in failed: $($q['error']): $($q['error_description'])" }
if ($q["state"] -ne $state) { throw "State mismatch; aborting." }

# ---- exchange code ----
$tok = Invoke-RestMethod -Method Post -Uri "$authority/token" -ContentType "application/x-www-form-urlencoded" -Body @{
  client_id = $ClientId; client_secret = $ClientSecret; grant_type = "authorization_code"
  code = $q["code"]; redirect_uri = $redirect; code_verifier = $verifier; scope = $scopes
}
if (-not $tok.refresh_token) { throw "No refresh_token in response. Is offline_access granted?" }
if (-not $tok.id_token) { throw "No id_token in response (openid scope missing?)" }

# ---- decode id_token claims (no signature check needed; came straight from the token endpoint) ----
$payload = $tok.id_token.Split('.')[1].Replace('-','+').Replace('_','/')
switch ($payload.Length % 4) { 2 { $payload += "==" } 3 { $payload += "=" } }
$claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)) | ConvertFrom-Json

$stored = @{
  refresh_token   = $tok.refresh_token
  oid             = $claims.oid
  username        = $claims.preferred_username
  home_account_id = $null
} | ConvertTo-Json -Compress

# ---- write to Key Vault via az (uses your existing az login) ----
$vaultName = ([Uri]$KeyVaultUrl).Host.Split('.')[0]
$tmp = New-TemporaryFile
Set-Content -Path $tmp -Value $stored -NoNewline
try {
  az keyvault secret set --vault-name $vaultName --name $SecretName --file $tmp --content-type application/json -o none
} finally { Remove-Item $tmp -Force }

Write-Host "Stored refresh token for $($claims.preferred_username) (oid $($claims.oid)) in Key Vault '$vaultName'."
Write-Host "This account is now the only one allowed to use the MCP server."
