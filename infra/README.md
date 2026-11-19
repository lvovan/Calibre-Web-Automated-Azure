# Deploying Calibre-Web Automated on Azure App Service

This fork can run CWA as a **Web App for Containers** on Azure App Service. The library lives on an **Azure Files (SMB)** share in your storage account; CWA's own configuration lives on the App Service plan's built-in persistent `/home` storage. Infrastructure is defined in Bicep (`infra/`), and deployment is automated with GitHub Actions (`.github/workflows/azure-deploy.yml`).

## Architecture

```
Resource group (default: rg-cwa, francecentral)
├─ App Service plan        Linux B1, 1 instance (includes 10 GB persistent /home storage)
├─ Web App for Containers  ghcr.io/<owner>/<repo>:sha-<commit>
│    /calibre-library ← SMB mount: external storage account, share "library"
│    /config          → symlink to /home/config (App Service persistent storage)
│    /cwa-book-ingest and /tmp/cwa_conversion on local container disk (transient)
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

### Large EPUBs and manga

The EPUB reader serves local EPUB/KEPUB files one resource at a time through authenticated,
revisioned URLs. It does not download the whole archive or extract it onto persistent storage.
Each request rechecks library visibility and viewer permissions; cached resources require revalidation.
Individual resources are limited to 64 MiB. Google Drive books retain the archive-based loading path.

Reader settings offer **Auto / Text / Manga**, saved per user and book. Auto samples the reading
order, not the title or archive size. Comic pages made of a single image (including converter output
with stylesheets or an SVG image wrapper) use a bounded image renderer with zoom, optional spreads and
RTL navigation; other pages of such books (credits, chapter titles) are shown in script-free frames.
The menu lists the pages, grouped by chapter. Books with mostly styled, captioned or mixed content
retain the EPUB engine and original markup. Manga uses page progress without text indexing. Novels
keep the text renderer and text-based percentages, with one revision-aware location index and exact
CFI position restoration. Existing bookmarks and percentage-based progress can still be restored.

The PDF reader has a **Manga mode** (in the tools menu): one page at a time fitted to the screen, page
turns by tapping the screen edges, swiping or arrow keys, optional right-to-left reading, and the next
pages loaded ahead. It turns on automatically for PDFs without text (scans and comics) unless the
user switched it off for that book.

### Library search

Search matches whole words, plurals and quoted phrases. When nothing matches as typed, it retries
tolerating typos (one edit for words of 4–7 letters, two from 8, missing spaces and cut-off words) in
titles, authors and series. Quoted phrases always match exactly.

### Reading progress across devices

When **KOReader sync** is enabled in the admin settings, the web readers for EPUB/KEPUB, comics
(CBZ/CBR/CBT), PDF and TXT share each signed-in user's position through the built-in kosync server,
in the same per-user record KOReader devices use. Each user has their own position per book.
Books open at the most recently saved position from any device; if another device moves ahead while a
book is open, the reader offers to jump there. Positions use KOReader's formats (an XPointer for
EPUB, a page number for PDFs and comics), so web readers and KOReader devices resume each other's
pages. Limitations: TXT exchanges only the percentage with devices; DJVU and anonymous browsing are not
synchronized; one position is kept per book across all of its formats. With KOReader sync disabled,
the readers keep their previous browser-local behavior.

Each reader has a **Sync progress now** action (EPUB settings, comic menu, PDF tools menu, TXT ↻
button) that saves the current position and opens a newer one from another device. KOReader
identifies a book by a hash of its file; copies downloaded before KOReader sync was enabled (or
modified since) have hashes the library does not know. When such a device's latest EPUB position
resolves exactly in the book being read, the EPUB reader offers to continue from it and, if accepted,
links that hash to the book so both sides stay in sync from then on.

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
| `CWA_ALLOWED_HOSTS` | `CUSTOM_DOMAIN_NAME` (only when set) | Returns 404 for any other `Host`, which blocks the default `*.azurewebsites.net` hostname. See step 4. |

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
| Variable | `ONEDRIVE_CLIENT_ID` | Optional, set by bootstrap: enables *Import from OneDrive* (step 5) |
| Secret | `ONEDRIVE_CLIENT_SECRET` | Optional, set by bootstrap: client secret of the OneDrive app registration |

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

Setting `CUSTOM_DOMAIN_NAME` also sets `CWA_ALLOWED_HOSTS`, which makes the app (`cps/host_allowlist.py`) return a plain 404 for every request whose `Host` header is not the custom domain. The default `app-cwa-<suffix>.azurewebsites.net` hostname stops serving the app at no extra cost.
- `/health` stays reachable on any host, for the App Service health check and the deploy workflow.
- `localhost` stays allowed, for the container `HEALTHCHECK`.
- The check uses the raw `Host` header, which the App Service front end routes on. A client-supplied `X-Forwarded-Host` can't bypass it.
- Kudu (`*.scm.azurewebsites.net`) is a separate site, protected by Entra ID, and isn't affected.
- `CUSTOM_DOMAIN_NAME` sets `CWA_ALLOWED_HOSTS` to that single domain. You can set a comma-separated list on the app instead, but each deployment resets it to `CUSTOM_DOMAIN_NAME`.

## 5. Import from OneDrive (optional)

Users with the *Upload* permission get a ☁️ button next to **Upload**. It opens a page where they connect their personal OneDrive (Microsoft consumer account, read-only), browse it, and import:
- **Selected files**: only formats this server accepts for upload; other files are greyed out.
- **Selected folders**: every file in one of those formats, found recursively; other file types are ignored.
- **Duplicates**: files with the same content (OneDrive hash) are imported once. Books already imported from OneDrive, or already in the library (title/author match, regardless of the *automerge* setting), are skipped.

Imports run in a persistent queue (app.db), two at a time, downloading one file at a time into the ingest folder. They survive restarts and appear on the **Tasks** page and under *Your imports* on the OneDrive page, where they can be cancelled. The connection is kept: the refresh token is stored encrypted in app.db until the user clicks **Disconnect**.

The feature needs `CUSTOM_DOMAIN_NAME` (the OAuth redirect URI is `https://<CUSTOM_DOMAIN_NAME>/onedrive/callback`) and an Entra app registration:

