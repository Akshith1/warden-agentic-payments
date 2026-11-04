<#
Phase 11: removes everything deploy.ps1 created, so nothing is left to bill.

    .\infra\azure\teardown.ps1               # everything, including sign-in
    .\infra\azure\teardown.ps1 -KeepSignIn   # keep the Entra ID apps and test people (they cost nothing)

It deletes the resource group (every app, Postgres and its data, the registry, the logs,
Key Vault, the write-once audit copy, after removing its unlocked policy), purges Key Vault so its name can be used again, and by default removes the two
app registrations and the two test people, and .azure-warden.json. Takes 5-15 minutes.
The audit log and ledger on Azure are deleted with Postgres; the local stack is untouched.
#>
param(
    [string]$ResourceGroup = "",
    [switch]$KeepSignIn,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StateFile = Join-Path $Root ".azure-warden.json"
$Graph = "https://graph.microsoft.com/v1.0"
$state = if (Test-Path $StateFile) { Get-Content $StateFile -Raw | ConvertFrom-Json } else { New-Object PSObject }
if (-not $ResourceGroup) { $ResourceGroup = if ($state.resourceGroup) { $state.resourceGroup } else { "warden-rg" } }

function Invoke-Az {
    $out = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

$account = (Invoke-Az account show) -join "`n" | ConvertFrom-Json
Write-Host "Subscription: $($account.name)"
Write-Host "This deletes the resource group '$ResourceGroup' and everything in it, including the Azure ledger and audit log."
if (-not $KeepSignIn) { Write-Host "It also removes the 'Warden', 'Warden ledger' and 'Warden intake' app registrations and the test people amira and clerk." }
if (-not $Yes) {
    $answer = Read-Host "Type the resource group's name to confirm"
    if ($answer -ne $ResourceGroup) { Write-Host "Nothing deleted."; exit 1 }
}

if ((Invoke-Az group exists -n $ResourceGroup) -eq "true") {
    # Phase 11b: the audit copy's write-once policy is unlocked, so it can be removed; without it
    # the container and its blobs can be deleted with everything else.
    $accounts = Invoke-Az storage account list -g $ResourceGroup --query "[?tags.purpose=='audit'].name" -o tsv
    foreach ($account in @($accounts | Where-Object { $_ })) {
        $etag = Invoke-Az storage container immutability-policy show -g $ResourceGroup --account-name $account `
            --container-name audit --query etag -o tsv
        if ($etag) {
            Write-Host "Removing the write-once policy on $account/audit"
            Invoke-Az storage container immutability-policy delete -g $ResourceGroup --account-name $account `
                --container-name audit --if-match $etag | Out-Null
        }
    }
    Write-Host "Deleting $ResourceGroup (5-15 minutes)..."
    Invoke-Az group delete -n $ResourceGroup --yes | Out-Null
} else {
    Write-Host "$ResourceGroup doesn't exist; nothing to delete there."
}

# A deleted API Management service is kept for 48 hours and holds on to its name; purge it too.
$apims = Invoke-Az apim deletedservice list --query "[?starts_with(name, 'warden-apim-')].[name, location]" -o tsv
foreach ($line in @($apims | Where-Object { $_ })) {
    $name, $where = $line -split "\s+", 2
    Write-Host "Purging API Management $name"
    Invoke-Az apim deletedservice purge --service-name $name --location $where | Out-Null
}

# A deleted vault is kept for 7 days and holds on to its name; purge it so a redeploy works.
$deleted = (Invoke-Az keyvault list-deleted --resource-type vault --query "[?starts_with(name, 'warden-kv-')].name" -o tsv)
foreach ($name in @($deleted | Where-Object { $_ })) {
    Write-Host "Purging Key Vault $name"
    Invoke-Az keyvault purge -n $name | Out-Null
}

if (-not $KeepSignIn) {
    foreach ($displayName in "Warden", "Warden ledger", "Warden intake") {
        $ids = Invoke-Az ad app list --filter "displayName eq '$displayName'" --query "[].id" -o tsv
        foreach ($id in @($ids | Where-Object { $_ })) {
            Write-Host "Removing app registration $displayName"
            Invoke-Az rest --method DELETE --uri "$Graph/applications/$id" | Out-Null
        }
    }
    $people = @()
    if ($state.people) { $people = @($state.people.PSObject.Properties | ForEach-Object { $_.Value.upn }) }
    foreach ($upn in $people) {
        Write-Host "Removing $upn"
        & az ad user delete --id $upn --only-show-errors
    }
    if (Test-Path $StateFile) { Remove-Item $StateFile -Force }
}

Write-Host ""
Write-Host "Done. Check the portal's Cost Management in a day or two: charges stop once the deletion finishes."
