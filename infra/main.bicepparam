using 'main.bicep'

// Non-secret values. Secrets are read from environment variables at deploy time
// (set by the GitHub workflow, or export them locally before `az deployment group create`).
param location = 'francecentral'
param namePrefix = 'cwa'
param appServiceSku = 'B1'
param timezone = 'Europe/Paris'

param containerImage = readEnvironmentVariable('CWA_CONTAINER_IMAGE', 'ghcr.io/lvovan/calibre-web-automated-azure:latest')

param contentStorageAccountName = readEnvironmentVariable('CONTENT_STORAGE_ACCOUNT_NAME', '')
param contentStorageAccountKey = readEnvironmentVariable('CONTENT_STORAGE_ACCOUNT_KEY', '')
param libraryShareName = readEnvironmentVariable('LIBRARY_SHARE_NAME', 'library')

param customDomainName = readEnvironmentVariable('CUSTOM_DOMAIN_NAME', '')
param hardcoverToken = readEnvironmentVariable('HARDCOVER_TOKEN', '')

param oneDriveClientId = readEnvironmentVariable('ONEDRIVE_CLIENT_ID', '')
param oneDriveClientSecret = readEnvironmentVariable('ONEDRIVE_CLIENT_SECRET', '')
