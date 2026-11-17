# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Cancelling an import from the Tasks page (ingest_processor side)."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def ingest_processor(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    monkeypatch.setenv("CWA_INGEST_BATCH_DIRTY_FILE", str(tmp_path / "batch_dirty"))
    import ingest_processor as module
    monkeypatch.setattr(module, "INGEST_CANCEL_DIR", str(tmp_path / "cancel"))
    return module


def test_cancel_watch_detects_request_for_original_or_truncated_name(ingest_processor, tmp_path):
    watch = ingest_processor.CancelWatch(["/ingest/long_name.epub", "/ingest/short.epub"])
    watch.check()  # no request: nothing happens
    (tmp_path / "cancel").mkdir()
    (tmp_path / "cancel" / "short.epub").write_text("x")
    with pytest.raises(ingest_processor.IngestCancelled):
        watch.check()
    watch.clear_requests()
    assert not list((tmp_path / "cancel").iterdir())


def test_only_created_books_are_rolled_back(ingest_processor, tmp_path, monkeypatch):
    parse = ingest_processor.NewBookProcessor._parse_new_book_ids
    assert parse("Merged book ids: 3\nAdded book ids: 7, 8") == [7, 8]
    assert parse("Merged book ids: 3") == []

    con = sqlite3.connect(tmp_path / "cwa.db")
    con.execute("CREATE TABLE cwa_import(timestamp TEXT, filename TEXT, original_backed_up TEXT)")
    con.execute("INSERT INTO cwa_import VALUES ('t', 'book', 'True')")
    con.commit()
    backup = tmp_path / "book.epub"
    backup.write_text("x")

    processor = object.__new__(ingest_processor.NewBookProcessor)
    processor.library_dir = "/calibre-library"
    processor.calibre_env = {}
    processor.new_book_ids = [7, 8]
    processor.last_added_book_id = 8
    processor.imported_backup = str(backup)
    processor.imported_stem = "book"
    processor.db = type("Db", (), {"con": con, "cur": con.cursor()})()
    calls = []
    monkeypatch.setattr(ingest_processor.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))

    processor.rollback_import()

    assert calls == [["calibredb", "remove", "--permanent", "7,8", "--library-path=/calibre-library"]]
    assert not backup.exists()
    assert con.execute("SELECT COUNT(*) FROM cwa_import").fetchone()[0] == 0
    con.close()
