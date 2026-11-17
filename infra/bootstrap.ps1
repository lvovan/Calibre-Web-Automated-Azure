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

  When -CustomDomainName is set (and -SkipOneDrive is not), also creates the "Import from OneDrive"
  app registration (personal Microsoft accounts, redirect https://<domain>/onedrive/callback,
  delegated Files.Read/User.Read/offline_access). A client secret is created for a new app, or
  with -RotateOneDriveSecret. If your tenant blocks this, manual steps are printed instead.

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
  [string] $OneDriveAppName = 'cwa-onedrive',
  [switch] $SkipOneDrive,
  [switch] $RotateOneDriveSecret,
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

function Add-FederatedCredential([string] $Name, [string] $Subject) {
  $existing = Invoke-Az ad app federated-credential list --id $appId --query "[?subject=='$Subject'].name" -o tsv
  if ($existing) { Write-Host "Federated credential for $Subject already exists."; return }
  Write-Host "Federated credential for $Subject..."
  $tmp = New-TemporaryFile
  @{
    name      = $Name
    issuer    = 'https://token.actions.githubusercontent.com'
    subject   = $Subject
    audiences = @('api://AzureADTokenExchange')
  } | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
  try { Invoke-Az ad app federated-credential create --id $appId --parameters "@$tmp" | Out-Null } finally { Remove-Item $tmp }
}

$credName = "github-$($GitHubRepo -replace '[^A-Za-z0-9-]', '-')-$Branch"
$credName = $credName.Substring(0, [Math]::Min(110, $credName.Length))
Add-FederatedCredential $credName "repo:${GitHubRepo}:ref:refs/heads/$Branch"
# Some repositories issue OIDC subjects with immutable owner/repo IDs (repo:owner@id/name@id:...)
if (Get-Command gh -ErrorAction SilentlyContinue) {
  $ids = & gh api "repos/$GitHubRepo" --jq '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"' 2>$null
  if ($LASTEXITCODE -eq 0 -and $ids) { Add-FederatedCredential "$credName-ids" "repo:${ids}:ref:refs/heads/$Branch" }
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

# ── Import from OneDrive: app registration for personal Microsoft accounts ──
$oneDriveClientId = $null
$oneDriveSecret = $null
if ($CustomDomainName -and -not $SkipOneDrive) {
  $redirectUri = "https://$CustomDomainName/onedrive/callback"
  $graphAppId = '00000003-0000-0000-c000-000000000000'
  $graphScopes = @(
    @{ id = 'df85f4d6-205c-4ac5-a5ea-6bf408dba283'; type = 'Scope' } # Files.Read
    @{ id = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'; type = 'Scope' } # User.Read
    @{ id = '7427e0e9-2fba-42fe-b0c0-848c9e6a8182'; type = 'Scope' } # offline_access
  )
  function ConvertFrom-AzJson($Lines) { $text = ($Lines -join "`n").Trim(); if ($text) { $text | ConvertFrom-Json } }
  function Invoke-GraphRest([string] $Method, [string] $Uri, $Body) {
    $tmp = New-TemporaryFile
    try {
      $Body | ConvertTo-Json -Depth 10 | Set-Content -Path $tmp -Encoding utf8
      ConvertFrom-AzJson (Invoke-Az rest --method $Method --uri $Uri --headers 'Content-Type=application/json' --body "@$tmp")
    } finally { Remove-Item $tmp }
  }
  try {
    Write-Host "OneDrive app registration $OneDriveAppName..."
    $app = ConvertFrom-AzJson (Invoke-Az ad app list --display-name $OneDriveAppName `
        --query '[0].{id:id,appId:appId,uris:web.redirectUris}' -o json)
    $isNew = -not $app
    if ($isNew) {
      $app = Invoke-GraphRest POST 'https://graph.microsoft.com/v1.0/applications' @{
        displayName            = $OneDriveAppName
        signInAudience         = 'PersonalMicrosoftAccount'
        api                    = @{ requestedAccessTokenVersion = 2 }
        web                    = @{ redirectUris = @($redirectUri) }
        requiredResourceAccess = @(@{ resourceAppId = $graphAppId; resourceAccess = $graphScopes })
      }
    } else {
      $uris = @(@($app.uris) + $redirectUri | Where-Object { $_ } | Select-Object -Unique)
      Invoke-GraphRest PATCH "https://graph.microsoft.com/v1.0/applications/$($app.id)" @{
        web                    = @{ redirectUris = $uris }
        requiredResourceAccess = @(@{ resourceAppId = $graphAppId; resourceAccess = $graphScopes })
      } | Out-Null
    }
    $oneDriveClientId = $app.appId
    if ($isNew -or $RotateOneDriveSecret) {
      # A just-created app can take a few seconds to replicate
      for ($i = 1; $i -le 6 -and -not $oneDriveSecret; $i++) {
        $oneDriveSecret = & az ad app credential reset --id $oneDriveClientId --append `
          --display-name 'cwa-onedrive' --years 2 --query password -o tsv 2>$null
        if (-not $oneDriveSecret) { Start-Sleep -Seconds 10 }
      }
      if (-not $oneDriveSecret) { throw 'client secret creation failed' }
      Write-Host 'OneDrive client secret created (expires in 2 years; rotate with -RotateOneDriveSecret).'
    } else {
      Write-Host 'OneDrive app exists; keeping its current secret (pass -RotateOneDriveSecret to create a new one).'
    }
  } catch {
    Write-Warning "Could not create the OneDrive app registration ($_). Your tenant may block it; create it manually:"
    Write-Host @"
  1. Entra admin center > App registrations > New registration
     Name: $OneDriveAppName
     Supported account types: Personal Microsoft accounts only
     Redirect URI: Web, $redirectUri
  2. API permissions > Add > Microsoft Graph > Delegated: Files.Read, User.Read, offline_access
  3. Certificates & secrets > New client secret
  4. Set GitHub variable ONEDRIVE_CLIENT_ID (Application ID) and secret ONEDRIVE_CLIENT_SECRET, then redeploy.
"@
  }
}

if ($SetGitHubConfig) {
  if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI (gh) not found.' }
  foreach ($k in $secrets.Keys) { $secrets[$k] | gh secret set $k --repo $GitHubRepo }
  if ($contentKey) { $contentKey | gh secret set CONTENT_STORAGE_ACCOUNT_KEY --repo $GitHubRepo }
  if ($oneDriveClientId) { gh variable set ONEDRIVE_CLIENT_ID --repo $GitHubRepo --body $oneDriveClientId }
  if ($oneDriveSecret) { $oneDriveSecret | gh secret set ONEDRIVE_CLIENT_SECRET --repo $GitHubRepo }
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
  if ($oneDriveSecret) { Write-Host "  ONEDRIVE_CLIENT_SECRET = $oneDriveSecret" }
  Write-Host "`nSet these GitHub repository VARIABLES:"
  $variables.GetEnumerator() | ForEach-Object { Write-Host "  $($_.Key) = $($_.Value)" }
  if ($oneDriveClientId) { Write-Host "  ONEDRIVE_CLIENT_ID = $oneDriveClientId" }
}
Write-Host "`nDone. Push to '$Branch' or run the 'Azure - build & deploy' workflow."
