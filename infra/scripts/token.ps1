# Signs in to Warden through Keycloak and returns request headers for PowerShell:
#   $h = .\infra\scripts\token.ps1 clerk
#   Invoke-RestMethod http://localhost:8100/runs -Headers $h
# Asks for the password (it isn't stored). Sign in to the console once first, so
# Keycloak has replaced the temporary password. Tokens last 15 minutes.
param([Parameter(Mandatory = $true)][string]$Username)
$ErrorActionPreference = "Stop"
$secure = Read-Host "Password for $Username" -AsSecureString
$plain = [System.Net.NetworkCredential]::new("", $secure).Password
$body = @{ grant_type = "password"; client_id = "warden-cli"; username = $Username; password = $plain; scope = "openid" }
$token = Invoke-RestMethod -Method Post -Uri "http://localhost:8180/realms/warden/protocol/openid-connect/token" `
  -ContentType "application/x-www-form-urlencoded" -Body $body
@{ Authorization = "Bearer $($token.access_token)" }
