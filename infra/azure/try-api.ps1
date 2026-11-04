<#
Phase 11b-2: plays the outside system. Submits an invoice to Warden through API Management as the
"Warden intake" app, and shows what each layer refuses on the way.

    .\infra\azure\try-api.ps1

Needs `az login` and .azure-warden.json from deploy.ps1. Prints one line per check:

  1. no subscription key                   -> 401 from API Management
  2. a key but no token                    -> 401 from API Management (the token check)
  3. a token, straight to the backend      -> 403 from the backend (no front-door key)
  4. key and token, through the gateway    -> 202: the invoice is accepted and a run starts
  5. the run, followed through the gateway -> its status until it settles or waits for a person
  6. a burst of requests                   -> 429 from API Management once the rate limit is hit
#>
param([string]$ResourceGroup = "")

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StateFile = Join-Path $Root ".azure-warden.json"
if (-not (Test-Path $StateFile)) { throw "No .azure-warden.json: run .\infra\azure\deploy.ps1 first." }
$state = Get-Content $StateFile -Raw | ConvertFrom-Json
if (-not $ResourceGroup) { $ResourceGroup = $state.resourceGroup }
if (-not $state.intakeClientSecret) { throw "No intake app yet: run .\infra\azure\deploy.ps1 again (phase 11b-2)." }

function Invoke-Az {
    $out = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

# Every request returns its status and body instead of throwing on 4xx/5xx.
function Send([string]$Method, [string]$Url, [hashtable]$Headers = @{}, $Body = $null) {
    $request = @{ Method = $Method; Uri = $Url; Headers = $Headers; UseBasicParsing = $true; TimeoutSec = 180 }
    if ($null -ne $Body) { $request.Body = ($Body | ConvertTo-Json -Depth 5); $request.ContentType = "application/json" }
    try {
        $r = Invoke-WebRequest @request
        return @{ status = [int]$r.StatusCode; body = $r.Content }
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) { throw }
        $text = ""
        try { $text = (New-Object System.IO.StreamReader($response.GetResponseStream())).ReadToEnd() } catch { }
        return @{ status = [int]$response.StatusCode; body = $text }
    }
}

function Check([string]$what, $result, [int[]]$expected) {
    $mark = if ($expected -contains $result.status) { "as expected" } else { "NOT EXPECTED" }
    $detail = ($result.body -replace '\s+', ' ')
    if ($detail.Length -gt 110) { $detail = $detail.Substring(0, 110) + "..." }
    Write-Host ("{0,-44} {1}  {2}   {3}" -f $what, $result.status, $mark, $detail)
}

Write-Host "Finding the gateway, the backend and the intake subscription's key..."
$subscription = (Invoke-Az account show --query id -o tsv)
$apim = (Invoke-Az apim list -g $ResourceGroup --query "[0].[name, gatewayUrl]" -o tsv) -split "\s+"
$gateway = "$($apim[1])/warden"
$backend = "https://" + (Invoke-Az containerapp show -n warden-backend -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv)
$key = Invoke-Az rest --method post --uri ("https://management.azure.com/subscriptions/$subscription/resourceGroups/$ResourceGroup" +
    "/providers/Microsoft.ApiManagement/service/$($apim[0])/subscriptions/intake/listSecrets?api-version=2022-08-01") `
    --query primaryKey -o tsv
$tenant = (Invoke-Az account show --query tenantId -o tsv)
$token = (Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$tenant/oauth2/v2.0/token" -Body @{
        client_id = $state.intakeAppId; client_secret = $state.intakeClientSecret
        scope = "api://$($state.signInAppId)/.default"; grant_type = "client_credentials" }).access_token
Write-Host "Gateway: $gateway"
Write-Host "Signed in as the Warden intake app ($($state.intakeAppId)); first calls may wait while Warden wakes."
Write-Host ""

$number = "K-2026-" + (Get-Date -Format "HHmmss")
$invoice = @"
TAX INVOICE

Kestrel Office Supplies Sdn Bhd
No. 12, Jalan Perindustrian 3, 47500 Subang Jaya, Selangor
Tel: 03-5631 2200

Invoice No: $number
Date: $(Get-Date -Format "dd/MM/yyyy")
Bill To: Warden Demo Co.

Description                     Qty   Unit Price    Amount (RM)
Printer Paper (ream)             10        12.00        120.00
Desk Organiser                    2        45.00         90.00

TOTAL (RM)                                              210.00
"@
$auth = @{ Authorization = "Bearer $token" }
$keyed = @{ "Ocp-Apim-Subscription-Key" = $key }

Check "1. No subscription key" (Send POST "$gateway/runs" $auth @{ invoice_text = $invoice }) @(401)
Check "2. A key but no token" (Send POST "$gateway/runs" $keyed @{ invoice_text = $invoice }) @(401)
Check "3. A token, straight to the backend" (Send POST "$backend/runs" $auth @{ invoice_text = $invoice }) @(403)
$submitted = Send POST "$gateway/runs" ($keyed + $auth) @{ invoice_text = $invoice }
Check "4. Key and token, through the gateway" $submitted @(202)

if ($submitted.status -eq 202) {
    $runId = ($submitted.body | ConvertFrom-Json).run_id
    Write-Host ("{0,-44} invoice {1}, run {2}" -f "", $number, $runId)
    $status = "submitted"
    for ($i = 0; $i -lt 36 -and $status -in @("submitted", "extracted", "scored", "approved", "settling", "resuming", "retrying"); $i++) {
        Start-Sleep -Seconds 5
        $seen = Send GET "$gateway/runs/$runId" ($keyed + $auth)
        if ($seen.status -eq 200) { $status = ($seen.body | ConvertFrom-Json).status }
    }
    Write-Host ("{0,-44} {1}" -f "5. The run, through the gateway", $status)
}

$codes = @()
for ($i = 0; $i -lt 12; $i++) { $codes += (Send GET "$gateway/runs/none" ($keyed + $auth)).status }
$first429 = [array]::IndexOf($codes, 429)
$summary = if ($first429 -ge 0) { "429 from call $($first429 + 1) of 12, as expected" } else { "no 429: NOT EXPECTED" }
Write-Host ("{0,-44} {1}   ({2})" -f "6. A burst of 12 requests", $summary, ($codes -join " "))
Write-Host ""
Write-Host "In the console, the run shows as submitted by app:$($state.intakeAppId), and the Audit log's"
Write-Host "'Submitted' entry for it records that it came in through the gateway (via apim)."
