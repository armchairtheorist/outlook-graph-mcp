// Outlook Graph MCP server on Azure Container Apps.
// Deploys: Log Analytics, Container Apps environment, Azure Files share (persistent OAuth state),
// Key Vault (Graph refresh token + secrets), Container Registry, user-assigned identity, Container App.
//
// Deploy with ./deploy.sh (resource-group scope).

targetScope = 'resourceGroup'

@description('Short name used as a prefix for all resources (lowercase letters/numbers).')
@minLength(3)
@maxLength(12)
param name string = 'graphmcp'

param location string = resourceGroup().location

@description('Entra application (client) ID of the app registration.')
param entraClientId string

@secure()
@description('Entra client secret. Stored in Key Vault; never in the app definition.')
param entraClientSecret string

@secure()
@description('Random 32+ byte string used to sign the tokens the server issues to Claude.')
param jwtSigningKey string

@secure()
@description('Random string Graph echoes back in webhook notifications.')
param webhookClientState string

@description('Graph resources to subscribe to for change notifications. Empty disables webhooks.')
param webhookResources string = '/me/mailFolders(\'inbox\')/messages,/me/events'

@description('Windows time-zone name used for calendar output.')
param timeZone string = 'Singapore Standard Time'

@description('Container image to run. deploy.sh builds and pushes this; the first run uses a placeholder.')
param image string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Object ID of the person deploying, granted Key Vault Secrets Officer so seed_token.py can write the refresh token.')
param deployerObjectId string

var suffix = uniqueString(resourceGroup().id)
var acrName = toLower('${name}acr${suffix}')
var kvName = toLower('${name}kv${take(suffix, 8)}')
var storageName = toLower('${name}st${take(suffix, 10)}')

// ---------------------------------------------------------------- identity
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-id'
  location: location
}

// ---------------------------------------------------------------- logging
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------- registry
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// AcrPull for the app identity
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- key vault
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

// Key Vault Secrets Officer (read+write) for the app: it must rotate the refresh token.
resource kvOfficerApp 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, identity.id, 'kv-officer')
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Same role for the human deployer so scripts/seed_token.py can write the first token.
resource kvOfficerHuman 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, deployerObjectId, 'kv-officer')
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    principalId: deployerObjectId
    principalType: 'User'
  }
}

resource secretClient 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'entra-client-secret'
  properties: { value: entraClientSecret }
}
resource secretJwt 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'jwt-signing-key'
  properties: { value: jwtSigningKey }
}
resource secretWebhook 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'webhook-client-state'
  properties: { value: webhookClientState }
}

// ---------------------------------------------------------------- storage (FastMCP OAuth state)
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false }
}
resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'fastmcp-data'
  properties: { shareQuota: 1 }
}

// ---------------------------------------------------------------- container apps env
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
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

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'fastmcp-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
}

// ---------------------------------------------------------------- the app
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: [
        { name: 'entra-client-secret', keyVaultUrl: secretClient.properties.secretUri, identity: identity.id }
        { name: 'jwt-signing-key', keyVaultUrl: secretJwt.properties.secretUri, identity: identity.id }
        { name: 'webhook-client-state', keyVaultUrl: secretWebhook.properties.secretUri, identity: identity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'server'
          image: image
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'ENTRA_CLIENT_ID', value: entraClientId }
            { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
            { name: 'ENTRA_TENANT', value: 'consumers' }
            { name: 'JWT_SIGNING_KEY', secretRef: 'jwt-signing-key' }
            { name: 'WEBHOOK_CLIENT_STATE', secretRef: 'webhook-client-state' }
            { name: 'WEBHOOK_RESOURCES', value: webhookResources }
            { name: 'TOKEN_STORE', value: 'keyvault' }
            { name: 'KEYVAULT_URL', value: kv.properties.vaultUri }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId } // tells DefaultAzureCredential which identity
            { name: 'BASE_URL', value: 'https://${name}.${env.properties.defaultDomain}' }
            { name: 'TIME_ZONE', value: timeZone }
            { name: 'FASTMCP_HOME', value: '/data' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
          volumeMounts: [ { volumeName: 'data', mountPath: '/data' } ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      volumes: [ { name: 'data', storageType: 'AzureFile', storageName: envStorage.name } ]
      scale: { minReplicas: 1, maxReplicas: 1 } // single instance: OAuth state is on disk, webhooks are in memory
    }
  }
  dependsOn: [ acrPull, kvOfficerApp ]
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output keyVaultUrl string = kv.properties.vaultUri
output containerAppName string = app.name
output identityClientId string = identity.properties.clientId
