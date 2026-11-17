// Calibre-Web Automated on Azure App Service (Web App for Containers).
// See infra/README.md for prerequisites and the deployment flow.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short prefix for resource names (lowercase letters/digits).')
@minLength(2)
@maxLength(10)
param namePrefix string = 'cwa'

@description('App Service plan SKU. B1 is the cheapest tier with Always On; F1/D1 are not supported.')
@allowed([
  'B1'
  'B2'
  'B3'
  'P0v3'
  'P1v3'
])
param appServiceSku string = 'B1'

@description('Container image (publicly pullable), e.g. ghcr.io/owner/calibre-web-automated-azure:<tag>.')
param containerImage string

@description('IANA timezone used by CWA schedules.')
param timezone string = 'Europe/Paris'

@description('Existing external storage account holding the Calibre library Azure Files share. Leave empty to deploy without the library mount; the container then refuses to start until it is configured.')
param contentStorageAccountName string = ''

@description('Access key of the external content storage account.')
@secure()
param contentStorageAccountKey string = ''

@description('Existing Azure Files share mounted at /calibre-library.')
param libraryShareName string = 'library'

@description('Optional custom hostname (e.g. books.example.com). Leave empty on the first deployment; set it once DNS records exist.')
param customDomainName string = ''

@description('Optional Hardcover API token.')
@secure()
param hardcoverToken string = ''

@description('Log Analytics daily ingestion cap in GB (string to allow decimals).')
param logDailyQuotaGb string = '0.1'

@description('Log retention in days (30 is free).')
param logRetentionInDays int = 30

param tags object = {
  workload: 'calibre-web-automated'
}

var suffix = uniqueString(resourceGroup().id)
var baseName = '${namePrefix}-${suffix}'

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    baseName: baseName
    dailyQuotaGb: logDailyQuotaGb
    retentionInDays: logRetentionInDays
    tags: tags
  }
}

module opsStorage 'modules/opsStorage.bicep' = {
  name: 'opsStorage'
  params: {
    location: location
    storageAccountName: take('st${namePrefix}${suffix}', 24)
    configShareName: 'config'
    configShareQuotaGiB: 5
    shareSoftDeleteDays: 7
    tags: tags
  }
}

module appService 'modules/appService.bicep' = {
  name: 'appService'
  params: {
    location: location
    baseName: baseName
    webAppName: 'app-${baseName}'
    appServiceSku: appServiceSku
    containerImage: containerImage
    timezone: timezone
    contentStorageAccountName: contentStorageAccountName
    contentStorageAccountKey: contentStorageAccountKey
    libraryShareName: libraryShareName
    opsStorageAccountName: opsStorage.outputs.storageAccountName
    configShareName: opsStorage.outputs.configShareName
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    hardcoverToken: hardcoverToken
    tags: tags
  }
}

module customDomain 'modules/customDomain.bicep' = if (!empty(customDomainName)) {
  name: 'customDomain'
  params: {
    location: location
    webAppName: appService.outputs.webAppName
    planId: appService.outputs.planId
    customDomainName: customDomainName
    tags: tags
  }
}

output webAppName string = appService.outputs.webAppName
output defaultUrl string = 'https://${appService.outputs.defaultHostName}'
output customUrl string = empty(customDomainName) ? '' : 'https://${customDomainName}'
@description('Value for the asuid.<customDomainName> TXT record.')
output customDomainVerificationId string = appService.outputs.customDomainVerificationId
output opsStorageAccountName string = opsStorage.outputs.storageAccountName
