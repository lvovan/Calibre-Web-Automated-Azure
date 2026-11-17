#!/usr/bin/env bash
# One-time Azure + GitHub setup for deploying Calibre-Web Automated to Azure App Service.
# Idempotent. Bash equivalent of infra/bootstrap.ps1 - see that file for details.
#
# Usage:
#   GITHUB_REPO=owner/repo [RESOURCE_GROUP=rg-cwa] [LOCATION=francecentral] [BRANCH=main] \
#   [APP_REGISTRATION_NAME=github-cwa-deploy] [SUBSCRIPTION_ID=...] \
#   [CONTENT_STORAGE_ACCOUNT_NAME=... CONTENT_STORAGE_RESOURCE_GROUP=...] [LIBRARY_SHARE_NAME=library] \
#   [CUSTOM_DOMAIN_NAME=books.example.com] [SET_GITHUB_CONFIG=true] ./infra/bootstrap.sh
set -euo pipefail

: "${GITHUB_REPO:?Set GITHUB_REPO=owner/repo}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-cwa}"
LOCATION="${LOCATION:-francecentral}"
BRANCH="${BRANCH:-main}"
APP_REGISTRATION_NAME="${APP_REGISTRATION_NAME:-github-cwa-deploy}"
LIBRARY_SHARE_NAME="${LIBRARY_SHARE_NAME:-library}"
CUSTOM_DOMAIN_NAME="${CUSTOM_DOMAIN_NAME:-}"
CONTENT_STORAGE_ACCOUNT_NAME="${CONTENT_STORAGE_ACCOUNT_NAME:-}"
CONTENT_STORAGE_RESOURCE_GROUP="${CONTENT_STORAGE_RESOURCE_GROUP:-}"
SET_GITHUB_CONFIG="${SET_GITHUB_CONFIG:-false}"

if [ -n "${SUBSCRIPTION_ID:-}" ]; then az account set --subscription "$SUBSCRIPTION_ID"; fi
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
echo "Subscription: $(az account show --query name -o tsv) ($SUBSCRIPTION_ID)"

echo "Resource group $RESOURCE_GROUP ($LOCATION)..."
RG_ID="$(az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --query id -o tsv)"

echo "App registration $APP_REGISTRATION_NAME..."
APP_ID="$(az ad app list --display-name "$APP_REGISTRATION_NAME" --query '[0].appId' -o tsv)"
if [ -z "$APP_ID" ]; then
  APP_ID="$(az ad app create --display-name "$APP_REGISTRATION_NAME" --query appId -o tsv)"
fi
SP_ID="$(az ad sp list --filter "appId eq '$APP_ID'" --query '[0].id' -o tsv)"
if [ -z "$SP_ID" ]; then
  SP_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"
fi

add_federated_credential() { # $1 = name, $2 = subject
  if [ -z "$(az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$2'].name" -o tsv)" ]; then
    echo "Federated credential for $2..."
    az ad app federated-credential create --id "$APP_ID" --parameters "{
      \"name\": \"$1\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"$2\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }" -o none
  else
    echo "Federated credential for $2 already exists."
  fi
}

CRED_NAME="$(echo "github-${GITHUB_REPO}-${BRANCH}" | tr -c 'A-Za-z0-9-\n' '-' | cut -c1-110)"
add_federated_credential "$CRED_NAME" "repo:${GITHUB_REPO}:ref:refs/heads/${BRANCH}"
# Some repositories issue OIDC subjects with immutable owner/repo IDs (repo:owner@id/name@id:...)
if command -v gh >/dev/null 2>&1; then
  IDS="$(gh api "repos/${GITHUB_REPO}" --jq '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"' 2>/dev/null || true)"
  [ -n "$IDS" ] && add_federated_credential "${CRED_NAME}-ids" "repo:${IDS}:ref:refs/heads/${BRANCH}"
fi

