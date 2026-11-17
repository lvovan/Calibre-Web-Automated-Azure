# Deploying Calibre-Web Automated on Azure App Service

This fork can run CWA as a **Web App for Containers** on Azure App Service, with all persistent data on **Azure Files (SMB)** shares. Infrastructure is defined in Bicep (`infra/`), and deployment is automated with GitHub Actions (`.github/workflows/azure-deploy.yml`).

## Architecture

```
Resource group (default: rg-cwa, francecentral)
├─ App Service plan        Linux B1, 1 instance
├─ Web App for Containers  ghcr.io/<owner>/<repo>:sha-<commit>
│    /calibre-library ← SMB mount: external storage account, share "library"
│    /config          ← SMB mount: ops storage account, share "config"
│    /cwa-book-ingest and /tmp/cwa_conversion on local container disk (transient)
├─ Storage account (ops)   Standard_LRS, share "config" (5 GiB), 7-day share soft delete
├─ Log Analytics           30-day retention, 0.1 GB/day cap
└─ Application Insights    workspace-based

External storage account (yours, not managed by these templates)
└─ Azure Files share "library"  ← Calibre library (metadata.db, books, covers)
```

| Path | Storage | Contents |
|---|---|---|
| `/calibre-library` | External account, Azure Files | Calibre library, including `metadata.db` |
| `/config` | Ops account in the app's resource group | `app.db` (users, settings), `cwa.db` (stats), logs, Calibre plugins |
| `/cwa-book-ingest` | Local container disk | Web UI uploads waiting to be imported (seconds) |
| `/tmp/cwa_conversion` | Local container disk | Ingest staging scratch files (CWA uses this folder for staging even when conversion is off) |

Books are added through the **web UI upload** button; the ingest folder isn't exposed as a share.

### Azure-specific settings (set by Bicep)

| App setting | Value | Why |
|---|---|---|
| `NETWORK_SHARE_MODE` | `true` | Disables SQLite WAL and `chown`, and uses polling watchers. All are required on SMB. |
| `CWA_TMP_CONVERSION_DIR` | `/tmp/cwa_conversion` | Keeps ingest staging I/O off the `/config` share. |
| `CWA_DISABLE_AUTO_CONVERT` | `true` | New installs import uploads in their original format, with no automatic conversion to EPUB. Can be re-enabled in CWA Settings. |
| `CWA_DISABLE_PROCESSED_BACKUPS` | `true` | New installs don't archive processed originals to `/config/processed_books`. Can be re-enabled in CWA Settings. |
| `TRUSTED_PROXY_COUNT` | `1` | The App Service front end is the only proxy. |
| `WEBSITES_PORT` | `8083` | CWA listen port. |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | `false` | All persistence is on the explicit Azure Files mounts. |

On App Service (`WEBSITE_SITE_NAME` is set), `cwa-init` refuses to start if `/calibre-library` or `/config` isn't a mount. This prevents a library from being silently created on ephemeral disk.

## Prerequisites

- Azure CLI ≥ 2.53 (`az login`). You need Owner or User Access Administrator on the subscription or resource group for the bootstrap, plus permission to create Entra app registrations.
- GitHub CLI (`gh auth login`), optional, to let the bootstrap set repository secrets.
- An existing storage account (StorageV2, Standard) with **shared key access enabled** and an Azure Files share for the library. The account should be in the **same region** as the app; latency matters for SQLite. Use a **dedicated** account: the key grants full access to it.
- A subscription where storage accounts may keep **shared key access** and **public network access** enabled. App Service storage mounts only support account keys. Policies that force `allowSharedKeyAccess=false` or `publicNetworkAccess=Disabled`, common in corporate tenants, make the mounts fail with `BYOSFailure ... Permission was denied` in the container log. Request a policy exemption for both storage accounts, or use another subscription.

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
| App Service plan B1 Linux | ~€12 |
| Ops storage (config share, < 1 GiB, TransactionOptimized) | < €0.50 |
| Library share (Hot) | ~€0.03/GiB + transactions |
| Log Analytics / App Insights | €0 within the 5 GB/month free allowance; hard cap 0.1 GB/day |
| Managed certificate, GHCR | Free |

The polling watchers (`NETWORK_SHARE_MODE`) generate a steady trickle of SMB transactions on the shares, typically well under €1/month.

**Why not the Free (F1) tier?** F1 has no Always On (the app unloads after 20 minutes idle, stopping CWA's background services), a 60 CPU-minute daily quota, and 1 GB of RAM. Imports still run Calibre (`calibredb`, `ebook-meta`) even with conversions disabled. B1 is the cheapest workable tier.

## Monitoring

- **Log stream**: Portal → Web App → *Log stream* (container stdout).
- **Log Analytics**: `AppServiceConsoleLogs`, `AppServiceHTTPLogs` and `AppServicePlatformLogs` tables.
- **CWA Logs** page in the UI, and `/config/log_archive` on the config share.

To change the caps, use the `logDailyQuotaGb` and `logRetentionInDays` parameters. Once the daily cap is reached, ingestion stops until the next day.

## Operations

### Rotating the content storage key

1. Update the `CONTENT_STORAGE_ACCOUNT_KEY` secret with **key2**: `az storage account keys list ... --query '[1].value'`.
2. Re-run the workflow. The mount now uses key2.
3. Regenerate key1: `az storage account keys renew -n <account> -g <rg> --key key1`.

Next time, swap roles. The ops account key is read at deployment time, so after rotating it, just redeploy.

### Backup and restore

- Share soft delete is enabled on the ops account (7 days). Enable it on the external content account yourself. Soft delete protects against **share deletion**, not file corruption.
- For point-in-time protection, take a share snapshot before risky operations:
  `az storage share snapshot --account-name <account> --name library` (you pay only for changed data).
- To copy data out: `azcopy copy "https://<account>.file.core.windows.net/library?<sas>" ./backup --recursive`, or use Azure Storage Explorer.

### Migrating an existing library later

1. Stop the web app.
2. Upload the library (with `metadata.db`) to the `library` share, and optionally an existing CWA `/config` (`app.db`, `cwa.db`) to the `config` share. Use `azcopy` or Storage Explorer.
3. Start the web app.

## Limitations

- **Single instance only.** `metadata.db`, `app.db` and `cwa.db` are SQLite. Never scale out the plan; it's pinned to one instance.
- **SQLite on SMB.** WAL is disabled and access is serialized, but a hard crash during a write can still corrupt a database. Keep share snapshots for anything you can't lose.
- **Performance.** Every library query and cover read goes over SMB. Large library pages are slower than on local disk.
- **In-flight uploads.** Uploads waiting in the local ingest folder are lost if the app restarts before they are imported.
- **Account keys.** App Service SMB mounts require the storage account key; SAS and managed identity aren't supported for mounts.
- **On-demand conversion.** Automatic conversion is off, but the stock Calibre-Web **Convert** button on a book's edit page and CWA's **Convert Library** page remain available to users with edit or admin rights. Avoid them on B1: conversions are CPU-heavy and write through SMB.

## Post-deployment validation checklist

Run through this once after the first deployment:

- [ ] `/health` returns 200, and the log stream shows `[cwa-init][azure] /calibre-library is mounted` and `/config is mounted`.
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
  - the App Service mount check and local-ingest ownership in `root/etc/s6-overlay/s6-rc.d/cwa-init/run`
