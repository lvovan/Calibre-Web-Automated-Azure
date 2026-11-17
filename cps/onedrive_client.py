# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Microsoft identity platform (consumer accounts) and Microsoft Graph client for the OneDrive import.

Kept free of Flask and database imports: token persistence is injected, so the
module can be used from request handlers, the import queue threads and tests.
"""

import base64
import hashlib
import os
import re
import secrets
import threading
import time
from urllib.parse import quote, urlencode

import requests

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "Files.Read User.Read offline_access"

# Formats cwa-ingest-service can import (scripts/ingest_processor.py)
CWA_INGEST_FORMATS = frozenset((
    "epub", "mobi", "azw", "azw3", "azw4", "pdf", "txt", "cbz", "cbr", "cb7", "cbc", "fb2", "fbz", "docx",
    "html", "htmlz", "lit", "lrf", "odt", "prc", "pdb", "pml", "rb", "snb", "tcr", "txtz", "kepub", "acsm",
    "m4b", "m4a",
))

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9!._\-]{1,256}$")
_SELECT = "id,name,folder,file,size"
_PAGE_SIZE = 200
_TIMEOUT = (10, 60)
_MAX_ATTEMPTS = 5
_MAX_RETRY_AFTER = 60


class OneDriveError(Exception):
    pass


class OneDriveAuthError(OneDriveError):
    """The stored connection is missing, revoked or expired: the user must reconnect."""


def settings():
    client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ONEDRIVE_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("ONEDRIVE_REDIRECT_URI", "").strip()
    return client_id, client_secret, redirect_uri


def is_configured():
    client_id, client_secret, __ = settings()
    return bool(client_id and client_secret)


def valid_item_id(item_id):
    return isinstance(item_id, str) and bool(_ITEM_ID_RE.match(item_id))


def valid_cursor(cursor):
    return isinstance(cursor, str) and cursor.startswith(GRAPH + "/") and len(cursor) < 4096


def extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def content_hash(item):
    hashes = (item.get("file") or {}).get("hashes") or {}
    for key, prefix in (("quickXorHash", "qx"), ("sha256Hash", "sha256"), ("sha1Hash", "sha1")):
        if hashes.get(key):
            return "{}:{}".format(prefix, hashes[key].lower() if prefix != "qx" else hashes[key])
    return None


# ── OAuth 2.0 authorization code flow with PKCE ──────────────────────────────

def new_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return verifier, challenge.decode("ascii")


def authorize_url(redirect_uri, state, code_challenge):
    client_id, __, __ = settings()
    return AUTHORITY + "/authorize?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })


def _token_request(data):
    client_id, client_secret, __ = settings()
    data = dict(data, client_id=client_id, client_secret=client_secret, scope=SCOPES)
    try:
        resp = requests.post(AUTHORITY + "/token", data=data, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise OneDriveError("Microsoft sign-in is unreachable: {}".format(e))
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code == 400 and payload.get("error") in ("invalid_grant", "interaction_required"):
        raise OneDriveAuthError(payload.get("error_description") or payload["error"])
    if resp.status_code >= 400 or "access_token" not in payload:
        raise OneDriveError("Token request failed ({}): {}".format(
            resp.status_code, payload.get("error_description") or payload.get("error") or resp.text[:200]))
    return payload


def exchange_code(code, redirect_uri, code_verifier):
    """Returns (access_token, expires_in, refresh_token)."""
    payload = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    })
    if not payload.get("refresh_token"):
        raise OneDriveError("Microsoft did not return a refresh token (offline_access not granted)")
    return payload["access_token"], int(payload.get("expires_in", 3600)), payload["refresh_token"]


class TokenManager:
    """Per-user access tokens, refreshed from the stored refresh token.

    load_refresh_token(user_id) -> str or None
    save_refresh_token(user_id, token) persists a rotated refresh token.
    """

    def __init__(self, load_refresh_token, save_refresh_token):
        self._load = load_refresh_token
        self._save = save_refresh_token
        self._cache = {}
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _lock(self, user_id):
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def invalidate(self, user_id):
        self._cache.pop(user_id, None)

    def seed(self, user_id, access_token, expires_in):
        self._cache[user_id] = (access_token, time.monotonic() + expires_in)

    def get(self, user_id, force_refresh=False):
        with self._lock(user_id):
            cached = self._cache.get(user_id)
            if cached and not force_refresh and cached[1] - time.monotonic() > 120:
                return cached[0]
            refresh_token = self._load(user_id)
            if not refresh_token:
                self._cache.pop(user_id, None)
                raise OneDriveAuthError("OneDrive is not connected")
            try:
                payload = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
            except OneDriveAuthError:
                self._cache.pop(user_id, None)
                raise
            rotated = payload.get("refresh_token")
            if rotated and rotated != refresh_token:
                self._save(user_id, rotated)
            self._cache[user_id] = (payload["access_token"],
                                    time.monotonic() + int(payload.get("expires_in", 3600)))
            return payload["access_token"]


# ── Microsoft Graph ──────────────────────────────────────────────────────────

class GraphClient:
    def __init__(self, tokens, user_id, sleep=time.sleep):
        self._tokens = tokens
        self._user_id = user_id
        self._sleep = sleep

    def get(self, url, params=None, stream=False):
        refreshed = False
        for attempt in range(_MAX_ATTEMPTS):
            token = self._tokens.get(self._user_id, force_refresh=refreshed)
            try:
                resp = requests.get(url, params=params, stream=stream, timeout=_TIMEOUT,
                                    headers={"Authorization": "Bearer " + token})
            except requests.RequestException as e:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise OneDriveError("OneDrive is unreachable: {}".format(e))
                self._sleep(2 ** attempt)
                continue
            if resp.status_code == 401 and not refreshed:
                resp.close()
                refreshed = True
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < _MAX_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After", "")
                resp.close()
                delay = int(retry_after) if retry_after.isdigit() else 2 ** attempt
                self._sleep(min(delay, _MAX_RETRY_AFTER))
                continue
            if resp.status_code == 401:
                resp.close()
                raise OneDriveAuthError("OneDrive rejected the access token")
            if resp.status_code >= 400:
                detail = resp.text[:200] if not stream else ""
                resp.close()
                raise OneDriveError("OneDrive request failed ({}) {}".format(resp.status_code, detail).strip())
            return resp
        raise OneDriveError("OneDrive request failed after retries")

    def me(self):
        data = self.get(GRAPH + "/me", params={"$select": "displayName,userPrincipalName,mail"}).json()
        return data.get("displayName") or "", data.get("mail") or data.get("userPrincipalName") or ""

    def children_page(self, item_id=None, cursor=None):
        """One page of a folder listing: (raw items, next cursor or None)."""
        if cursor:
            if not valid_cursor(cursor):
                raise OneDriveError("Invalid cursor")
            resp = self.get(cursor)
        else:
            if item_id:
                if not valid_item_id(item_id):
                    raise OneDriveError("Invalid item id")
                url = GRAPH + "/me/drive/items/{}/children".format(quote(item_id, safe="!"))
            else:
                url = GRAPH + "/me/drive/root/children"
            resp = self.get(url, params={"$top": _PAGE_SIZE, "$select": _SELECT})
        data = resp.json()
        return data.get("value", []), data.get("@odata.nextLink")

    def item(self, item_id):
        if not valid_item_id(item_id):
            raise OneDriveError("Invalid item id")
        return self.get(GRAPH + "/me/drive/items/{}".format(quote(item_id, safe="!")),
                        params={"$select": _SELECT}).json()

    def walk_files(self, folder_id, extensions, should_stop=lambda: False):
        """Files with one of `extensions` under folder_id, recursively, depth-first in name order."""
        entries = []
        cursor = None
        while True:
            if should_stop():
                return
            page, cursor = self.children_page(folder_id, cursor)
            entries.extend(page)
            if not cursor:
                break
        entries.sort(key=lambda e: (e.get("name") or "").casefold())
        for entry in entries:
            if should_stop():
                return
            if "folder" in entry:
                yield from self.walk_files(entry["id"], extensions, should_stop)
            elif "file" in entry and extension(entry.get("name", "")) in extensions:
                yield entry

    def download(self, item_id, dest_path, should_stop=lambda: False, chunk_size=1024 * 1024):
        """Streams the file content to dest_path. Returns False if stopped before completion."""
        if not valid_item_id(item_id):
            raise OneDriveError("Invalid item id")
        # Graph answers with a redirect to a pre-authenticated URL; requests drops the
        # Authorization header when the redirect leaves graph.microsoft.com.
        resp = self.get(GRAPH + "/me/drive/items/{}/content".format(quote(item_id, safe="!")), stream=True)
        try:
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if should_stop():
                        return False
                    if chunk:
                        f.write(chunk)
        finally:
            resp.close()
        return True


def describe(entry, allowed_formats):
    """Browser-facing view of a Graph driveItem."""
    is_folder = "folder" in entry
    return {
        "id": entry.get("id"),
        "name": entry.get("name", ""),
        "folder": is_folder,
        "childCount": (entry.get("folder") or {}).get("childCount", 0) if is_folder else None,
        "size": entry.get("size", 0),
        "importable": bool(allowed_formats) if is_folder
        else ("file" in entry and extension(entry.get("name", "")) in allowed_formats),
    }
