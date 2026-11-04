// Phase 11a: Warden's core payment path on Azure.
//
// Deployed twice by infra/azure/deploy.ps1. With deployApps=false: the foundations (logs,
// registry, Key Vault and its secrets, identities, Postgres, the Container Apps environment).
// The script then pushes the images. With deployApps=true: the same foundations (unchanged),
// plus the database job and the seven apps.
//
// Cost shape: every app scales to zero when idle, so an idle Warden costs only the registry
// (Basic) and Key Vault operations; Postgres B1ms with 32 GB is in the free account's 12-month
// allowance; Container Apps use is within its monthly free grant for occasional demos.

targetScope = 'resourceGroup'

@description('Region for everything. Central US by default (set by deploy.ps1).')
param location string = resourceGroup().location

@description('Short name used in every resource name.')
param prefix string = 'warden'

@description('false: foundations only. true: also the database job and the apps (images must be pushed).')
param deployApps bool = false

@description('Tag of the images in the registry, set by deploy.ps1 when it pushes them.')
param imageTag string = 'none'

@description('Entra ID app registration for people signing in (the console and the APIs).')
param signInAppId string

@description('Entra ID app registration the ledger accepts managed-identity tokens for.')
param ledgerAppId string

param tenantId string = tenant().tenantId
param postgresAdminLogin string = 'wardenadmin'

// Settings the local stack keeps in .env.
param openingCash string = '250000.00'
param routineAmountThreshold string = '5000'
param confidenceThreshold string = '0.75'
param settlementHoldSeconds string = '20'
param ledgerCurrency string = 'MYR'
param minPriorPayments string = '2'
param approvalSamples string = '3'
param agentTemperatures string = 'risk-scoring=0'

@secure()
param openaiApiKey string
@secure()
param groqApiKey string
@secure()
param litellmMasterKey string
@secure()
param vendorLookupKey string
@secure()
param postgresAdminPassword string
@secure()
param pgLedgerPassword string
@secure()
param pgBackendPassword string
@secure()
param pgControlPassword string
@secure()
param signInClientSecret string
@description('The per-agent gateway keys, as the JSON file the gateway and backend read.')
@secure()
param agentKeysJson string

var suffix = uniqueString(resourceGroup().id)
var tags = { project: 'warden', phase: '11a' }

// ---------------------------------------------------------------- logs
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    // A ceiling on ingestion, so a noisy app can't run up a bill: 0.15 GB a day stays
    // within the 5 GB a month Log Analytics gives each billing account free.
    workspaceCapping: { dailyQuotaGb: json('0.15') }
  }
}

// ---------------------------------------------------------------- registry
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // apps pull with a managed identity, never a password
  }
}

// ---------------------------------------------------------------- identities
// One identity the apps use to pull images and read their secrets; and one per workload
// that writes or reads the ledger, standing in for the SPIFFE IDs of phase 6b.
resource appsIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-apps'
  location: location
  tags: tags
}

resource backendIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-backend-id'
  location: location
  tags: tags
}

resource policyIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-policy-id'
  location: location
  tags: tags
}

var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var keyVaultSecretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, appsIdentity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
    principalId: appsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- secrets
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}-kv-${take(suffix, 8)}'
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true // access by Azure roles, not vault access policies
    softDeleteRetentionInDays: 7 // the minimum; teardown.ps1 purges it so a redeploy can reuse the name
  }
}

resource readSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, appsIdentity.id, keyVaultSecretsUserRole)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRole)
    principalId: appsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

var postgresHost = '${prefix}-pg-${suffix}.postgres.database.azure.com'
var secretValues = {
  'openai-api-key': openaiApiKey
  'groq-api-key': groqApiKey
  'litellm-master-key': litellmMasterKey
  'vendor-lookup-key': vendorLookupKey
  'pg-admin-password': postgresAdminPassword
  'pg-ledger-password': pgLedgerPassword
  'pg-backend-password': pgBackendPassword
  'pg-control-password': pgControlPassword
  'control-db-url': 'postgresql://warden_control:${pgControlPassword}@${postgresHost}:5432/warden_control?sslmode=require'
  'signin-client-secret': signInClientSecret
  'agent-keys-json': agentKeysJson
}