- **Automatic**: re-run the bootstrap with `-CustomDomainName books.example.com -SetGitHubConfig` (bash: `CUSTOM_DOMAIN_NAME=… SET_GITHUB_CONFIG=true`). It creates the app `cwa-onedrive` (personal Microsoft accounts only, Graph delegated `Files.Read`, `User.Read`, `offline_access`) and a 2-year client secret, and sets `ONEDRIVE_CLIENT_ID` / `ONEDRIVE_CLIENT_SECRET`. Then re-run the workflow.
- **Manual** (if your tenant blocks app creation, the bootstrap prints these steps): in any Entra tenant, *App registrations → New registration*, *Personal Microsoft accounts only*, redirect URI *Web* `https://<domain>/onedrive/callback`; add the three delegated Graph permissions; create a client secret; set the GitHub variable and secret above.

No admin consent is needed: each user consents when connecting. The settings are only applied when the client ID, the secret and `CUSTOM_DOMAIN_NAME` are all set; otherwise the button is hidden.

**Rotating the secret** (it expires after 2 years): re-run the bootstrap with `-RotateOneDriveSecret` (bash: `ROTATE_ONEDRIVE_SECRET=true`), re-run the workflow, then delete the old secret in the app registration. Users stay connected.

## Cost (France Central, approximate)

| Resource | Monthly |
|---|---|
| App Service plan B1 Linux (includes 10 GB `/home` storage for `/config`) | ~€12 |
| Library share (Hot) | ~€0.03/GiB + transactions |
| Log Analytics / App Insights | €0 within the 5 GB/month free allowance; hard cap 0.1 GB/day |
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
2. Re-run the workflow. The mount now uses key2.
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
- **No overlapping containers.** On a restart or deployment, App Service starts the new container before it stops the old one. If both write `app.db`/`cwa.db` on `/home` at the same time, the databases can be corrupted. The deploy workflow therefore stops the app before deploying and starts it afterwards, which costs 1–2 minutes of downtime per deployment. For the same reason, prefer *Stop* then *Start* over *Restart* in the portal.
- **Startup integrity check.** At every start, `cwa-init` runs `scripts/db_integrity.py` on `app.db` and `cwa.db`:
  - A corrupted database is rebuilt with `VACUUM INTO`. The rebuilt copy is used only if it passes `integrity_check` with the same row counts, and the original is kept as `<db>.corrupt-<timestamp>`.
  - A database that can't be repaired without data loss is left unchanged, and every page shows a red banner until an administrator restores it from a backup.
  - The latest result is in `/config/.cwa_db_health.json`.
- **Performance.** Every library query and cover read goes over SMB. Large library pages are slower than on local disk.
- **In-flight uploads.** Uploads waiting in the local ingest folder are lost if the app restarts before they are imported.
- **Account keys.** App Service SMB mounts require the storage account key; SAS and managed identity aren't supported for mounts.
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

**When to update:** admins see a "CWA UPDATE AVAILABLE" banner (at most once a day) when [crocodilestick/Calibre-Web-Automated](https://github.com/crocodilestick/Calibre-Web-Automated/releases) publishes a release newer than the one this fork contains. Images are versioned `<newest upstream tag in the fork>-azure.<sha>`, and the running app re-checks GitHub daily, not just at container start. Requires "CWA update notifications" enabled in CWA Settings (default).

**How to update:**
```bash
git remote add upstream https://github.com/crocodilestick/Calibre-Web-Automated.git  # once
git fetch upstream --tags
git merge v4.x.y            # the release tag from the banner
git push                    # triggers build & deploy
```

Azure-specific changes are isolated so upstream releases merge cleanly:

- `infra/` (including this guide) and `.github/workflows/azure-deploy.yml`: new files.
- Upstream-only workflows (Docker Hub, Discord, translations) are skipped on forks with `if: github.repository == 'crocodilestick/Calibre-Web-Automated'`.
- Code changes, all inert unless the environment variable is set:
  - `CWA_TMP_CONVERSION_DIR` in `scripts/ingest_processor.py`, `scripts/convert_library.py` and `cps/cwa_functions.py`
  - `CWA_DISABLE_PROCESSED_BACKUPS` and `CWA_DISABLE_AUTO_CONVERT` in `scripts/cwa_db.py`
  - `init-cwa-persistent-config` s6 oneshot (runs before `init-migrations`; active only on App Service with `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`)
  - the App Service storage check and local-ingest ownership in `root/etc/s6-overlay/s6-rc.d/cwa-init/run`
- `Dockerfile` doesn't declare `VOLUME /config`: an anonymous volume would prevent the `/config` → `/home/config` symlink. Keep this when merging upstream Dockerfile changes.
