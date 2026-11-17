// Storage account in the app's resource group holding the /config Azure Files share
// (app.db, cwa.db, logs, Calibre plugins).

@description('Azure region.')
param location string

@description('Globally unique storage account name (3-24 lowercase letters/digits).')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Name of the Azure Files share mounted at /config.')
param configShareName string

@description('Quota of the config share in GiB.')
param configShareQuotaGiB int

@description('Soft delete retention for shares, in days.')
@minValue(1)
@maxValue(365)
param shareSoftDeleteDays int

param tags object = {}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // App Service Azure Files mounts authenticate with the account key
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Enabled'
  }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: {
      enabled: true
      days: shareSoftDeleteDays
    }
  }
}

resource configShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: configShareName
  properties: {
    shareQuota: configShareQuotaGiB
    accessTier: 'TransactionOptimized'
    enabledProtocols: 'SMB'
  }
}

output storageAccountName string = storage.name
output configShareName string = configShare.name