resource secrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for s in items(secretValues): {
  parent: vault
  name: s.key
  properties: { value: s.value }
}]

// ---------------------------------------------------------------- Postgres
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${prefix}-pg-${suffix}'
  location: location
  tags: tags
  // B1ms with 32 GB is the size the free account covers for 12 months (750 hours a month).
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32, autoGrow: 'Disabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    authConfig: { activeDirectoryAuth: 'Disabled', passwordAuth: 'Enabled' }
  }
}

// Only Azure-hosted clients may connect (the 0.0.0.0 rule), always over TLS (the server's
// default). A private network would also shut out other Azure tenants; see the README.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ---------------------------------------------------------------- Container Apps environment
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------- apps (second pass)
var registryServer = registry.properties.loginServer
var consoleUrl = 'https://warden-console.${env.properties.defaultDomain}'
var authority = '${environment().authentication.loginEndpoint}${tenantId}'
var userSignIn = [
  { name: 'OIDC_ISSUER', value: '${authority}/v2.0' }
  { name: 'OIDC_JWKS_URL', value: '${authority}/discovery/v2.0/keys' }
  { name: 'TOKEN_AUDIENCE', value: signInAppId }
  { name: 'ROLES_CLAIM', value: 'roles' }
]
var noTelemetry = [{ name: 'OTEL_SDK_DISABLED', value: 'true' }]
var agentKeysVolume = {
  name: 'agent-keys'
  storageType: 'Secret'
  secrets: [{ secretRef: 'agent-keys-json', path: 'agent-keys.json' }]
}

func vaultSecret(vaultUri string, name string, identityId string) object => {
  name: name
  keyVaultUrl: '${vaultUri}secrets/${name}'
  identity: identityId
}

// What each app is; nothing here depends on the deployment, so the loop can be planned up front.
var apps = [
  {
    name: 'warden-ledger'
    image: 'warden-ledger'
    port: 8000
    external: false
    cpu: '0.25'
    memory: '0.5Gi'
    workloadIdentity: ''
    secrets: ['pg-ledger-password', 'vendor-lookup-key']
    volumes: []
  }
  {
    name: 'warden-control'
    image: 'warden-control'
    port: 8000
    external: false
    cpu: '0.25'
    memory: '0.5Gi'
    workloadIdentity: ''
    secrets: ['control-db-url']
    volumes: []
  }
  {
    name: 'warden-policy'
    image: 'warden-policy'
    port: 8000
    external: false
    cpu: '0.25'
    memory: '0.5Gi'
    workloadIdentity: policyIdentity.id
    secrets: []
    volumes: []
  }
  {
    name: 'warden-pii'
    image: 'warden-pii'
    port: 8000
    external: false
    cpu: '0.5'
    memory: '1Gi'
    workloadIdentity: ''
    secrets: []
    volumes: []
  }
  {
    name: 'warden-gateway'
    image: 'warden-gateway'
    port: 4000
    external: false
    cpu: '0.5'
    memory: '1Gi'
    workloadIdentity: ''
    secrets: ['openai-api-key', 'groq-api-key', 'litellm-master-key', 'agent-keys-json']
    volumes: [agentKeysVolume]
  }
  {
    name: 'warden-backend'
    image: 'warden-backend'
    port: 8000
    external: false
    cpu: '0.5'
    memory: '1Gi'
    workloadIdentity: backendIdentity.id
    secrets: ['pg-backend-password', 'agent-keys-json']
    volumes: [agentKeysVolume]
  }
  {
    name: 'warden-console'
    image: 'warden-console'
    port: 3100
    external: true
    cpu: '0.25'
    memory: '0.5Gi'
    workloadIdentity: ''
    secrets: ['vendor-lookup-key', 'signin-client-secret']
    volumes: []
  }
]

