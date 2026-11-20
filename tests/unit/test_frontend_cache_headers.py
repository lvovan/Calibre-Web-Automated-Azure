# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import importlib.util
import sys
import types
from pathlib import Path

from flask import Flask


def _load_cache_buster(monkeypatch):
    package_name = "cwa_cache_buster_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[2] / "cps")]
    logger = types.ModuleType(f"{package_name}.logger")
    logger.create = lambda: types.SimpleNamespace()
    constants = types.ModuleType(f"{package_name}.constants")
    constants.BUILD_NUMBER = "build-123"
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, f"{package_name}.logger", logger)
    monkeypatch.setitem(sys.modules, f"{package_name}.constants", constants)
    path = Path(__file__).resolve().parents[2] / "cps" / "cache_buster.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.cache_buster", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_static_assets_receive_production_cache_headers(monkeypatch, tmp_path):
    (tmp_path / "site.css").write_text("body { color: black; }", encoding="utf-8")
    cache_buster = _load_cache_buster(monkeypatch)
    app = Flask(__name__, static_folder=str(tmp_path), static_url_path="/static")
    app.debug = False
    cache_buster.init_cache_busting(app)

    client = app.test_client()
    versioned = client.get("/static/site.css?q=build-123")
    unversioned = client.get("/static/site.css")

    assert "immutable" in versioned.headers["Cache-Control"]
    assert "max-age=31536000" in versioned.headers["Cache-Control"]
    assert "no-cache" not in versioned.headers["Cache-Control"]
    assert "immutable" not in unversioned.headers["Cache-Control"]
    assert "max-age=3600" in unversioned.headers["Cache-Control"]
    assert "no-cache" not in unversioned.headers["Cache-Control"]
