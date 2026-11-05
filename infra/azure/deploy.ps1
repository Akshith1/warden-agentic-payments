<#
Phase 11a: deploys Warden's core payment path to Azure.

    .\infra\azure\deploy.ps1                 # first deploy, and every redeploy after a change
    .\infra\azure\deploy.ps1 -SkipImages     # settings only: reuse the images pushed last time
    .\infra\azure\deploy.ps1 -LoadData       # also run the data loader again (it runs once by itself)

Run it from the repository folder, signed in with `az login`, with Docker Desktop running and
.env filled in (it reads the model keys and passwords from there). Safe to run again: every
step finds what exists and changes only what differs.

What it does, in order:
  1. Registers the Azure resource providers Warden uses (a new subscription has none).
  2. Sets up sign-in in Entra ID: two app registrations ("Warden" for people, "Warden ledger"
     for the services' identities), the three roles, you with all three, and two test people
     (amira: submitter and approver; clerk: submitter) for the four-eyes check.
  3. Deploys the foundations with Bicep (infra/azure/main.bicep): logs, registry, Key Vault,
     identities, Postgres, the Container Apps environment.
  4. Builds the images on this PC and pushes them to the registry.
  5. Deploys the apps and the database job, then runs the job.
  6. Prints the console's address and the test people's first passwords.

Values generated for Azure (Postgres admin and control passwords, the agents' gateway keys,
the sign-in secret, the test people's first passwords) are kept in .azure-warden.json beside
.env. It is gitignored; never commit it. teardown.ps1 deletes it with everything else.
#>
param(
    [string]$Location = "centralus",
    [string]$ResourceGroup = "warden-rg",
    [switch]$SkipImages,
    [switch]$LoadData,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$StateFile = Join-Path $Root ".azure-warden.json"
$Graph = "https://graph.microsoft.com/v1.0"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

# Runs az and stops on failure. A plain function (no param block) on purpose: every argument,
# -n and -o included, goes to az untouched instead of being read as a PowerShell parameter.
function Invoke-Az {
    $out = & az @args --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}
function Invoke-AzJson { $text = (Invoke-Az @args) -join "`n"; if ($text) { return $text | ConvertFrom-Json } }

# Microsoft Graph calls take a JSON body from a file: Windows PowerShell mangles quotes in
# JSON passed to native programs on the command line. A new app or person takes a few seconds
# to reach every Entra ID replica, so a failed call is tried again before giving up.
function Invoke-Graph {
    param([string]$Method, [string]$Uri, $Body)
    $file = $null
    if ($null -ne $Body) {
        $file = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($file, ($Body | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding $false))
    }
    try {
        for ($attempt = 1; ; $attempt++) {
            try {
                if ($file) { return Invoke-AzJson rest --method $Method --uri $Uri --headers "Content-Type=application/json" --body "@$file" }
                return Invoke-AzJson rest --method $Method --uri $Uri
            } catch {
                if ($attempt -ge 6) { throw }
                Write-Host "  (Entra ID not ready yet; trying again in 10 seconds)"
                Start-Sleep -Seconds 10
            }
        }
    } finally { if ($file) { Remove-Item $file -ErrorAction SilentlyContinue } }
}

function New-Secret([int]$Bytes = 24) {
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    return [Convert]::ToBase64String($buffer).Replace("+", "-").Replace("/", "_").TrimEnd("=")
}

function Read-DotEnv {
    $values = @{}
    if (-not (Test-Path (Join-Path $Root ".env"))) { throw ".env not found in $Root. Run this from the repository folder." }
    foreach ($line in Get-Content (Join-Path $Root ".env")) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Trim().Trim('"') }
    }
    return $values
}

function Save-State($state) {
    [System.IO.File]::WriteAllText($StateFile, ($state | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
}

# ------------------------------------------------------------------ checks
Step "Checking az, Docker and .env"
$account = Invoke-AzJson account show
$dotenv = Read-DotEnv
foreach ($name in "OPENAI_API_KEY", "GROQ_API_KEY", "PGPASS_WARDEN_LEDGER", "PGPASS_WARDEN_BACKEND", "VENDOR_LOOKUP_KEY", "LITELLM_MASTER_KEY") {
    if (-not $dotenv[$name]) { throw "$name is empty in .env" }
}
if (-not $SkipImages) {
    & docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Docker isn't running. Start Docker Desktop and try again." }
}
# Container Apps commands live in an az extension; install or update it without a prompt.
Invoke-Az extension add --name containerapp --upgrade | Out-Null
Write-Host "Subscription: $($account.name) ($($account.user.name))"
Write-Host "Region:       $Location    Resource group: $ResourceGroup"
if (-not $Yes) {
    $answer = Read-Host "Deploy Warden here? This creates billable resources (see the README for costs). [y/N]"
    if ($answer -notmatch '^(y|yes)$') { Write-Host "Nothing changed."; exit 1 }
}

$state = if (Test-Path $StateFile) { Get-Content $StateFile -Raw | ConvertFrom-Json } else { New-Object PSObject }
function Ensure-State($name, $value) {
    if (-not ($state.PSObject.Properties.Name -contains $name)) { $state | Add-Member -NotePropertyName $name -NotePropertyValue $value }
}
Ensure-State "pgAdminPassword" ((New-Secret 24) + "-Wd7")
Ensure-State "pgControlPassword" (New-Secret 24)
Ensure-State "pgQualityPassword" (New-Secret 24)  # phase 11c: the data-quality checks' read-only login
Ensure-State "entryKeyConsole" (New-Secret 32)    # phase 11b-2: the backend's front-door keys
Ensure-State "entryKeyGateway" (New-Secret 32)
$agents = @("guardrail", "extraction", "risk-scoring", "approval", "critic", "eval-gate")
$keys = [ordered]@{}; foreach ($a in $agents) { $keys[$a] = "sk-warden-$a-" + (New-Secret 24) }
Ensure-State "agentKeys" $keys
Save-State $state

# ------------------------------------------------------------------ providers
Step "Registering resource providers (first time: a few minutes)"
foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.ContainerRegistry",
                "Microsoft.DBforPostgreSQL", "Microsoft.KeyVault", "Microsoft.ManagedIdentity", "Microsoft.Storage", "Microsoft.ApiManagement") {
    $registered = Invoke-Az provider show --namespace $ns --query registrationState -o tsv
    if ($registered -ne "Registered") { Write-Host "  $ns"; Invoke-Az provider register --namespace $ns --wait | Out-Null }
}

# ------------------------------------------------------------------ Entra ID
Step "Setting up sign-in in Entra ID"
$tenantId = $account.tenantId
$me = Invoke-AzJson ad signed-in-user show
$domain = (Invoke-Graph GET "$Graph/domains").value | Where-Object { $_.isDefault } | Select-Object -First 1 -ExpandProperty id

function Find-App($displayName) {
    $found = Invoke-Graph GET "$Graph/applications?`$filter=displayName eq '$displayName'"
    return $found.value | Select-Object -First 1
}
function Ensure-ServicePrincipal($appId) {
    $sp = (Invoke-Graph GET "$Graph/servicePrincipals?`$filter=appId eq '$appId'").value | Select-Object -First 1
    if (-not $sp) { $sp = Invoke-Graph POST "$Graph/servicePrincipals" @{ appId = $appId } }
    return $sp
}

$roleDefs = @(
    @{ value = "submitter"; name = "Submitter"; text = "Submit invoices; see runs, the ledger and the rules" },
    @{ value = "approver"; name = "Approver"; text = "Decide escalated invoices up to 50,000.00; link vendor names" },
    @{ value = "controller"; name = "Controller"; text = "Decide escalated invoices of any amount; reset the kill switch" }
)

# "Warden": people sign in to the console with it, and the backend and control plane check
# its tokens. App roles carry submitter/approver/controller; "access" is the API scope.
$app = Find-App "Warden"
if (-not $app) {
    Write-Host "  creating the Warden app registration"
    $app = Invoke-Graph POST "$Graph/applications" @{
        displayName    = "Warden"
        signInAudience = "AzureADMyOrg"
        appRoles       = @($roleDefs | ForEach-Object { @{
                    id = [guid]::NewGuid().ToString(); value = $_.value; displayName = $_.name; description = $_.text
                    allowedMemberTypes = @("User"); isEnabled = $true } })
        api            = @{
            requestedAccessTokenVersion = 2
            oauth2PermissionScopes      = @(@{
                    id = [guid]::NewGuid().ToString(); value = "access"; type = "User"; isEnabled = $true
                    adminConsentDisplayName = "Use Warden"; adminConsentDescription = "Use Warden as the signed-in person"
                    userConsentDisplayName = "Use Warden"; userConsentDescription = "Use Warden as you" })
        }
    }
}
$signInAppId = $app.appId
$signInSp = Ensure-ServicePrincipal $signInAppId

# "Warden ledger": the audience of the tokens the backend's and policy service's managed
# identities send the ledger. People never sign in to it.
$ledgerApp = Find-App "Warden ledger"
if (-not $ledgerApp) {
    Write-Host "  creating the Warden ledger app registration"
    $ledgerApp = Invoke-Graph POST "$Graph/applications" @{
        displayName = "Warden ledger"; signInAudience = "AzureADMyOrg"; api = @{ requestedAccessTokenVersion = 2 } }
}
$ledgerAppId = $ledgerApp.appId
Ensure-ServicePrincipal $ledgerAppId | Out-Null
if (-not ($ledgerApp.identifierUris -contains "api://$ledgerAppId")) {
    Invoke-Graph PATCH "$Graph/applications/$($ledgerApp.id)" @{ identifierUris = @("api://$ledgerAppId") } | Out-Null
}

# Only people given a Warden role may sign in at all.
Invoke-Graph PATCH "$Graph/servicePrincipals/$($signInSp.id)" @{ appRoleAssignmentRequired = $true } | Out-Null

# Consent granted once for the whole directory, so nobody sees a consent screen.
$graphSp = (Invoke-Graph GET "$Graph/servicePrincipals?`$filter=appId eq '00000003-0000-0000-c000-000000000000'").value[0]
$grants = (Invoke-Graph GET "$Graph/oauth2PermissionGrants?`$filter=clientId eq '$($signInSp.id)'").value
foreach ($g in @(@{ resource = $graphSp.id; scope = "openid profile offline_access" }, @{ resource = $signInSp.id; scope = "access" })) {
    if (-not ($grants | Where-Object { $_.resourceId -eq $g.resource })) {
        Invoke-Graph POST "$Graph/oauth2PermissionGrants" @{
            clientId = $signInSp.id; consentType = "AllPrincipals"; resourceId = $g.resource; scope = $g.scope } | Out-Null
    }
}

# A secret for the console's server (Entra requires one of a web app); kept in Key Vault.
if (-not ($state.PSObject.Properties.Name -contains "signInClientSecret")) {
    Write-Host "  creating the console's client secret (valid one year)"
    $cred = Invoke-Graph POST "$Graph/applications/$($app.id)/addPassword" @{
        passwordCredential = @{ displayName = "warden-console"; endDateTime = (Get-Date).AddYears(1).ToUniversalTime().ToString("o") } }
    Ensure-State "signInClientSecret" $cred.secretText
    Save-State $state
}

# People and roles.
$app = Invoke-Graph GET "$Graph/applications/$($app.id)"
# Phase 11b-2: an outside system may hold the submitter role too (and only that one).
$submitterRole = $app.appRoles | Where-Object { $_.value -eq "submitter" }
if (-not ($submitterRole.allowedMemberTypes -contains "Application")) {
    Write-Host "  letting apps hold the submitter role"
    $roles = @($app.appRoles | ForEach-Object {
            [string[]]$types = if ($_.value -eq "submitter") { @("User", "Application") } else { @($_.allowedMemberTypes) }
            @{ id = $_.id; value = $_.value; displayName = $_.displayName; description = $_.description
               allowedMemberTypes = $types; isEnabled = $_.isEnabled } })
    Invoke-Graph PATCH "$Graph/applications/$($app.id)" @{ appRoles = $roles } | Out-Null
    $app = Invoke-Graph GET "$Graph/applications/$($app.id)"
}
$roleIds = @{}; foreach ($r in $app.appRoles) { $roleIds[$r.value] = $r.id }
$assigned = (Invoke-Graph GET "$Graph/servicePrincipals/$($signInSp.id)/appRoleAssignedTo").value
function Grant-Role($principalId, $role) {
    if (-not ($assigned | Where-Object { $_.principalId -eq $principalId -and $_.appRoleId -eq $roleIds[$role] })) {
        Invoke-Graph POST "$Graph/servicePrincipals/$($signInSp.id)/appRoleAssignedTo" @{
            principalId = $principalId; resourceId = $signInSp.id; appRoleId = $roleIds[$role] } | Out-Null
    }
}
foreach ($role in "submitter", "approver", "controller") { Grant-Role $me.id $role }

# Phase 11b-2: "Warden intake", standing in for an outside system that submits invoices through
# API Management. It signs in as itself (no person), with its own secret, and is a submitter only.
$intakeApp = Find-App "Warden intake"
if (-not $intakeApp) {
    Write-Host "  creating the Warden intake app registration"
    $intakeApp = Invoke-Graph POST "$Graph/applications" @{ displayName = "Warden intake"; signInAudience = "AzureADMyOrg" }
}
$intakeSp = Ensure-ServicePrincipal $intakeApp.appId
if (-not ($state.PSObject.Properties.Name -contains "intakeClientSecret")) {
    Write-Host "  creating the intake app's client secret (valid one year)"
    $cred = Invoke-Graph POST "$Graph/applications/$($intakeApp.id)/addPassword" @{
        passwordCredential = @{ displayName = "warden-intake"; endDateTime = (Get-Date).AddYears(1).ToUniversalTime().ToString("o") } }
    Ensure-State "intakeClientSecret" $cred.secretText
}
$state | Add-Member -NotePropertyName "intakeAppId" -NotePropertyValue $intakeApp.appId -Force
Save-State $state
Grant-Role $intakeSp.id "submitter"

Ensure-State "people" (New-Object PSObject)
$testPeople = @(
    @{ alias = "amira"; name = "Amira Approver"; roles = @("submitter", "approver") },
    @{ alias = "clerk"; name = "Clerk Submitter"; roles = @("submitter") }
)
foreach ($p in $testPeople) {
    $upn = "$($p.alias)@$domain"
    $user = (Invoke-Graph GET "$Graph/users?`$filter=userPrincipalName eq '$upn'").value | Select-Object -First 1
    if (-not $user) {
        $first = "Wd7!" + (New-Secret 12)
        Write-Host "  creating $upn"
        $user = Invoke-Graph POST "$Graph/users" @{
            accountEnabled = $true; displayName = $p.name; mailNickname = $p.alias; userPrincipalName = $upn
            passwordProfile = @{ password = $first; forceChangePasswordNextSignIn = $true } }
        $state.people | Add-Member -NotePropertyName $p.alias -NotePropertyValue @{ upn = $upn; firstPassword = $first } -Force
        Save-State $state
    }
    foreach ($role in $p.roles) { Grant-Role $user.id $role }
}
$state | Add-Member -NotePropertyName "signInAppId" -NotePropertyValue $signInAppId -Force
$state | Add-Member -NotePropertyName "ledgerAppId" -NotePropertyValue $ledgerAppId -Force
$state | Add-Member -NotePropertyName "location" -NotePropertyValue $Location -Force
$state | Add-Member -NotePropertyName "resourceGroup" -NotePropertyValue $ResourceGroup -Force
Save-State $state

# ------------------------------------------------------------------ Bicep
function Deploy-Bicep([bool]$apps, [string]$tag) {
    $params = @{
        '$schema'      = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        contentVersion = "1.0.0.0"
        parameters     = @{
            location               = @{ value = $Location }
            deployApps             = @{ value = $apps }
            imageTag               = @{ value = $tag }
            signInAppId            = @{ value = $signInAppId }
            ledgerAppId            = @{ value = $ledgerAppId }
            openaiApiKey           = @{ value = $dotenv["OPENAI_API_KEY"] }
            groqApiKey             = @{ value = $dotenv["GROQ_API_KEY"] }
            litellmMasterKey       = @{ value = $dotenv["LITELLM_MASTER_KEY"] }
            vendorLookupKey        = @{ value = $dotenv["VENDOR_LOOKUP_KEY"] }
            postgresAdminPassword  = @{ value = $state.pgAdminPassword }
            pgLedgerPassword       = @{ value = $dotenv["PGPASS_WARDEN_LEDGER"] }
            pgBackendPassword      = @{ value = $dotenv["PGPASS_WARDEN_BACKEND"] }
            pgControlPassword      = @{ value = $state.pgControlPassword }
            pgQualityPassword      = @{ value = $state.pgQualityPassword }
            signInClientSecret     = @{ value = $state.signInClientSecret }
            agentKeysJson          = @{ value = ($state.agentKeys | ConvertTo-Json -Compress) }
            entryKeyConsole        = @{ value = $state.entryKeyConsole }
            entryKeyGateway        = @{ value = $state.entryKeyGateway }
            apimPublisherEmail     = @{ value = $account.user.name }
        }
    }
    foreach ($pair in @(@("OPENING_CASH", "openingCash"), @("ROUTINE_AMOUNT_THRESHOLD", "routineAmountThreshold"),
                        @("CONFIDENCE_THRESHOLD", "confidenceThreshold"), @("SETTLEMENT_HOLD_SECONDS", "settlementHoldSeconds"),
                        @("LEDGER_CURRENCY", "ledgerCurrency"), @("MIN_PRIOR_PAYMENTS", "minPriorPayments"),
                        @("APPROVAL_SAMPLES", "approvalSamples"))) {
        if ($dotenv[$pair[0]]) { $params.parameters[$pair[1]] = @{ value = $dotenv[$pair[0]] } }
    }
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ("warden-params-" + [guid]::NewGuid() + ".json")
    try {
        [System.IO.File]::WriteAllText($file, ($params | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
        $name = "warden-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        return (Invoke-AzJson deployment group create -g $ResourceGroup -n $name -f (Join-Path $PSScriptRoot "main.bicep") `
                    -p "@$file" --query properties.outputs).PSObject.Properties |
            ForEach-Object -Begin { $o = @{} } -Process { $o[$_.Name] = $_.Value.value } -End { $o }
    } finally { Remove-Item $file -ErrorAction SilentlyContinue }
}

Step "Deploying the foundations (first time: about 10 minutes, mostly Postgres and API Management)"
Invoke-Az group create -n $ResourceGroup -l $Location --tags project=warden | Out-Null
$out = Deploy-Bicep $false "none"
$state | Add-Member -NotePropertyName "vaultName" -NotePropertyValue $out.vaultName -Force
Save-State $state

# The console's address is known once the environment exists: register it for sign-in.
$consoleUrl = $out.consoleUrl
Invoke-Graph PATCH "$Graph/applications/$($app.id)" @{
    identifierUris = @("api://$signInAppId")
    web            = @{ redirectUris = @("$consoleUrl/api/auth/callback", "$consoleUrl/")
                        implicitGrantSettings = @{ enableIdTokenIssuance = $false; enableAccessTokenIssuance = $false } }
    requiredResourceAccess = @(
        @{ resourceAppId = "00000003-0000-0000-c000-000000000000"; resourceAccess = @(
                @{ id = "37f7f235-527c-4136-accd-4a02d197296e"; type = "Scope" },   # openid
                @{ id = "14dad69e-099b-42c9-810b-d002981feec1"; type = "Scope" },   # profile
                @{ id = "7427e0e9-2fba-42fe-b0c0-848c9e6a8182"; type = "Scope" }) }, # offline_access
        @{ resourceAppId = $signInAppId; resourceAccess = @(
                @{ id = ($app.api.oauth2PermissionScopes | Where-Object { $_.value -eq "access" }).id; type = "Scope" }) })
} | Out-Null

# ------------------------------------------------------------------ images
$registry = $out.registryServer
if ($SkipImages) {
    if (-not $state.imageTag) { throw "No images pushed yet: run once without -SkipImages." }
    $tag = $state.imageTag
} else {
    Step "Building and pushing the images to $registry (first time: 10-20 minutes, mostly upload)"
    $tag = Get-Date -Format "yyyyMMddHHmm"
    Invoke-Az acr login -n $out.registryName | Out-Null
    $images = @(
        @{ name = "warden-ledger"; context = "warden-ledger" },
        @{ name = "warden-pii"; context = "warden-pii" },
        @{ name = "warden-ocr"; context = "warden-ocr" },
        @{ name = "warden-quality"; context = "warden-quality" },
        @{ name = "warden-data-loader"; context = "warden-data-loader" },
        @{ name = "warden-backend"; context = "warden-backend" },
        @{ name = "warden-gateway"; context = "infra/litellm" },
        @{ name = "warden-console"; context = "warden-console" },
        @{ name = "warden-db-init"; context = "infra/azure"; file = "infra/azure/images/db-init.Dockerfile" },
        @{ name = "warden-policy-base"; context = "warden-policy" },
        @{ name = "warden-policy"; context = "policies"; file = "infra/azure/images/policy.Dockerfile"; base = "warden-policy-base" },
        @{ name = "warden-control-base"; context = "warden-control" },
        @{ name = "warden-control"; context = "infra/litellm"; file = "infra/azure/images/control.Dockerfile"; base = "warden-control-base" }
    )
    foreach ($img in $images) {
        $ref = "$registry/$($img.name):$tag"
        Write-Host "  $($img.name)"
        $buildArgs = @("build", "--platform", "linux/amd64", "-t", $ref)
        if ($img.file) { $buildArgs += @("-f", $img.file) }
        if ($img.base) { $buildArgs += @("--build-arg", "BASE=$registry/$($img.base):$tag") }
        & docker @buildArgs $img.context
        if ($LASTEXITCODE -ne 0) { throw "building $($img.name) failed" }
        if ($img.name -notlike "*-base") {
            & docker push $ref
            if ($LASTEXITCODE -ne 0) { throw "pushing $($img.name) failed" }
        }
    }
    $state | Add-Member -NotePropertyName "imageTag" -NotePropertyValue $tag -Force
    Save-State $state
}

# ------------------------------------------------------------------ apps
Step "Deploying the apps (image $tag)"
$out = Deploy-Bicep $true $tag

# Starts a Container Apps job and waits for it; stops the deploy if it doesn't succeed.
function Run-Job([string]$job, [int]$minutes) {
    $execution = Invoke-Az containerapp job start -n $job -g $ResourceGroup --query name -o tsv
    $status = "Running"
    for ($i = 0; $i -lt ($minutes * 6) -and $status -in @("Running", "Processing", ""); $i++) {
        Start-Sleep -Seconds 10
        $status = Invoke-Az containerapp job execution show -n $job -g $ResourceGroup --job-execution-name $execution --query properties.status -o tsv
        if ($i % 3 -eq 2 -or $status -notin @("Running", "Processing", "")) { Write-Host "  $status" }
    }
    if ($status -ne "Succeeded") {
        throw "The $job job ended '$status'. Its log: Azure portal > $ResourceGroup > $job > Execution history > Console logs."
    }
}

Step "Creating the service databases (the warden-db-init job)"
Run-Job "warden-db-init" 10

# Phase 11c: real receipts and vendor history, once (or again with -LoadData).
if ($LoadData -or -not $state.dataLoaded) {
    Step "Loading receipts and vendor history (the warden-data-loader job: 5-15 minutes)"
    Run-Job "warden-data-loader" 30
    $state | Add-Member -NotePropertyName "dataLoaded" -NotePropertyValue (Get-Date -Format "yyyy-MM-dd HH:mm") -Force
    Save-State $state
}

# ------------------------------------------------------------------ done
Step "Warden is on Azure"
Write-Host "Console:  $consoleUrl"
Write-Host "API:      $($out.apimGatewayUrl)   (try it: .\infra\azure\try-api.ps1)"
Write-Host "Sign in with your own Microsoft account: you have submitter, approver and controller."
foreach ($p in $state.people.PSObject.Properties) {
    Write-Host ("Test person {0}: {1}   first password: {2}  (must be changed at first sign-in)" -f $p.Name, $p.Value.upn, $p.Value.firstPassword)
}
Write-Host ""
Write-Host "Every app sleeps after 5 idle minutes. The first page after a quiet spell takes 20-60 seconds."
Write-Host "Remove everything:  .\infra\azure\teardown.ps1"
