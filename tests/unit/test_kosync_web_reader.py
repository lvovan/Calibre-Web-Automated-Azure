# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Per-user web reader progress stored with KOReader sync progress (/kosync/reader/<book_id>)."""

import logging
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CPS_DIR = Path(__file__).resolve().parents[2] / "cps"
BOOK_ID = 42
HIDDEN_BOOK_ID = 43


@pytest.fixture
def env(monkeypatch):
    """The real kosync blueprint, cps.ub and progress models on a bare Flask app."""
    from flask import Flask
    from flask_babel import Babel
    from sqlalchemy import create_engine
    from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
    from sqlalchemy.pool import StaticPool

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
    hardcover_calls = []
    package("cps.kobo").push_reading_state_to_hardcover = lambda user, book, pct: hardcover_calls.append(pct)
    package("cps.render_template").render_title_template = lambda *a, **k: ""
    package("cps.db").Base = declarative_base()
    package("cps.progress_syncing", CPS_DIR / "progress_syncing")
    package("cps.progress_syncing.protocols", CPS_DIR / "progress_syncing" / "protocols")
    state = types.SimpleNamespace(enabled=True)
    package("cps.progress_syncing.settings").is_koreader_sync_enabled = lambda: state.enabled
    cps.logger = logger
    cps.services = services
    cps.csrf = types.SimpleNamespace(exempt=lambda f: f)
    cps.config = types.SimpleNamespace(config_allow_reverse_proxy_header_login=False, config_login_type=0)
    cps.usermanagement = types.SimpleNamespace(user_login_required=lambda f: f)

    from cps import ub
    from cps.progress_syncing import models
    sys.modules["cps.db"].BookFormatChecksum = models.BookFormatChecksum
    from sqlalchemy import Column, Integer, Table
    Table("books", sys.modules["cps.db"].Base.metadata, Column("id", Integer, primary_key=True))
    from cps.progress_syncing.protocols import kosync

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ub.Base.metadata.create_all(engine)
    sys.modules["cps.db"].Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(ub, "session", session)
    users = []
    for name in ("alice", "bob"):
        user = ub.User()
        user.name = name
        user.email = name + "@example.com"
        user.password = "x"
        session.add(user)
        session.commit()
        users.append(types.SimpleNamespace(id=user.id, is_authenticated=True, is_anonymous=False))

    books = {BOOK_ID: types.SimpleNamespace(id=BOOK_ID, title="One Piece 1")}
    cps.calibre_db = types.SimpleNamespace(
        session=session,
        get_filtered_book=lambda book_id: books.get(book_id),
        get_book=lambda book_id: books.get(book_id))
    read_status = []
    monkeypatch.setattr(kosync, "update_book_read_status",
                        lambda user, book_id, pct: read_status.append((user.id, book_id, pct)))

    app = Flask(__name__)
    Babel(app)
    app.register_blueprint(kosync.kosync)

    def login(user):
        cw_login.current_user = user

    login(users[0])
    return types.SimpleNamespace(kosync=kosync, models=models, session=session, users=users, login=login,
                                 client=app.test_client(), app=app, state=state, hardcover=hardcover_calls,
                                 read_status=read_status)


def _url(book_id=BOOK_ID):
    return f"/kosync/reader/{book_id}"


def _put(env, progress="/body/DocFragment[3]/body/p[2]/text().5", percentage=0.25, **extra):
    return env.client.put(_url(), json={"progress": progress, "percentage": percentage,
                                        "device_id": "browser-1", **extra})


def test_progress_round_trip_is_per_user(env):
    assert env.client.get(_url()).get_json() == {}

    saved = _put(env)
    assert saved.status_code == 200
    body = saved.get_json()
    assert body["progress"] == "/body/DocFragment[3]/body/p[2]/text().5"
    assert body["percentage"] == pytest.approx(0.25)
    assert body["device"] == "CWA Web Reader"
    assert body["device_id"] == "browser-1"
    assert body["timestamp"] == pytest.approx(datetime.now(timezone.utc).timestamp(), abs=5)
    assert env.client.get(_url()).get_json() == body

    record = env.session.query(env.models.KOSyncProgress).one()
    assert (record.document, record.percentage) == (str(BOOK_ID), pytest.approx(25.0))
    assert env.read_status == [(env.users[0].id, BOOK_ID, pytest.approx(25.0))]

    env.login(env.users[1])
    assert env.client.get(_url()).get_json() == {}
    _put(env, progress="7", percentage=0.5)
    env.login(env.users[0])
    assert env.client.get(_url()).get_json()["progress"] == "/body/DocFragment[3]/body/p[2]/text().5"
    assert env.session.query(env.models.KOSyncProgress).count() == 2


def test_reads_and_updates_progress_a_device_stored_by_checksum(env):
    env.session.add(env.models.BookFormatChecksum(book=BOOK_ID, format="EPUB", checksum="abc123"))
    env.session.add(env.models.KOSyncProgress(user_id=env.users[0].id, document="abc123", progress="12",
                                              percentage=60.0, device="Kobo", device_id="kobo-1",
                                              timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)))
    env.session.commit()

    state = env.client.get(_url()).get_json()
    assert (state["progress"], state["device"], state["percentage"]) == ("12", "Kobo", pytest.approx(0.6))
    assert state["timestamp"] == datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()

    _put(env, progress="13", percentage=0.65)
    records = env.session.query(env.models.KOSyncProgress).all()
    assert [(r.document, r.progress) for r in records] == [("abc123", "13")]


def test_hardcover_is_only_pushed_when_the_whole_percent_changes(env):
    _put(env, percentage=0.251)
    _put(env, percentage=0.254)
    _put(env, percentage=0.26)
    assert env.hardcover == [25, 26]


