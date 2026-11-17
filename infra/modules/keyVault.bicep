// Key Vault holding the external content storage account key, read by the web app
// through a user-assigned identity (Key Vault reference used by the storage mount).

@description('Azure region.')
param location string

@description('Key Vault name (3-24 chars, globally unique).')
param keyVaultName string

@description('User-assigned identity name for the web app.')
param identityName string

@description('Whether external content storage is configured (writes the secret).')
param contentStorageConfigured bool

@description('Access key of the external content storage account.')
@secure()
param contentStorageAccountKey string

param tags object = {}

var contentKeySecretName = 'content-storage-key'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

// Access policies (not RBAC): required by App Service storage mounts with Key Vault,
// and manageable by a deploy identity that only has Contributor.
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: false
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: identity.properties.principalId
        permissions: {
          secrets: [
            'get'
          ]
        }
      }
    ]
  }
}

resource contentKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (contentStorageConfigured) {
  parent: vault
  name: contentKeySecretName
  properties: {
    value: contentStorageAccountKey
    contentType: 'Azure Storage account key (content library)'
  }
}

output identityId string = identity.id
output keyVaultName string = vault.name
// Versioned so that a new key changes the app setting, which restarts the app and re-resolves it
output contentKeyReference string = !contentStorageConfigured ? '' : '@Microsoft.KeyVault(SecretUri=${contentKeySecret!.properties.secretUriWithVersion})'
