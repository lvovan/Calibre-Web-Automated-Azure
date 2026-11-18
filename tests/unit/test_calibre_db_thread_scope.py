# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The global web CalibreDB must never share a Session with the worker thread.

setup_db() (run by TaskReconnectDatabase on the worker thread after every ingest)
re-inits the session of every CalibreDB instance from the calling thread. The web
instance then held the worker thread's scoped session, which worker tasks also got
from CalibreDB(init=True) and closed, detaching books mid-delete in web requests.
"""

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from cps import db

pytestmark = pytest.mark.unit


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://")
    scoped = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(db.CalibreDB, "session_factory", scoped)
    monkeypatch.setattr(db.CalibreDB, "create_functions", lambda self, config=None: None)
    yield scoped
    scoped.remove()


def _in_thread(fn):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("value", fn()))
    t.start()
    t.join()
    return result["value"]


def test_reinit_from_worker_thread_does_not_pin_worker_session(factory):
    web = db.CalibreDB(thread_scoped=True)
    main_session = web.session

    # What setup_db() does for every instance when a reconnect runs on the worker thread.
    worker_session = _in_thread(lambda: (web.init_session(), web.session)[1])

    assert worker_session is not main_session
    assert web.session is main_session


def test_worker_task_closing_its_session_does_not_touch_web_session(factory):
    web = db.CalibreDB(thread_scoped=True)
    web.session.close()
    web_session = web.session

    def worker_task():
        web.init_session()  # reconnect on the worker thread
        task_db = db.CalibreDB(init=False)
        task_db.init_session()
        shared = task_db.session is web.session
        task_db.session.close()
        return shared

    # Within the worker thread the task and web instance resolve the same thread-local session,
    assert _in_thread(worker_task) is True
    # but the web thread keeps its own.
    assert web.session is web_session


def test_explicit_session_assignment_is_kept(factory):
    web = db.CalibreDB(thread_scoped=True)
    fake = object()
    web.session = fake
    assert web.session is fake
    web.session = None
    assert web.session is factory()


def test_dispose_clears_factory_so_ensure_session_waits_for_rebuild(factory, monkeypatch):
    web = db.CalibreDB(thread_scoped=True)
    monkeypatch.setattr(db.CalibreDB, "instances", type(db.CalibreDB.instances)())
    db.CalibreDB.instances.add(web)
    monkeypatch.setattr(db, "cc_classes", {})
    db.CalibreDB.dispose()
    assert db.CalibreDB.session_factory is None
    assert web.session is None