@pytest.mark.parametrize("payload", [
    {"percentage": 0.5},
    {"progress": "", "percentage": 0.5},
    {"progress": "x" * 256, "percentage": 0.5},
    {"progress": "1", "percentage": 1.5},
    {"progress": "1", "percentage": -0.1},
    {"progress": "1", "percentage": True},
    {"progress": "1", "percentage": "0.5"},
    {"progress": "1", "percentage": 0.5, "device": "d" * 101},
    {"progress": "1", "percentage": 0.5, "device_id": "d" * 101},
])
def test_invalid_progress_is_rejected(env, payload):
    assert env.client.put(_url(), json=payload).status_code == 400
    assert env.session.query(env.models.KOSyncProgress).count() == 0


def test_hidden_books_and_disabled_sync_are_not_found(env):
    assert env.client.get(_url(HIDDEN_BOOK_ID)).status_code == 404
    assert env.client.put(_url(HIDDEN_BOOK_ID), json={"progress": "1", "percentage": 0.1}).status_code == 404

    env.state.enabled = False
    assert env.client.get(_url()).status_code == 404
    assert _put(env).status_code == 404


def test_reader_config_is_only_provided_when_sync_is_available(env):
    _put(env)
    with env.app.test_request_context():
        config = env.kosync.reader_progress_config(BOOK_ID)
        assert config["url"] == _url()
        assert config["state"]["progress"] == "/body/DocFragment[3]/body/p[2]/text().5"
        assert "{percent}" in config["messages"]["remote"] and "{device}" in config["messages"]["remote"]

        env.login(types.SimpleNamespace(id=0, is_authenticated=True, is_anonymous=True))
        assert env.kosync.reader_progress_config(BOOK_ID) is None
        env.login(env.users[0])
        env.state.enabled = False
        assert env.kosync.reader_progress_config(BOOK_ID) is None

def _koreader_record(env, document, progress="/body/DocFragment[6]/body/p[18]/text().477", when=2025, user=0):
    env.session.add(env.models.KOSyncProgress(
        user_id=env.users[user].id, document=document, progress=progress, percentage=11.88,
        device="KindleOasis2", device_id="kindle-1", timestamp=datetime(when, 1, 1, tzinfo=timezone.utc)))
    env.session.commit()


@pytest.fixture
def linking(env, monkeypatch):
    """A book with an EPUB file and a checksum store that records links in the checksum table."""
    env.kosync_book = types.SimpleNamespace(id=BOOK_ID, title="One Piece 1",
                                            data=[types.SimpleNamespace(format="EPUB")])
    import cps
    cps.calibre_db.get_filtered_book = lambda book_id: env.kosync_book if book_id == BOOK_ID else None
    checksums = types.ModuleType("cps.progress_syncing.checksums")

    def store_checksum(book_id, book_format, checksum):
        env.session.add(env.models.BookFormatChecksum(book=book_id, format=book_format, checksum=checksum))
        env.session.commit()
        return True

    checksums.store_checksum = store_checksum
    monkeypatch.setitem(sys.modules, "cps.progress_syncing.checksums", checksums)
    return env


def test_reader_config_offers_only_newer_unmatched_koreader_progress(linking):
    env = linking
    env.session.add(env.models.BookFormatChecksum(book=7, format="EPUB", checksum="known"))
    env.session.commit()
    _koreader_record(env, "known")
    _koreader_record(env, "stale", when=2020)
    _koreader_record(env, "pdfpage", progress="12")
    _koreader_record(env, "otheruser", user=1)
    _koreader_record(env, "kindlehash")
    env.session.add(env.models.KOSyncProgress(user_id=env.users[0].id, document=str(BOOK_ID), progress="3",
                                              percentage=5.0, device="CWA Web Reader", device_id="web",
                                              timestamp=datetime(2022, 1, 1, tzinfo=timezone.utc)))
    env.session.commit()

    with env.app.test_request_context():
        config = env.kosync.reader_progress_config(BOOK_ID, "epub")
        assert [c["document"] for c in config["candidates"]] == ["kindlehash"]
        assert config["candidates"][0]["percentage"] == pytest.approx(0.1188)
        assert config["linkUrl"] == _url() + "/link"
        assert config["format"] == "EPUB"
        assert env.kosync.reader_progress_config(BOOK_ID, "pdf")["candidates"] == []


def test_linking_attaches_the_koreader_hash_so_progress_is_shared(linking):
    env = linking
    _koreader_record(env, "kindlehash")
    link = _url() + "/link"

    assert env.client.post(link, json={"document": "unknown", "format": "EPUB"}).status_code == 404
    assert env.client.post(link, json={"document": "kindlehash", "format": "PDF"}).status_code == 400
    assert env.client.post(link, json={"format": "EPUB"}).status_code == 400
    assert env.client.post(_url(HIDDEN_BOOK_ID) + "/link",
                           json={"document": "kindlehash", "format": "EPUB"}).status_code == 404
    env.login(env.users[1])
    assert env.client.post(link, json={"document": "kindlehash", "format": "EPUB"}).status_code == 404
    env.login(env.users[0])

    linked = env.client.post(link, json={"document": "kindlehash", "format": "epub"})
    assert linked.status_code == 200
    assert linked.get_json()["device"] == "KindleOasis2"
    assert env.client.get(_url()).get_json()["progress"] == "/body/DocFragment[6]/body/p[18]/text().477"
    with env.app.test_request_context():
        assert env.kosync.reader_progress_config(BOOK_ID, "epub")["candidates"] == []
