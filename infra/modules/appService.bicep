// Linux App Service plan + Web App for Containers running CWA, with Azure Files (SMB) mounts.

@description('Azure region.')
param location string

@description('Base name used for resource names.')
param baseName string

@description('Globally unique web app name.')
param webAppName string

@description('App Service plan SKU. B1 is the cheapest tier with Always On (required by CWA background services).')
param appServiceSku string

@description('Container image, e.g. ghcr.io/owner/repo:tag (must be publicly pullable).')
param containerImage string

@description('IANA timezone for CWA schedules.')
param timezone string

@description('Existing external storage account holding the Calibre library share.')
param contentStorageAccountName string

@description('Access key of the external content storage account.')
@secure()
param contentStorageAccountKey string

@description('Azure Files share (in the external account) mounted at /calibre-library.')
param libraryShareName string

@description('Log Analytics workspace receiving App Service diagnostic logs.')
param logAnalyticsWorkspaceId string

@description('Application Insights connection string.')
param appInsightsConnectionString string

@description('Optional Hardcover API token for the Hardcover metadata provider.')
@secure()
param hardcoverToken string = ''

param tags object = {}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'asp-${baseName}'
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: appServiceSku
    capacity: 1 // SQLite databases: never scale out
  }
  properties: {
    reserved: true
  }
}

var baseAppSettings = [
  { name: 'WEBSITES_PORT', value: '8083' }
  // Persistent /home storage holds /config (app.db, cwa.db, logs); see init-cwa-persistent-config
  { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'true' }
  // First start initializes databases on network storage; allow generous startup time
  { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '900' }
  { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://ghcr.io' }
  { name: 'PUID', value: '1000' }
  { name: 'PGID', value: '1000' }
  { name: 'TZ', value: timezone }
  // /home and Azure Files are network storage: disables WAL + chown and uses polling watchers
  { name: 'NETWORK_SHARE_MODE', value: 'true' }
  // App Service front end is the only proxy in front of the container
  { name: 'TRUSTED_PROXY_COUNT', value: '1' }
  // Keep ingest staging scratch I/O on local container disk instead of persistent /config
  { name: 'CWA_TMP_CONVERSION_DIR', value: '/tmp/cwa_conversion' }
  // Don't archive processed originals to /config/processed_books (new installs only)
  { name: 'CWA_DISABLE_PROCESSED_BACKUPS', value: 'true' }
  // Import uploads in their original format, without auto-conversion (new installs only)
  { name: 'CWA_DISABLE_AUTO_CONVERT', value: 'true' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
]

var optionalAppSettings = empty(hardcoverToken) ? [] : [
  { name: 'HARDCOVER_TOKEN', value: hardcoverToken }
]

// Omitted until the external content storage is configured
var libraryMount = empty(contentStorageAccountName) ? {} : {
  library: {
    type: 'AzureFiles'
    protocol: 'Smb'
    accountName: contentStorageAccountName
    shareName: libraryShareName
    mountPath: '/calibre-library'
    accessKey: contentStorageAccountKey
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  tags: tags
  kind: 'app,linux,container'
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      linuxFxVersion: 'DOCKER|${containerImage}'
      alwaysOn: true
      numberOfWorkers: 1
      healthCheckPath: '/health'
      http20Enabled: true
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat(baseAppSettings, optionalAppSettings)
      azureStorageAccounts: libraryMount
    }
  }
}

// Deployments go through ARM; disable basic-auth publishing endpoints
resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: site
  name: 'scm'
  properties: {
    allow: false
  }
}

// Container stdout/stderr to the local file system, size and age capped
resource siteLogs 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: site
  name: 'logs'
  properties: {
    applicationLogs: {
      fileSystem: {
        level: 'Warning'
      }
    }
    httpLogs: {
      fileSystem: {
        enabled: true
        retentionInDays: 7
        retentionInMb: 35
      }
    }
    detailedErrorMessages: {
      enabled: false
    }
    failedRequestsTracing: {
      enabled: false
    }
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: site
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'AppServiceConsoleLogs', enabled: true }
      { category: 'AppServiceHTTPLogs', enabled: true }
      { category: 'AppServicePlatformLogs', enabled: true }
    ]
  }
}

output webAppName string = site.name
output defaultHostName string = site.properties.defaultHostName
output customDomainVerificationId string = site.properties.customDomainVerificationId
output planId string = plan.id
