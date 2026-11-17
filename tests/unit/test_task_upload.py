"""TaskUpload reports the real outcome of the separate ingest service."""

import importlib.util
import logging
import sqlite3
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def upload(monkeypatch, tmp_path):
    cps = types.ModuleType("cps")
    cps.__path__ = []
    cps.logger = types.SimpleNamespace(create=lambda: logging.getLogger("test"))
    babel = types.ModuleType("flask_babel")
    babel.lazy_gettext = lambda s, **kw: s % kw if kw else s
    for name, mod in {"cps": cps, "cps.logger": cps.logger, "flask_babel": babel,
                      "cps.services": types.ModuleType("cps.services"),
                      "cps.tasks": types.ModuleType("cps.tasks")}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setitem(sys.modules, "cps.services.worker", None)
    worker = _load("cps.services.worker", "cps/services/worker.py")
    module = _load("cps.tasks.upload", "cps/tasks/upload.py")
    monkeypatch.setattr(module, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(module, "CWA_DB_PATH", str(tmp_path / "cwa.db"))
    monkeypatch.setattr(module, "INGEST_STATUS_FILE", str(tmp_path / "status"))
    con = sqlite3.connect(tmp_path / "cwa.db")
    con.execute("CREATE TABLE cwa_import(timestamp TEXT, filename TEXT, original_backed_up TEXT)")
    con.commit()
    con.close()
    return module, worker


def _record_import(tmp_path, stem):
    con = sqlite3.connect(tmp_path / "cwa.db")
    con.execute("INSERT INTO cwa_import VALUES ('t', ?, 'False')", (stem,))
    con.commit()
    con.close()


def test_stays_started_while_file_waits_in_ingest(upload, tmp_path):
    module, worker = upload
    book = tmp_path / "new_1_123_book.epub"
    book.write_text("x")
    task = module.TaskUpload("msg", "book.epub", ingest_path=str(book))
    assert task.stat == worker.STAT_STARTED
    assert task.end_time is None
    assert "queued" in task.message

    (tmp_path / "status").write_text("processing:new_1_123_book.epub:2025")
    assert task.stat == worker.STAT_STARTED
    assert "importing" in task.message


def test_finished_when_import_recorded(upload, tmp_path):
    module, worker = upload
    book = tmp_path / "new_1_123_book.epub"
    task = module.TaskUpload("msg", "book.epub", ingest_path=str(book))
    _record_import(tmp_path, "new_1_123_book")
    assert task.stat == worker.STAT_FINISH_SUCCESS
    assert task.end_time is not None and task.progress == 1


def test_failed_when_file_gone_without_import_record(upload, tmp_path):
    module, worker = upload
    task = module.TaskUpload("msg", "book.epub", ingest_path=str(tmp_path / "new_1_123_book.epub"))
    assert task.stat == worker.STAT_FAIL
    assert task.error


def test_long_filename_matches_truncated_stem(upload, tmp_path):
    module, worker = upload
    stem = "n" * 200
    task = module.TaskUpload("msg", "b", ingest_path=str(tmp_path / (stem + ".epub")))
    _record_import(tmp_path, stem[:150 - len(".epub")])
    assert task.stat == worker.STAT_FINISH_SUCCESS


def test_format_upload_outcome_from_manifest(upload, tmp_path):
    module, worker = upload
    ok = module.TaskUpload("msg", "b", ingest_path=str(tmp_path / "fmt_ok.epub"), is_format=True)
    assert ok.stat == worker.STAT_FINISH_SUCCESS

    failed_path = tmp_path / "fmt_bad.epub"
    Path(str(failed_path) + ".cwa.failed.json").write_text("{}")
    bad = module.TaskUpload("msg", "b", ingest_path=str(failed_path), is_format=True)
    assert bad.stat == worker.STAT_FAIL


def test_without_ingest_path_finishes_immediately(upload):
    module, worker = upload
    task = module.TaskUpload("msg", "b")
    assert task.stat == worker.STAT_FINISH_SUCCESS
