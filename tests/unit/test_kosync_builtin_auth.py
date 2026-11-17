# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""KOReader built-in progress sync (x-auth-user / x-auth-key) and KOReader-readable error statuses."""

import base64
import hashlib
import logging
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CPS_DIR = Path(__file__).resolve().parents[2] / "cps"


@pytest.fixture
def env(monkeypatch):
    """The real kosync blueprint and cps.ub on a bare Flask app, without initializing cps."""
    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import StaticPool
    from werkzeug.security import generate_password_hash

    for name in list(sys.modules):
        if name == "cps" or name.startswith("cps."):
            monkeypatch.delitem(sys.modules, name)

    def package(name, path=None):
        mod = types.ModuleType(name)
        if path is not None:
            mod.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    cps = package("cps", CPS_DIR)
    logger = package("cps.logger")
    logger.create = lambda *a, **k: logging.getLogger("cwa-test")
    cw_login = package("cps.cw_login")
    cw_login.AnonymousUserMixin = type("AnonymousUserMixin", (), {})
    cw_login.current_user = None
    cw_login.user_logged_in = types.SimpleNamespace(connect=lambda f: None)
    services = package("cps.services", CPS_DIR / "services")
    services.SyncToken = None
    services.hardcover = None
    services.ldap = None
    package("cps.kobo").push_reading_state_to_hardcover = lambda *a, **k: None
    package("cps.render_template").render_title_template = lambda *a, **k: ""
    package("cps.progress_syncing", CPS_DIR / "progress_syncing")
    package("cps.progress_syncing.protocols", CPS_DIR / "progress_syncing" / "protocols")
    package("cps.progress_syncing.models").KOSyncProgress = object
    state = types.SimpleNamespace(enabled=True)
    package("cps.progress_syncing.settings").is_koreader_sync_enabled = lambda: state.enabled
    cps.logger = logger
    cps.services = services
    cps.csrf = types.SimpleNamespace(exempt=lambda f: f)
    cps.config = types.SimpleNamespace(config_allow_reverse_proxy_header_login=False, config_login_type=0)
    cps.usermanagement = types.SimpleNamespace()

    import cps.ub as ub
    from cps.progress_syncing.protocols import kosync

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ub.Base.metadata.create_all(engine)
    monkeypatch.setattr(ub, "session", scoped_session(sessionmaker(bind=engine)))
    user = ub.User()
    user.name = "Reader"
    user.email = "reader@example.com"
    user.password = generate_password_hash("s3cret")
    ub.session.add(user)
    ub.session.commit()

    app = Flask(__name__)
    app.register_blueprint(kosync.kosync)
    return types.SimpleNamespace(ub=ub, user=user, client=app.test_client(), state=state)


def _key(password):
    return hashlib.md5(password.encode()).hexdigest()


def _headers(username="reader", password="s3cret"):
    return {"x-auth-user": username, "x-auth-key": _key(password),
            "Accept": "application/vnd.koreader.v1+json"}


def test_key_is_only_valid_after_login_and_until_the_password_changes(env):
    from werkzeug.security import generate_password_hash
    ub, user = env.ub, env.user

    assert not ub.verify_kosync_key(user, _key("s3cret"))
    ub.remember_kosync_key(user, "s3cret")
    assert ub.verify_kosync_key(user, _key("s3cret"))
    assert ub.verify_kosync_key(user, _key("s3cret").upper())
    assert not ub.verify_kosync_key(user, _key("wrong"))
    assert ub.session.get(ub.KOSyncKey, user.id).key_hash != _key("s3cret")  # stored salted

    user.password = generate_password_hash("n3w")  # e.g. an admin resets it
    ub.session.commit()
    assert not ub.verify_kosync_key(user, _key("s3cret"))
    ub.remember_kosync_key(user, "n3w")
    assert ub.verify_kosync_key(user, _key("n3w"))


def test_builtin_progress_sync_login(env):
    response = env.client.get("/kosync/users/auth", headers=_headers())
    assert response.status_code == 401
    assert "sign in to the web interface" in response.get_json()["message"]

    env.ub.remember_kosync_key(env.user, "s3cret")
    assert env.client.get("/kosync/users/auth", headers=_headers()).get_json() == {"authorized": "OK"}
    assert env.client.get("/kosync/users/auth", headers=_headers(password="wrong")).status_code == 401


def test_plugin_basic_auth_still_works(env):
    token = base64.b64encode(b"Reader:s3cret").decode()
    response = env.client.get("/kosync/users/auth", headers={"Authorization": "Basic " + token})
    assert response.status_code == 200


def test_errors_use_statuses_koreader_can_display(env):
    unauthorized = env.client.put("/kosync/syncs/progress", headers=_headers(),
                                  json={"document": "d", "progress": "p", "percentage": 0.5, "device": "x"})
    assert unauthorized.status_code == 401  # was 400, shown as "Unknown server error"

    register = env.client.post("/kosync/users/create", json={"username": "a", "password": "b"})
    assert register.status_code == 402
    assert "Calibre-Web Automated" in register.get_json()["message"]

    env.state.enabled = False
    disabled = env.client.get("/kosync/users/auth", headers=_headers())
    assert disabled.status_code == 401
    assert disabled.get_json()["message"] == "KOReader sync is disabled on this server"
