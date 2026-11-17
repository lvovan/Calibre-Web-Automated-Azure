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


def test_existing_marker_reports_already_in_library(upload, tmp_path, monkeypatch):
    module, worker = upload
    outcomes = tmp_path / "outcomes"
    outcomes.mkdir()
    monkeypatch.setattr(module, "INGEST_OUTCOME_DIR", str(outcomes))
    stem = "onedrive_" + "n" * 200
    (outcomes / (stem[:150 - len(".epub")] + ".epub")).write_text("existing")
    path = str(tmp_path / (stem + ".epub"))

    task = module.TaskUpload("msg", "b", ingest_path=path)
    assert task.stat == worker.STAT_FINISH_SUCCESS
    assert "already in library" in task.message
    assert not list(outcomes.iterdir())  # consumed by the task

    (outcomes / (stem[:150 - len(".epub")] + ".epub")).write_text("existing")
    assert module.ingest_outcome(path, consume_marker=True) == module.OUTCOME_EXISTING
    assert not list(outcomes.iterdir())
    assert module.ingest_outcome(path) == module.OUTCOME_FAILED  # no marker, no cwa_import row


def test_imported_marker_shows_escaped_book_title(upload, tmp_path, monkeypatch):
    module, worker = upload
    outcomes = tmp_path / "outcomes"
    outcomes.mkdir()
    monkeypatch.setattr(module, "INGEST_OUTCOME_DIR", str(outcomes))
    (outcomes / "new_1_book.epub").write_text("imported\nDune <Deluxe>")
    task = module.TaskUpload("msg", "book.epub", ingest_path=str(tmp_path / "new_1_book.epub"))
    assert task.stat == worker.STAT_FINISH_SUCCESS
    assert task.message == 'book.epub → "Dune &lt;Deluxe&gt;" (imported)'


@pytest.fixture
def cancel_dir(upload, tmp_path, monkeypatch):
    module, _ = upload
    folder = tmp_path / "cancel"
    monkeypatch.setattr(module, "INGEST_CANCEL_DIR", str(folder))
    monkeypatch.setattr(module, "INGEST_OUTCOME_DIR", str(tmp_path / "outcomes"))
    return folder


def test_cancel_before_ingest_starts_removes_queued_file(upload, tmp_path, cancel_dir):
    module, worker = upload
    book = tmp_path / "new_1_book.epub"
    book.write_text("x")
    Path(str(book) + ".cwa.json").write_text("{}")
    task = module.TaskUpload("msg", "book.epub", ingest_path=str(book))
    assert worker.can_cancel(task)

    task.stat = worker.STAT_ENDED
    assert task.stat == worker.STAT_CANCELLED
    assert "cancelled" in task.message and task.end_time is not None
    assert not book.exists() and not Path(str(book) + ".cwa.json").exists()
    assert (cancel_dir / "new_1_book.epub").exists()


def test_cancel_during_ingest_waits_for_ingest_processor(upload, tmp_path, cancel_dir):
    module, worker = upload
    book = tmp_path / "new_1_book.epub"
    book.write_text("x")
    (tmp_path / "status").write_text("processing:new_1_book.epub:2025")
    task = module.TaskUpload("msg", "book.epub", ingest_path=str(book))

    task.stat = worker.STAT_ENDED
    assert book.exists()  # the ingest service owns it now
    assert (cancel_dir / "new_1_book.epub").exists()
    assert task.stat == worker.STAT_STARTED and "cancelling" in task.message
    assert not worker.can_cancel(task)

    book.unlink()
    (tmp_path / "outcomes").mkdir()
    (tmp_path / "outcomes" / "new_1_book.epub").write_text("cancelled")
    assert task.stat == worker.STAT_CANCELLED


def test_end_task_checks_owner_and_cancels_waiting_tasks(upload, monkeypatch):
    module, worker = upload

    class Waiting(worker.CalibreTask):
        def run(self, worker_thread):
            pass

        @property
        def name(self):
            return "w"

        @property
        def is_cancellable(self):
            return False

    task = Waiting("msg")
    queued = worker.QueuedTask(num=1, user="alice", added=None, task=task, hidden=False)
    ins = types.SimpleNamespace(tasks=[queued])
    ins.get_instance = lambda: ins
    end_task = lambda task_id, user: worker.WorkerThread.end_task(ins, task_id, user)

    assert end_task(task.id, user="bob") is False
    assert task.stat == worker.STAT_WAITING
    assert end_task(task.id, user="alice") is True
    assert task.stat == worker.STAT_CANCELLED and task.end_time is not None

    task._handleSuccess()  # a late completion must not overwrite the cancellation
    assert task.stat == worker.STAT_CANCELLED
