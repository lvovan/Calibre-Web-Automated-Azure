# Deploying Calibre-Web Automated on Azure App Service

This fork can run CWA as a **Web App for Containers** on Azure App Service. The library lives on an **Azure Files (SMB)** share in your storage account; CWA's own configuration lives on the App Service plan's built-in persistent `/home` storage. Infrastructure is defined in Bicep (`infra/`), and deployment is automated with GitHub Actions (`.github/workflows/azure-deploy.yml`).

## Architecture

```
Resource group (default: rg-cwa, francecentral)
├─ App Service plan        Linux B1, 1 instance (includes 10 GB persistent /home storage)
├─ Web App for Containers  ghcr.io/<owner>/<repo>:sha-<commit>
│    /calibre-library ← SMB mount: external storage account, share "library" (key read from Key Vault)
│    /config          → symlink to /home/config (App Service persistent storage)
│    /cwa-book-ingest and /tmp/cwa_conversion on local container disk (transient)
├─ User-assigned identity  used by the web app to read Key Vault references
├─ Key Vault               Standard, access policies, secret "content-storage-key"
├─ Log Analytics           30-day retention, 0.1 GB/day cap
└─ Application Insights    workspace-based

External storage account (yours, not managed by these templates)
└─ Azure Files share "library"  ← Calibre library (metadata.db, books, covers)
```

| Path | Storage | Contents |
|---|---|---|
| `/calibre-library` | External account, Azure Files | Calibre library, including `metadata.db` |
| `/config` → `/home/config` | App Service persistent storage (`WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`) | `app.db` (users, settings), `cwa.db` (stats), logs, Calibre plugins |
| `/cwa-book-ingest` | Local container disk | Web UI uploads waiting to be imported (seconds) |
| `/tmp/cwa_conversion` | Local container disk | Ingest staging scratch files (CWA uses this folder for staging even when conversion is off) |

Books are added through the **web UI upload** button; the ingest folder isn't exposed as a share.