// Each app's settings, in the same order as apps.
var appEnv = [
  concat(noTelemetry, [
    { name: 'POSTGRES_HOST', value: postgresHost }
    { name: 'PGPASS_WARDEN_LEDGER', secretRef: 'pg-ledger-password' }
    { name: 'VENDOR_LOOKUP_KEY', secretRef: 'vendor-lookup-key' }
    { name: 'OPENING_CASH', value: openingCash }
    { name: 'LEDGER_AUTH', value: 'entra' }
    { name: 'ENTRA_TENANT_ID', value: tenantId }
    { name: 'LEDGER_AUDIENCE', value: ledgerAppId }
    { name: 'LEDGER_WRITERS', value: backendIdentity.properties.clientId }
    { name: 'LEDGER_READERS', value: policyIdentity.properties.clientId }
  ])
  concat(noTelemetry, userSignIn, [
    { name: 'CONTROL_DB', secretRef: 'control-db-url' }
    { name: 'CANARY_VENDORS', value: 'CANARY-DO-NOT-PAY' }
  ])
  concat(noTelemetry, [
    { name: 'CONTROL_URL', value: 'http://warden-control' }
    { name: 'LEDGER_URL', value: 'http://warden-ledger' }
    { name: 'LEDGER_AUTH', value: 'entra' }
    { name: 'LEDGER_AUDIENCE', value: 'api://${ledgerAppId}' }
    { name: 'AZURE_CLIENT_ID', value: policyIdentity.properties.clientId }
  ])
  []
  [
    { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
    { name: 'GROQ_API_KEY', secretRef: 'groq-api-key' }
    { name: 'LITELLM_MASTER_KEY', secretRef: 'litellm-master-key' }
    { name: 'CONTROL_URL', value: 'http://warden-control' }
    { name: 'WARDEN_AGENT_KEYS_FILE', value: '/run/agent-keys/agent-keys.json' }
  ]
  concat(noTelemetry, userSignIn, [
    { name: 'POSTGRES_HOST', value: postgresHost }
    { name: 'PGPASS_WARDEN_BACKEND', secretRef: 'pg-backend-password' }
    { name: 'LITELLM_URL', value: 'http://warden-gateway' }
    { name: 'AGENT_KEYS_FILE', value: '/run/agent-keys/agent-keys.json' }
    { name: 'LEDGER_URL', value: 'http://warden-ledger' }
    { name: 'POLICY_URL', value: 'http://warden-policy' }
    { name: 'PII_URL', value: 'http://warden-pii' }
    { name: 'CONTROL_URL', value: 'http://warden-control' }
    { name: 'LEDGER_AUTH', value: 'entra' }
    { name: 'LEDGER_AUDIENCE', value: 'api://${ledgerAppId}' }
    { name: 'AZURE_CLIENT_ID', value: backendIdentity.properties.clientId }
    { name: 'WAKE_DEPENDENCIES', value: 'true' }
    { name: 'ROUTINE_AMOUNT_THRESHOLD', value: routineAmountThreshold }
    { name: 'CONFIDENCE_THRESHOLD', value: confidenceThreshold }
    { name: 'SETTLEMENT_HOLD_SECONDS', value: settlementHoldSeconds }
    { name: 'LEDGER_CURRENCY', value: ledgerCurrency }
    { name: 'MIN_PRIOR_PAYMENTS', value: minPriorPayments }
    { name: 'APPROVAL_SAMPLES', value: approvalSamples }
    { name: 'AGENT_TEMPERATURES', value: agentTemperatures }
  ])
  [
    { name: 'HOSTNAME', value: '0.0.0.0' } // Next.js listens on every interface, whatever the platform sets
    { name: 'BACKEND_URL', value: 'http://warden-backend' }
    { name: 'LEDGER_URL', value: 'http://warden-ledger' }
    { name: 'CONTROL_URL', value: 'http://warden-control' }
    { name: 'CONSOLE_URL', value: consoleUrl }
    { name: 'VENDOR_LOOKUP_KEY', secretRef: 'vendor-lookup-key' }
    { name: 'OIDC_ISSUER', value: '${authority}/v2.0' }
    { name: 'OIDC_AUTHORIZE_URL', value: '${authority}/oauth2/v2.0/authorize' }
    { name: 'OIDC_TOKEN_URL', value: '${authority}/oauth2/v2.0/token' }
    { name: 'OIDC_JWKS_URL', value: '${authority}/discovery/v2.0/keys' }
    { name: 'OIDC_LOGOUT_URL', value: '${authority}/oauth2/v2.0/logout' }
    { name: 'OIDC_CLIENT_ID', value: signInAppId }
    { name: 'OIDC_CLIENT_SECRET', secretRef: 'signin-client-secret' }
    { name: 'OIDC_SCOPE', value: 'openid profile offline_access api://${signInAppId}/access' }
    { name: 'OIDC_AUDIENCE', value: signInAppId }
    { name: 'OIDC_ROLES_CLAIM', value: 'roles' }
  ]
]

resource containerApps 'Microsoft.App/containerApps@2024-03-01' = [for (app, i) in apps: if (deployApps) {
  name: app.name
  location: location
  tags: tags
  dependsOn: [pull, readSecrets, secrets]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: union({ '${appsIdentity.id}': {} }, empty(app.workloadIdentity) ? {} : { '${app.workloadIdentity}': {} })
  }
  properties: {
    environmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: app.external
        targetPort: app.port
        transport: 'http'
        // Inside the environment the apps call each other over plain HTTP by name
        // (http://warden-ledger); only the console is reachable from outside, over HTTPS.
        allowInsecure: !app.external
      }
      registries: [{ server: registryServer, identity: appsIdentity.id }]
      secrets: [for s in app.secrets: vaultSecret(vault.properties.vaultUri, s, appsIdentity.id)]
    }
    template: {
      containers: [
        {
          name: app.name
          image: '${registryServer}/${app.image}:${imageTag}'
          resources: { cpu: json(app.cpu), memory: app.memory }
          env: appEnv[i]
          volumeMounts: [for v in app.volumes: { volumeName: v.name, mountPath: '/run/${v.name}' }]
        }
      ]
      volumes: app.volumes
      // Scale to zero after five idle minutes; one replica at most (the ledger and control
      // plane are single writers, and it caps the cost).
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}]