echo "Contributor role on $RESOURCE_GROUP..."
if [ -z "$(az role assignment list --assignee "$SP_ID" --scope "$RG_ID" --role Contributor --query '[0].id' -o tsv)" ]; then
  for i in 1 2 3 4 5 6; do
    if az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
         --role Contributor --scope "$RG_ID" -o none 2>/dev/null; then break; fi
    [ "$i" = 6 ] && { echo "Role assignment failed" >&2; exit 1; }
    sleep 10 # new service principals can take a few seconds to replicate
  done
fi

CONTENT_KEY=""
if [ -n "$CONTENT_STORAGE_ACCOUNT_NAME" ] && [ -n "$CONTENT_STORAGE_RESOURCE_GROUP" ]; then
  echo "Checking share '$LIBRARY_SHARE_NAME' in $CONTENT_STORAGE_ACCOUNT_NAME..."
  if ! az storage share-rm show --storage-account "$CONTENT_STORAGE_ACCOUNT_NAME" -g "$CONTENT_STORAGE_RESOURCE_GROUP" \
       --name "$LIBRARY_SHARE_NAME" -o none 2>/dev/null; then
    echo "WARNING: share '$LIBRARY_SHARE_NAME' not found. Create it before deploying (see infra/README.md)." >&2
  fi
  CONTENT_KEY="$(az storage account keys list -n "$CONTENT_STORAGE_ACCOUNT_NAME" -g "$CONTENT_STORAGE_RESOURCE_GROUP" --query '[0].value' -o tsv)"
fi

if [ "$SET_GITHUB_CONFIG" = "true" ]; then
  command -v gh >/dev/null || { echo "GitHub CLI (gh) not found." >&2; exit 1; }
  gh secret set AZURE_CLIENT_ID --repo "$GITHUB_REPO" --body "$APP_ID"
  gh secret set AZURE_TENANT_ID --repo "$GITHUB_REPO" --body "$TENANT_ID"
  gh secret set AZURE_SUBSCRIPTION_ID --repo "$GITHUB_REPO" --body "$SUBSCRIPTION_ID"
  [ -n "$CONTENT_KEY" ] && printf '%s' "$CONTENT_KEY" | gh secret set CONTENT_STORAGE_ACCOUNT_KEY --repo "$GITHUB_REPO"
  gh variable set AZURE_RESOURCE_GROUP --repo "$GITHUB_REPO" --body "$RESOURCE_GROUP"
  gh variable set LIBRARY_SHARE_NAME --repo "$GITHUB_REPO" --body "$LIBRARY_SHARE_NAME"
  [ -n "$CONTENT_STORAGE_ACCOUNT_NAME" ] && gh variable set CONTENT_STORAGE_ACCOUNT_NAME --repo "$GITHUB_REPO" --body "$CONTENT_STORAGE_ACCOUNT_NAME"
  [ -n "$CUSTOM_DOMAIN_NAME" ] && gh variable set CUSTOM_DOMAIN_NAME --repo "$GITHUB_REPO" --body "$CUSTOM_DOMAIN_NAME"
  echo "GitHub secrets and variables set on $GITHUB_REPO."
  [ -z "$CONTENT_KEY" ] && echo "WARNING: content storage not configured. Set the CONTENT_STORAGE_CONNECTION_STRING secret, then redeploy." >&2
else
  cat <<EOF

Set these GitHub repository SECRETS:
  AZURE_CLIENT_ID = $APP_ID
  AZURE_TENANT_ID = $TENANT_ID
  AZURE_SUBSCRIPTION_ID = $SUBSCRIPTION_ID
  CONTENT_STORAGE_ACCOUNT_KEY = <key of the content storage account>
  HARDCOVER_TOKEN = <optional>

Set these GitHub repository VARIABLES:
  AZURE_RESOURCE_GROUP = $RESOURCE_GROUP
  CONTENT_STORAGE_ACCOUNT_NAME = $CONTENT_STORAGE_ACCOUNT_NAME
  LIBRARY_SHARE_NAME = $LIBRARY_SHARE_NAME
  CUSTOM_DOMAIN_NAME = $CUSTOM_DOMAIN_NAME
EOF
fi
echo
echo "Done. Push to '$BRANCH' or run the 'Azure - build & deploy' workflow."