At startup, the `init-cwa-persistent-config` s6 service runs before the base image initialization. On App Service with `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, it creates `/home/config`, seeds it from the image on first start, and replaces `/config` with a symlink to it. Elsewhere it does nothing, so bind-mounting `/config` works as usual. `/home` counts toward the plan's storage quota (10 GB on B1), shared with the plan's other apps; `/config` typically stays well under 1 GB.

### Azure-specific settings (set by Bicep)

| App setting | Value | Why |
|---|---|---|
| `NETWORK_SHARE_MODE` | `true` | Disables SQLite WAL and `chown`, and uses polling watchers. All are required on SMB and on `/home`, which is also network storage. |
| `CWA_TMP_CONVERSION_DIR` | `/tmp/cwa_conversion` | Keeps ingest staging I/O on local disk instead of persistent `/config`. |
| `CWA_DISABLE_AUTO_CONVERT` | `true` | New installs import uploads in their original format, with no automatic conversion to EPUB. Can be re-enabled in CWA Settings. |
| `CWA_DISABLE_PROCESSED_BACKUPS` | `true` | New installs don't archive processed originals to `/config/processed_books`. Can be re-enabled in CWA Settings. |
| `TRUSTED_PROXY_COUNT` | `1` | The App Service front end is the only proxy. |
| `WEBSITES_PORT` | `8083` | CWA listen port. |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | `true` | Mounts persistent `/home`, which holds `/config`. |
| `CONTENT_STORAGE_KEY` | `@Microsoft.KeyVault(SecretUri=https://kv-….vault.azure.net/secrets/content-storage-key/<version>)` | Key Vault reference. The `/calibre-library` mount reads its access key from it (`@AppSettingRef(CONTENT_STORAGE_KEY)`). Only set when content storage is configured. |

### Content storage key and Key Vault

The deployment workflow writes the content account key (from the `CONTENT_STORAGE_CONNECTION_STRING` GitHub secret) to the `content-storage-key` secret in the Key Vault. The web app resolves it with its user-assigned identity, which is the vault's only access policy (*get* secrets). Every deployment re-applies the access policies, so policies added by hand are removed. Keep the GitHub secret as the source of truth: a value changed directly in Key Vault is overwritten by the next deployment.

App Service resolves Key Vault references in app settings into environment variables, so the key is also visible inside the container as `CONTENT_STORAGE_KEY`. Key Vault adds central storage, auditing and rotation; it doesn't hide the key from the container itself.

On App Service (`WEBSITE_SITE_NAME` is set), `cwa-init` refuses to start if `/calibre-library` isn't an Azure Files (network) mount, or if `/config` is neither a mount nor on persistent `/home`. This prevents data from being silently written to ephemeral disk, including the anonymous volume Docker creates for `/calibre-library` when no share is mounted.

### Slimmer image

The `azure-deploy.yml` workflow sets two Dockerfile build arguments to leave out features this deployment doesn't use. Both default to the full upstream image, so other builds are unchanged.

| Build argument | Value in `azure-deploy.yml` | Effect |
|---|---|---|
| `INSTALL_KEPUBIFY` | `false` | No kepubify. KEPUB conversion is unavailable, and Kobo sync sends plain EPUB instead of KEPUB. |
| `PIP_EXCLUDE` | `google-api-python-client PyDrive2 oauth2client google-auth-oauthlib goodreads python-Levenshtein python-ldap Flask-SimpleLDAP scholarly` | Drops the Google Drive library, Gmail sending, Goodreads, LDAP login and the Google Scholar metadata source. |

The packages in `PIP_EXCLUDE` are moved to `optional-requirements.txt` inside the image. The startup dependency check treats them as optional, so it doesn't stop the app. To get a feature back, remove its package from `PIP_EXCLUDE` (or set `INSTALL_KEPUBIFY=true`) and push.

## Prerequisites

- Azure CLI ≥ 2.53 (`az login`). You need Owner or User Access Administrator on the subscription or resource group for the bootstrap, plus permission to create Entra app registrations.
- GitHub CLI (`gh auth login`), optional, to let the bootstrap set repository secrets.
- An existing storage account (StorageV2, Standard) with **shared key access enabled** and an Azure Files share for the library. The account should be in the **same region** as the app; latency matters for SQLite. Use a **dedicated** account: the key grants full access to it.
- A subscription where storage accounts may keep **shared key access** and **public network access** enabled. App Service storage mounts only support account keys. Policies that force `allowSharedKeyAccess=false` or `publicNetworkAccess=Disabled`, common in corporate tenants, make the mounts fail with `BYOSFailure ... Permission was denied` in the container log. Request a policy exemption for the content storage account, or use another subscription. (`/config` uses App Service's built-in storage, so it isn't affected.)

Create the library share if needed:

```bash
az storage share-rm create -g <content-rg> --storage-account <content-account> \
  --name library --quota 100 --access-tier Hot
```

## 1. Bootstrap (one-time)

Creates the resource group, an Entra app registration with a GitHub OIDC federated credential (no client secret), and a Contributor role assignment on the resource group. With `-SetGitHubConfig`, it also writes the GitHub secrets and variables.

```powershell
./infra/bootstrap.ps1 -GitHubRepo <owner>/<repo> `
  -ContentStorageAccountName <content-account> -ContentStorageResourceGroup <content-rg> `
  -SetGitHubConfig
```

```bash
GITHUB_REPO=<owner>/<repo> CONTENT_STORAGE_ACCOUNT_NAME=<content-account> \
CONTENT_STORAGE_RESOURCE_GROUP=<content-rg> SET_GITHUB_CONFIG=true ./infra/bootstrap.sh
```

GitHub configuration used by the workflow:

| Kind | Name | Notes |
|---|---|---|
| Secret | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | OIDC login |
| Secret | `CONTENT_STORAGE_CONNECTION_STRING` | **Simplest option:** connection string of the external content account (portal: storage account → *Security + networking* → *Access keys* → *Connection string*). Takes precedence over the two settings below. |
| Secret | `CONTENT_STORAGE_ACCOUNT_KEY` | Alternative: key of the external content account |
| Secret | `HARDCOVER_TOKEN` | Optional |
| Variable | `AZURE_RESOURCE_GROUP` | e.g. `rg-cwa` |
| Variable | `CONTENT_STORAGE_ACCOUNT_NAME` | Alternative: external content account name |
| Variable | `LIBRARY_SHARE_NAME` | Default `library` |
| Variable | `CUSTOM_DOMAIN_NAME` | Leave unset until DNS is ready (step 4) |

The content storage can be configured after the first deployment. Until it is set, the library mount is omitted, the workflow skips the health check with a warning, and the container refuses to start (`/calibre-library is not a mount` in the log stream). Configure it in GitHub, not in the portal's *Path mappings*: every deployment overwrites the portal settings.

## 2. First deployment

Push to `main`, or run **Actions → Azure - build & deploy → Run workflow**. The workflow:

1. Builds the image and pushes `ghcr.io/<owner>/<repo>:sha-<commit>` and `:latest`.
2. Deploys `infra/main.bicep` with that image.
3. Waits up to 20 minutes for `/health` to return 200.

**The first build creates a private GHCR package.** App Service pulls anonymously, so make it public once: GitHub → your profile → *Packages* → the package → *Package settings* → *Change visibility* → Public. Then re-run the workflow, or restart the web app.

Deploying manually from a workstation:

```powershell
$env:CWA_CONTAINER_IMAGE = 'ghcr.io/<owner>/<repo>:latest'
$env:CONTENT_STORAGE_ACCOUNT_NAME = '<content-account>'
$env:CONTENT_STORAGE_ACCOUNT_KEY = (az storage account keys list -n <content-account> -g <content-rg> --query '[0].value' -o tsv)
az deployment group create -g rg-cwa --parameters infra/main.bicepparam
```

## 3. First login

Open the default URL (output `defaultUrl`, e.g. `https://app-cwa-<suffix>.azurewebsites.net`). Log in with **admin / admin123** and **change the password immediately**: the app is publicly reachable. On an empty share, CWA creates a new library automatically.

Authentication is CWA's built-in username/password login. OPDS and KOReader sync use HTTP Basic with the same credentials; Kobo devices use a per-user token. Books can be read in the browser (EPUB, PDF, comics, DjVu, TXT, audio).

## 4. Custom domain with a free managed certificate

Use a subdomain, e.g. `books.example.com`.

1. After the first deployment, read the `customDomainVerificationId` output (shown in the workflow summary).
2. Create the DNS records:
   - `CNAME books.example.com → app-cwa-<suffix>.azurewebsites.net`
   - `TXT asuid.books.example.com → <customDomainVerificationId>`
3. Set the GitHub variable `CUSTOM_DOMAIN_NAME=books.example.com` and re-run the workflow.

The deployment binds the hostname, creates an App Service managed certificate (free, renewed automatically), then enables SNI TLS. Each deployment re-applies the binding, which may cause a brief TLS hiccup on that hostname during the restart.

## Cost (France Central, approximate)

| Resource | Monthly |
|---|---|
| App Service plan B1 Linux (includes 10 GB `/home` storage for `/config`) | ~€12 |
| Library share (Hot) | ~€0.03/GiB + transactions |
| Log Analytics / App Insights | €0 within the 5 GB/month free allowance; hard cap 0.1 GB/day |
| Key Vault (Standard) | A few cents (secret reads at app restarts and every 24 hours) |
| Managed certificate, GHCR | Free |

The polling watchers (`NETWORK_SHARE_MODE`) generate a steady trickle of SMB transactions on the library share, typically well under €1/month.

**Why not the Free (F1) tier?** F1 has no Always On (the app unloads after 20 minutes idle, stopping CWA's background services), a 60 CPU-minute daily quota, and 1 GB of RAM. Imports still run Calibre (`calibredb`, `ebook-meta`) even with conversions disabled. B1 is the cheapest workable tier.

## Monitoring

- **Log stream**: Portal → Web App → *Log stream* (container stdout).
- **Log Analytics**: `AppServiceConsoleLogs`, `AppServiceHTTPLogs` and `AppServicePlatformLogs` tables.
- **CWA Logs** page in the UI, and `/home/config/log_archive` (browse it with Kudu: `https://<app>.scm.azurewebsites.net/newui` → *File Manager*).

To change the caps, use the `logDailyQuotaGb` and `logRetentionInDays` parameters. Once the daily cap is reached, ingestion stops until the next day.

## Operations

### Rotating the content storage key

1. Update the `CONTENT_STORAGE_CONNECTION_STRING` secret with the connection string for **key2** (or `CONTENT_STORAGE_ACCOUNT_KEY` with `az storage account keys list ... --query '[1].value'`).
2. Re-run the workflow. It writes a new version of the Key Vault secret and restarts the app, so the mount now uses key2.
3. Regenerate key1: `az storage account keys renew -n <account> -g <rg> --key key1`.

Next time, swap roles.

### Backup and restore

- **Library:** enable share soft delete on the external content account yourself. Soft delete protects against **share deletion**, not file corruption. For point-in-time protection, take a share snapshot before risky operations:
  `az storage share snapshot --account-name <account> --name library` (you pay only for changed data).
  To copy data out: `azcopy copy "https://<account>.file.core.windows.net/library?<sas>" ./backup --recursive`, or use Azure Storage Explorer.
- **`/config`:** download it as a zip through Kudu (needs an account with Contributor or higher on the app):
  ```bash
  curl -fSL -H "Authorization: Bearer $(az account get-access-token --query accessToken -o tsv)" \
    -o cwa-config.zip https://<app>.scm.azurewebsites.net/api/zip/home/config/
  ```
  `/home` survives restarts, redeployments and image changes, but it's deleted with the web app.

### Migrating an existing library later

1. Stop the web app.
2. Upload the library (with `metadata.db`) to the `library` share with `azcopy` or Storage Explorer. To bring an existing CWA `/config` (`app.db`, `cwa.db`), upload it to `/home/config` with Kudu's *File Manager*, or `PUT https://<app>.scm.azurewebsites.net/api/zip/home/config/` with a zip.
3. Start the web app.

## Limitations

- **Single instance only.** `metadata.db`, `app.db` and `cwa.db` are SQLite. Never scale out the plan; it's pinned to one instance.
- **SQLite on network storage.** WAL is disabled and access is serialized, but a hard crash during a write can still corrupt a database. Keep share snapshots and `/config` backups for anything you can't lose.
- **Performance.** Every library query and cover read goes over SMB. Large library pages are slower than on local disk.
- **In-flight uploads.** Uploads waiting in the local ingest folder are lost if the app restarts before they are imported.
- **Account keys.** App Service SMB mounts require the storage account key; SAS and managed identity aren't supported for mounts. The key is stored in Key Vault, but App Service also exposes it to the container as an environment variable.
- **On-demand conversion.** Automatic conversion is off, but the stock Calibre-Web **Convert** button on a book's edit page and CWA's **Convert Library** page remain available to users with edit or admin rights. Avoid them on B1: conversions are CPU-heavy and write through SMB.

## Post-deployment validation checklist

Run through this once after the first deployment:

- [ ] `/health` returns 200, and the log stream shows `[cwa-persistent-config] /config -> /home/config`, `[cwa-init][azure] /calibre-library is mounted` and `/config is stored on persistent App Service storage`.
- [ ] A new library is created on the `library` share (`metadata.db` visible in the portal).
- [ ] Upload an EPUB through the web UI; it appears in the library within a minute.
- [ ] Edit metadata and the cover; the change persists after a page reload.
- [ ] Create a user, then restart the web app; the user and books are still there.
- [ ] Optional: download `metadata.db`, `app.db` and `cwa.db` and run `sqlite3 <db> "PRAGMA integrity_check"`. Expect `ok`.
- [ ] Check memory and CPU in *Diagnose and solve problems* during an import (B1: 1.75 GB).

## Keeping up with upstream

Azure-specific changes are isolated so upstream releases merge cleanly:

- `infra/` (including this guide) and `.github/workflows/azure-deploy.yml`: new files.
- Upstream-only workflows (Docker Hub, Discord, translations) are skipped on forks with `if: github.repository == 'crocodilestick/Calibre-Web-Automated'`.
- Code changes, all inert unless the environment variable is set:
  - `CWA_TMP_CONVERSION_DIR` in `scripts/ingest_processor.py`, `scripts/convert_library.py` and `cps/cwa_functions.py`
  - `CWA_DISABLE_PROCESSED_BACKUPS` and `CWA_DISABLE_AUTO_CONVERT` in `scripts/cwa_db.py`
  - `init-cwa-persistent-config` s6 oneshot (runs before `init-migrations`; active only on App Service with `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`)
  - the App Service storage check and local-ingest ownership in `root/etc/s6-overlay/s6-rc.d/cwa-init/run`
- `Dockerfile` doesn't declare `VOLUME /config`: an anonymous volume would prevent the `/config` → `/home/config` symlink. Keep this when merging upstream Dockerfile changes.