// The job that creates the service databases; deploy.ps1 starts it after each deploy.
resource dbInit 'Microsoft.App/jobs@2024-03-01' = if (deployApps) {
  name: 'warden-db-init'
  location: location
  tags: tags
  dependsOn: [pull, readSecrets, secrets, allowAzure]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${appsIdentity.id}': {} }
  }
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 300
      replicaRetryLimit: 1
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registryServer, identity: appsIdentity.id }]
      secrets: [for s in ['pg-admin-password', 'pg-ledger-password', 'pg-backend-password', 'pg-control-password']: vaultSecret(vault.properties.vaultUri, s, appsIdentity.id)]
    }
    template: {
      containers: [
        {
          name: 'db-init'
          image: '${registryServer}/warden-db-init:${imageTag}'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'PGHOST', value: postgresHost }
            { name: 'PGUSER', value: postgresAdminLogin }
            { name: 'PGPASSWORD', secretRef: 'pg-admin-password' }
            { name: 'PGSSLMODE', value: 'require' }
            { name: 'PGPASS_WARDEN_LEDGER', secretRef: 'pg-ledger-password' }
            { name: 'PGPASS_WARDEN_BACKEND', secretRef: 'pg-backend-password' }
            { name: 'PGPASS_WARDEN_CONTROL', secretRef: 'pg-control-password' }
          ]
        }
      ]
    }
  }
}

output registryName string = registry.name
output registryServer string = registryServer
output vaultName string = vault.name
output postgresHost string = postgresHost
output environmentDomain string = env.properties.defaultDomain
output consoleUrl string = consoleUrl
output backendIdentityClientId string = backendIdentity.properties.clientId
output policyIdentityClientId string = policyIdentity.properties.clientId
