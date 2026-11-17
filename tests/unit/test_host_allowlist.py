# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import importlib.util
from pathlib import Path

import pytest

# Load the module directly: importing the cps package starts the whole app.
_spec = importlib.util.spec_from_file_location(
    "host_allowlist", Path(__file__).resolve().parents[2] / "cps" / "host_allowlist.py")
host_allowlist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_allowlist)

pytestmark = pytest.mark.unit


def _app(environ, start_response):
    start_response("200 OK", [])
    return [b"app"]


def _call(wsgi, host, path="/", **extra):
    status = {}
    environ = {"HTTP_HOST": host, "PATH_INFO": path, **extra}
    body = wsgi(environ, lambda s, h: status.setdefault("s", s))
    return status["s"], b"".join(body)


@pytest.fixture
def wsgi(monkeypatch):
    monkeypatch.setenv(host_allowlist.ENV_VAR, "Books.Example.com")
    return host_allowlist.wrap(_app)


@pytest.mark.parametrize("host", ["books.example.com", "BOOKS.example.com:443", "books.example.com.",
                                  "localhost:8083", "127.0.0.1", "[::1]:8083"])
def test_allowed_hosts_pass(wsgi, host):
    assert _call(wsgi, host) == ("200 OK", b"app")


@pytest.mark.parametrize("host", ["app-cwa.azurewebsites.net", "", "books.example.com.evil.net"])
def test_other_hosts_get_404(wsgi, host):
    assert _call(wsgi, host)[0] == "404 Not Found"


def test_forwarded_host_cannot_bypass(wsgi):
    status, _ = _call(wsgi, "app-cwa.azurewebsites.net", HTTP_X_FORWARDED_HOST="books.example.com")
    assert status == "404 Not Found"


def test_health_exempt_on_any_host(wsgi):
    assert _call(wsgi, "app-cwa.azurewebsites.net", "/health")[0] == "200 OK"
    assert _call(wsgi, "app-cwa.azurewebsites.net", "/health/x")[0] == "404 Not Found"


def test_disabled_when_unset(monkeypatch):
    monkeypatch.delenv(host_allowlist.ENV_VAR, raising=False)
    assert host_allowlist.wrap(_app) is _app
