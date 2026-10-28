/*  costwatchdog - the scheduled job itself (resource group scope).
 *
 *  Consumption-plan Python function with a user-assigned managed identity. Nothing here
 *  stores an Azure credential: the function reads inventory, cost and telemetry with its
 *  identity, and writes its reports to the blob container below.
 */

@description('Short name used to build every resource name.')
@minLength(3)
@maxLength(18)
param baseName string

@description('Azure region for all resources.')
param location string

@description('Subscriptions the watchdog audits.')
param watchedSubscriptionIds array

@description('true lets the scheduled run execute actions.')
param applyActions bool

@description('true allows deletion of orphaned resources.')
param allowOrphanDeletion bool

@description('true allows automatic actions on production resources.')
param allowProdActions bool

@description('Days of cost history each run analyses.')
param lookbackDays int

/* Storage names must be globally unique, 3-24 lowercase alphanumerics. The unique suffix
 * may be truncated by `take`, which is why baseName is also part of the name. */
var storageAccountName = take(toLower('st${baseName}${uniqueString(resourceGroup().id)}'), 24)
var functionAppName = toLower('func-${baseName}-${uniqueString(resourceGroup().id)}')
var reportContainerName = 'reports'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${baseName}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${baseName}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    RetentionInDays: 30
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    accessTier: 'Hot'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource reportContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: reportContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${baseName}'
  location: location
}

resource plan 'Microsoft.Web/serverfarms@2022-09-01' = {
  name: 'asp-${baseName}'
  location: location
  kind: 'linux'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2022-09-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      alwaysOn: false
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${listKeys(storage.id, '2023-05-01').keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'WATCHDOG_SUBSCRIPTIONS'
          value: join(watchedSubscriptionIds, ',')
        }
        {
          name: 'WATCHDOG_DAYS'
          value: string(lookbackDays)
        }
        {
          name: 'WATCHDOG_APPLY'
          value: applyActions ? 'true' : 'false'
        }
        {
          name: 'WATCHDOG_ALLOW_ORPHANS'
          value: allowOrphanDeletion ? 'true' : 'false'
        }
        {
          name: 'WATCHDOG_ALLOW_PROD'
          value: allowProdActions ? 'true' : 'false'
        }
        {
          name: 'WATCHDOG_REPORT_CONTAINER'
          value: reportContainerName
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: identity.properties.clientId
        }
      ]
    }
  }
}

output principalId string = identity.properties.principalId
output functionAppName string = functionApp.name
output functionAppPrincipalId string = functionApp.identity.principalId
output reportUrl string = 'https://${storage.name}.blob.core.windows.net/${reportContainerName}'
