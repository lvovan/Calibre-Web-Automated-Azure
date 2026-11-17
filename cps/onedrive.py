# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Import books from OneDrive (personal Microsoft accounts).

Enabled when ONEDRIVE_CLIENT_ID and ONEDRIVE_CLIENT_SECRET are set. The browser
only talks to these routes; Microsoft Graph is called server-side with a
per-user refresh token stored Fernet-encrypted in app.db.
"""

import hmac
import secrets
import threading
from types import SimpleNamespace
from urllib.parse import urlparse, urlunparse

from flask import Blueprint, abort, flash, jsonify, redirect, request, session as flask_session, url_for
from flask_babel import gettext as _
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

from . import config, logger, ub
from . import onedrive_client as client
from .cw_login import current_user
from .render_template import render_title_template
from .usermanagement import user_login_required

log = logger.create()

onedrive = Blueprint('onedrive', __name__)

MAX_SELECTION = 1000
_SESSION_KEY = "onedrive_oauth"

_session_maker = None
_queue = None
_init_lock = threading.Lock()


def is_enabled():
    return client.is_configured()


def allowed_formats():
    configured = {f.strip().lower() for f in (config.config_upload_formats or "").split(",") if f.strip()}
    return (configured & client.CWA_INGEST_FORMATS) if configured else set(client.CWA_INGEST_FORMATS)


def _db_session():
    global _session_maker
    with _init_lock:
        if _session_maker is None:
            engine = create_engine("sqlite:///{}".format(ub.app_DB_path), connect_args={"timeout": 30})
            _session_maker = sessionmaker(bind=engine)
    return _session_maker()


def _load_refresh_token(user_id):
    session = _db_session()
    try:
        conn = session.query(ub.OneDriveConnection).filter(ub.OneDriveConnection.user_id == user_id).first()
        if conn is None:
            return None
        try:
            return config._fernet.decrypt(conn.refresh_token.encode()).decode()
        except Exception:
            log.warning("OneDrive: stored token for user %s cannot be decrypted", user_id)
            return None
    finally:
        session.close()


def _save_refresh_token(user_id, token, account_name=None, account_email=None):
    session = _db_session()
    try:
        conn = session.query(ub.OneDriveConnection).filter(ub.OneDriveConnection.user_id == user_id).first()
        if conn is None:
            conn = ub.OneDriveConnection(user_id=user_id)
            session.add(conn)
        conn.refresh_token = config._fernet.encrypt(token.encode()).decode()
        if account_name is not None:
            conn.account_name = account_name
        if account_email is not None:
            conn.account_email = account_email
        session.commit()
    finally:
        session.close()


tokens = client.TokenManager(_load_refresh_token, _save_refresh_token)


def _new_ingest_path(name, user_id):
    from .editbooks import _get_ingest_path
    safe = secure_filename(name)
    ext = client.extension(name)
    if client.extension(safe) != ext:  # e.g. names without any ASCII characters
        safe = "book." + ext
    return _get_ingest_path(SimpleNamespace(filename=safe), prefix_parts=["onedrive", user_id])


def _ingest_dir():
    from .cwa_functions import get_ingest_dir
    return get_ingest_dir()


def get_queue():
    global _queue
    with _init_lock:
        if _queue is None:
            from .onedrive_queue import ImportQueue
            _queue = ImportQueue(tokens, _db_session, _ingest_dir, _new_ingest_path, allowed_formats)
    return _queue


def start_queue():
    if is_enabled():
        get_queue().start()


@onedrive.app_context_processor
def _inject_enabled():
    return {"onedrive_enabled": is_enabled()}


# ── helpers ──────────────────────────────────────────────────────────────────

def _guard():
    if not is_enabled():
        abort(404)
    if not (current_user.role_upload() and config.config_uploading):
        abort(403)


def _redirect_uri():
    __, __, configured = client.settings()
    return configured or url_for("onedrive.callback", _external=True)


def _canonical_host_redirect():
    """The OAuth redirect URI is registered for one host; the session cookie must live on that host."""
    __, __, configured = client.settings()
    host = urlparse(configured).netloc if configured else ""
    if host and request.host.lower() != host.lower():
        target = urlparse(request.url)._replace(scheme=urlparse(configured).scheme, netloc=host)
        return redirect(urlunparse(target))
    return None


def _connection():
    return ub.session.query(ub.OneDriveConnection).filter(ub.OneDriveConnection.user_id == current_user.id).first()


def _api_error(e):
    if isinstance(e, client.OneDriveAuthError):
        return jsonify({"error": _("Your OneDrive connection has expired. Please reconnect."), "reconnect": True}), 401
    log.warning("OneDrive request failed: %s", e)
    return jsonify({"error": _("OneDrive request failed. Please try again.")}), 502


# ── pages ────────────────────────────────────────────────────────────────────

@onedrive.route("/onedrive")
@user_login_required
def page():
    _guard()
    redirect_resp = _canonical_host_redirect()
    if redirect_resp:
        return redirect_resp
    formats = allowed_formats()
    return render_title_template("onedrive.html", title=_("Import from OneDrive"), page="onedrive",
                                 connection=_connection(), formats=sorted(formats),
                                 folders_allowed="epub" in formats)


@onedrive.route("/onedrive/connect")
@user_login_required
def connect():
    _guard()
    redirect_resp = _canonical_host_redirect()
    if redirect_resp:
        return redirect_resp
    verifier, challenge = client.new_pkce()
    state = secrets.token_urlsafe(32)
    redirect_uri = _redirect_uri()
    flask_session[_SESSION_KEY] = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri}
    return redirect(client.authorize_url(redirect_uri, state, challenge))


@onedrive.route("/onedrive/callback")
@user_login_required
def callback():
    _guard()
    pending = flask_session.pop(_SESSION_KEY, None)
    if request.args.get("error"):
        log.info("OneDrive sign-in not completed: %s", request.args.get("error_description") or request.args["error"])
        flash(_("OneDrive was not connected: sign-in was cancelled or denied."), category="error")
        return redirect(url_for("onedrive.page"))
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not pending or not code or not hmac.compare_digest(state, pending.get("state", "")):
        flash(_("OneDrive sign-in expired or is invalid. Please try again."), category="error")
        return redirect(url_for("onedrive.page"))
    try:
        access_token, expires_in, refresh_token = client.exchange_code(code, pending["redirect_uri"],
                                                                       pending["verifier"])
        _save_refresh_token(current_user.id, refresh_token)
        tokens.seed(current_user.id, access_token, expires_in)
        try:
            name, email = client.GraphClient(tokens, current_user.id).me()
            _save_refresh_token(current_user.id, refresh_token, name, email)
        except client.OneDriveError as e:
            log.warning("OneDrive: could not read the account profile: %s", e)
    except client.OneDriveError as e:
        log.error("OneDrive: token exchange failed: %s", e)
        flash(_("OneDrive could not be connected. Please try again."), category="error")
        return redirect(url_for("onedrive.page"))
    flash(_("OneDrive connected."), category="success")
    return redirect(url_for("onedrive.page"))


@onedrive.route("/onedrive/disconnect", methods=["POST"])
@user_login_required
def disconnect():
    _guard()
    ub.session.query(ub.OneDriveConnection).filter(ub.OneDriveConnection.user_id == current_user.id).delete()
    ub.session_commit()
    tokens.invalidate(current_user.id)
    flash(_("OneDrive disconnected."), category="success")
    return redirect(url_for("onedrive.page"))


# ── JSON API ─────────────────────────────────────────────────────────────────

@onedrive.route("/onedrive/api/children")
@user_login_required
def api_children():
    _guard()
    item_id = request.args.get("item") or None
    cursor = request.args.get("cursor") or None
    if (item_id and not client.valid_item_id(item_id)) or (cursor and not client.valid_cursor(cursor)):
        return jsonify({"error": _("Invalid request")}), 400
    try:
        page, next_cursor = client.GraphClient(tokens, current_user.id).children_page(item_id, cursor)
    except client.OneDriveError as e:
        return _api_error(e)
    formats = allowed_formats()
    return jsonify({"items": [client.describe(entry, formats) for entry in page], "next": next_cursor})


@onedrive.route("/onedrive/api/import", methods=["POST"])
@user_login_required
def api_import():
    _guard()
    payload = request.get_json(silent=True) or {}
    raw = payload.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SELECTION:
        return jsonify({"error": _("Select between 1 and %(max)s items", max=MAX_SELECTION)}), 400
    selection = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict) or not client.valid_item_id(entry.get("id")):
            return jsonify({"error": _("Invalid request")}), 400
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        selection.append({"id": entry["id"], "name": str(entry.get("name") or "")[:255],
                          "folder": bool(entry.get("folder"))})
    if _connection() is None:
        return jsonify({"error": _("Your OneDrive connection has expired. Please reconnect."), "reconnect": True}), 401
    job_id = get_queue().submit(current_user.id, current_user.name, selection)
    return jsonify({"job": job_id, "tasks_url": url_for("tasks.get_tasks_status")})


@onedrive.route("/onedrive/api/jobs")
@user_login_required
def api_jobs():
    _guard()
    return jsonify({"jobs": get_queue().jobs_for_user(current_user.id)})


@onedrive.route("/onedrive/api/jobs/<int:job_id>/cancel", methods=["POST"])
@user_login_required
def api_cancel(job_id):
    _guard()
    if not get_queue().cancel(job_id, user_id=current_user.id):
        return jsonify({"error": _("This import can no longer be cancelled")}), 409
    return jsonify({"ok": True})
