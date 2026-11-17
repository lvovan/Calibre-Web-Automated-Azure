# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import json
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import db_integrity  # noqa: E402

PAGE = 4096


def _make_db(path, rows=400):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA page_size={PAGE}")
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, blob TEXT)")
    conn.executemany("INSERT INTO user (name) VALUES (?)", [(f"user{i}",) for i in range(5)])
    conn.executemany("INSERT INTO item (blob) VALUES (?)", [("x" * 500,) for _ in range(rows)])
    conn.commit()
    conn.execute("DELETE FROM item WHERE id > 100")  # leaves free pages behind
    conn.commit()
    conn.close()


def _corrupt_freelist(path):
    with open(path, "r+b") as f:
        f.seek(32)
        trunk = struct.unpack(">I", f.read(4))[0]
        assert trunk, "test database has no freelist"
        f.seek((trunk - 1) * PAGE + 4)
        f.write(struct.pack(">I", 100000))


def _corrupt_table(path):
    conn = sqlite3.connect(path)
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='item'").fetchone()[0]
    conn.close()
    with open(path, "r+b") as f:
        f.seek((root - 1) * PAGE)
        f.write(b"\xff" * PAGE)


def test_healthy_database_is_left_alone(tmp_path):
    db = tmp_path / "app.db"
    _make_db(db)
    before = db.read_bytes()

    result = db_integrity.check_database(str(db))

    assert result["status"] == db_integrity.OK
    assert db.read_bytes() == before
    assert not list(tmp_path.glob("app.db.*"))


def test_freelist_corruption_is_repaired_without_data_loss(tmp_path):
    db = tmp_path / "app.db"
    _make_db(db)
    _corrupt_freelist(db)
    corrupted = db.read_bytes()

    result = db_integrity.check_database(str(db), stamp="20260101000000")

    assert result["status"] == db_integrity.REPAIRED
    assert "freelist" in result["detail"].lower()
    backup = tmp_path / "app.db.corrupt-20260101000000"
    assert backup.read_bytes() == corrupted
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 100
    conn.close()
    assert not list(tmp_path.glob("app.db.repair-*"))


def test_unrepairable_database_is_untouched_and_reported(tmp_path):
    db = tmp_path / "app.db"
    healthy = tmp_path / "cwa.db"
    status = tmp_path / ".cwa_db_health.json"
    _make_db(db)
    _make_db(healthy)
    _corrupt_table(db)
    corrupted = db.read_bytes()

    code = db_integrity.main([str(db), str(healthy), str(tmp_path / "missing.db"), "--status-file", str(status)])

    assert code == 1
    assert db.read_bytes() == corrupted
    assert not list(tmp_path.glob("app.db.*"))
    reported = {d["db"]: d["status"] for d in json.loads(status.read_text())["databases"]}
    assert reported == {"app.db": "corrupt", "cwa.db": "ok", "missing.db": "missing"}
