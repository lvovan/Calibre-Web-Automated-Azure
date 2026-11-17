<#
.SYNOPSIS
  One-time Azure + GitHub setup for deploying Calibre-Web Automated to Azure App Service.

.DESCRIPTION
  Idempotent. Creates (or reuses):
    - the resource group
    - an Entra ID app registration + service principal for GitHub Actions
    - a federated credential trusting pushes/dispatches on the given branch (OIDC, no secrets)
    - a Contributor role assignment scoped to the resource group
  Optionally (when the GitHub CLI is installed and -SetGitHubConfig is passed) sets the GitHub
  secrets/variables used by .github/workflows/azure-deploy.yml, reading the content storage
  account key with your own Azure credentials.

.EXAMPLE
  ./infra/bootstrap.ps1 -GitHubRepo lvovan/Calibre-Web-Automated-Azure `
    -ContentStorageAccountName mybooks -ContentStorageResourceGroup rg-storage -SetGitHubConfig
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string] $GitHubRepo,
  [string] $ResourceGroup = 'rg-cwa',
  [string] $Location = 'francecentral',
  [string] $Branch = 'main',
  [string] $AppRegistrationName = 'github-cwa-deploy',
  [string] $SubscriptionId,
  [string] $ContentStorageAccountName,
  [string] $ContentStorageResourceGroup,
  [string] $LibraryShareName = 'library',
  [string] $CustomDomainName = '',
  [switch] $SetGitHubConfig
)

$ErrorActionPreference = 'Stop'

function Invoke-Az { $out = & az @args; if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }; $out }

if ($SubscriptionId) { Invoke-Az account set --subscription $SubscriptionId | Out-Null }
$account = Invoke-Az account show -o json | ConvertFrom-Json
$SubscriptionId = $account.id
$TenantId = $account.tenantId
Write-Host "Subscription: $($account.name) ($SubscriptionId)"

Write-Host "Resource group $ResourceGroup ($Location)..."
$rgId = Invoke-Az group create --name $ResourceGroup --location $Location --query id -o tsv

Write-Host "App registration $AppRegistrationName..."
$appId = Invoke-Az ad app list --display-name $AppRegistrationName --query '[0].appId' -o tsv
if (-not $appId) {
  $appId = Invoke-Az ad app create --display-name $AppRegistrationName --query appId -o tsv
}
$spId = Invoke-Az ad sp list --filter "appId eq '$appId'" --query '[0].id' -o tsv
if (-not $spId) {
  $spId = Invoke-Az ad sp create --id $appId --query id -o tsv
}

$credName = "github-$($GitHubRepo -replace '[^A-Za-z0-9-]', '-')-$Branch"
$subject = "repo:${GitHubRepo}:ref:refs/heads/$Branch"
$existing = Invoke-Az ad app federated-credential list --id $appId --query "[?subject=='$subject'].name" -o tsv
if (-not $existing) {
  Write-Host "Federated credential for $subject..."
  $tmp = New-TemporaryFile
  @{
    name      = $credName.Substring(0, [Math]::Min(120, $credName.Length))
    issuer    = 'https://token.actions.githubusercontent.com'
    subject   = $subject
    audiences = @('api://AzureADTokenExchange')
  } | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
  try { Invoke-Az ad app federated-credential create --id $appId --parameters "@$tmp" | Out-Null } finally { Remove-Item $tmp }
} else {
  Write-Host "Federated credential for $subject already exists."
}

Write-Host "Contributor role on $ResourceGroup..."
$hasRole = Invoke-Az role assignment list --assignee $spId --scope $rgId --role Contributor --query '[0].id' -o tsv
if (-not $hasRole) {
  # New service principals can take a few seconds to replicate
  for ($i = 1; $i -le 6; $i++) {
    & az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal `
      --role Contributor --scope $rgId -o none 2>$null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 10
  }
  if ($LASTEXITCODE -ne 0) { throw 'Role assignment failed' }
}

$secrets = [ordered]@{
  AZURE_CLIENT_ID       = $appId
  AZURE_TENANT_ID       = $TenantId
  AZURE_SUBSCRIPTION_ID = $SubscriptionId
}
$variables = [ordered]@{
  AZURE_RESOURCE_GROUP         = $ResourceGroup
  CONTENT_STORAGE_ACCOUNT_NAME = $ContentStorageAccountName
  LIBRARY_SHARE_NAME           = $LibraryShareName
  CUSTOM_DOMAIN_NAME           = $CustomDomainName
}

$contentKey = $null
if ($ContentStorageAccountName -and $ContentStorageResourceGroup) {
  Write-Host "Checking share '$LibraryShareName' in $ContentStorageAccountName..."
  & az storage share-rm show --storage-account $ContentStorageAccountName -g $ContentStorageResourceGroup `
    --name $LibraryShareName -o none 2>$null
  if ($LASTEXITCODE -ne 0) { Write-Warning "Share '$LibraryShareName' not found. Create it before deploying (see infra/README.md)." }
  $contentKey = Invoke-Az storage account keys list -n $ContentStorageAccountName -g $ContentStorageResourceGroup --query '[0].value' -o tsv
}

if ($SetGitHubConfig) {
  if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI (gh) not found.' }
  foreach ($k in $secrets.Keys) { $secrets[$k] | gh secret set $k --repo $GitHubRepo }
  if ($contentKey) { $contentKey | gh secret set CONTENT_STORAGE_ACCOUNT_KEY --repo $GitHubRepo }
  foreach ($k in $variables.Keys) {
    if ($variables[$k]) { gh variable set $k --repo $GitHubRepo --body $variables[$k] }
  }
  Write-Host "GitHub secrets and variables set on $GitHubRepo."
  if (-not $contentKey) { Write-Warning 'Content storage not configured. Set the CONTENT_STORAGE_CONNECTION_STRING secret (or pass -ContentStorageAccountName and -ContentStorageResourceGroup), then redeploy.' }
} else {
  Write-Host "`nSet these GitHub repository SECRETS:"
  $secrets.GetEnumerator() | ForEach-Object { Write-Host "  $($_.Key) = $($_.Value)" }
  Write-Host '  CONTENT_STORAGE_ACCOUNT_KEY = <key of the content storage account>'
  Write-Host '  HARDCOVER_TOKEN = <optional>'
  Write-Host "`nSet these GitHub repository VARIABLES:"
  $variables.GetEnumerator() | ForEach-Object { Write-Host "  $($_.Key) = $($_.Value)" }
}
Write-Host "`nDone. Push to '$Branch' or run the 'Azure - build & deploy' workflow."
