/*  costwatchdog - subscription-scope deployment
 *
 *  Creates:
 *    - a resource group for the watchdog itself
 *    - the Function app stack (see functionApp.bicep): storage, plan, Log Analytics,
 *      Application Insights, user-assigned managed identity, function app
 *    - least-privilege role assignments for that identity *on the audited subscription*:
 *        Reader                    - inventory + telemetry
 *        Cost Management Reader    - cost query API
 *        Virtual Machine Contributor - deallocate VMs and VMSS (not delete them)
 *
 *  Deploy with:
 *    az deployment sub create --location eastus --template-file infra/main.bicep \
 *      --parameters baseName=costwatchdog
 *
 *  Role assignments need Owner or User Access Administrator on the subscription, which is
 *  more than the identity itself ever gets. Note that assignments are created in the
 *  *deployment* subscription: if you audit several subscriptions, deploy once per
 *  subscription (or move to a management-group-scoped deployment).
 */

targetScope = 'subscription'

@description('Short name used to build every resource name in this deployment.')
@minLength(3)
@maxLength(18)
param baseName string = 'costwatchdog'

@description('Azure region for all resources.')
param location string = deployment().location

@description('Subscriptions the watchdog audits. Used for its configuration, not for RBAC scope.')
param watchedSubscriptionIds array = [subscription().subscriptionId]

@description('true lets the scheduled run execute actions. Leave false to publish reports only.')
param applyActions bool = false

@description('true allows deletion of orphaned disks, snapshots, public IPs and empty plans.')
param allowOrphanDeletion bool = false

@description('true allows automatic actions on resources tagged environment=prod.')
param allowProdActions bool = false

@description('Days of cost history each run analyses.')
@minValue(1)
@maxValue(90)
param lookbackDays int = 30

/*  Built-in role definition ids. Names are documented so a reviewer can check them:
 *    acdd72a7-3385-48ef-bd42-f606fba81ae7  Reader
 *    72fafb9e-0641-4937-9268-a91bfd8191a3  Cost Management Reader
 *    9980e02c-c2be-4d73-94e8-173b1dc7cf3c  Virtual Machine Contributor
 */
param readerRoleDefinitionId string = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
param costManagementReaderRoleDefinitionId string = '72fafb9e-0641-4937-9268-a91bfd8191a3'
param vmContributorRoleDefinitionId string = '9980e02c-c2be-4d73-94e8-173b1dc7cf3c'

var resourceGroupName = 'rg-${baseName}'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'created-by': 'costwatchdog'
  }
}

module app 'functionApp.bicep' = {
  name: '${baseName}-app'
  scope: resourceGroup
  params: {
    baseName: baseName
    location: location
    watchedSubscriptionIds: watchedSubscriptionIds
    applyActions: applyActions
    allowOrphanDeletion: allowOrphanDeletion
    allowProdActions: allowProdActions
    lookbackDays: lookbackDays
  }
}

// --- RBAC for the watchdog identity on the audited subscription -------------

resource readerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, resourceGroupName, 'reader')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleDefinitionId)
    principalId: app.outputs.principalId
    principalType: 'ServicePrincipal'
    description: 'costwatchdog: read inventory, resource properties and telemetry'
  }
}

resource costReaderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, resourceGroupName, 'cost-reader')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      costManagementReaderRoleDefinitionId
    )
    principalId: app.outputs.principalId
    principalType: 'ServicePrincipal'
    description: 'costwatchdog: read cost data and exports'
  }
}

resource vmContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, resourceGroupName, 'vm-contributor')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      vmContributorRoleDefinitionId
    )
    principalId: app.outputs.principalId
    principalType: 'ServicePrincipal'
    description: 'costwatchdog: deallocate virtual machines and scale sets (cannot delete them)'
  }
}

output resourceGroupName string = resourceGroup.name
output functionAppName string = app.outputs.functionAppName
output principalId string = app.outputs.principalId
output reportUrl string = app.outputs.reportUrl
