# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from flask_babel import lazy_gettext as N_
from markupsafe import escape

from cps.services.worker import (CalibreTask, STAT_STARTED, STAT_FINISH_SUCCESS, STAT_FAIL, STAT_ENDED,
                                 STAT_CANCELLED)

CWA_DB_PATH = "/config/cwa.db"
INGEST_STATUS_FILE = os.environ.get("CWA_INGEST_STATUS_FILE", "/config/cwa_ingest_status")
# Written by scripts/ingest_processor.py record_ingest_outcome()
INGEST_OUTCOME_DIR = os.environ.get("CWA_INGEST_OUTCOME_DIR", "/config/.cwa_ingest_outcomes")
# Read by scripts/ingest_processor.py CancelWatch
INGEST_CANCEL_DIR = os.environ.get("CWA_INGEST_CANCEL_DIR", "/config/.cwa_ingest_cancel")
# Must match the filename truncation in scripts/ingest_processor.py main()
_INGEST_MAX_FILENAME = 150
_POLL_INTERVAL = 2

OUTCOME_IMPORTED = "imported"
OUTCOME_EXISTING = "existing"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"


def ingest_pending(path):
    # ingest_processor renames over-long files first: the file is still pending under its truncated name
    if os.path.exists(path + ".uploading"):
        return True
    folder = os.path.dirname(path)
    return any(os.path.exists(os.path.join(folder, name)) for name in _ingest_names(path))


def ingest_outcome(path, is_format=False, consume_marker=False):
    """Outcome of a file handed to cwa-ingest-service, or None while it is still in the ingest folder."""
    details = ingest_outcome_details(path, is_format, consume_marker)
    return details[0] if details else None


def ingest_outcome_details(path, is_format=False, consume_marker=False):
    """(outcome, book title) of a file handed to cwa-ingest-service, or None while it is still pending."""
    if ingest_pending(path):
        return None
    for name in _ingest_names(path):
        marker = os.path.join(INGEST_OUTCOME_DIR, name)
        try:
            with open(marker, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            if consume_marker:
                os.remove(marker)
        except OSError:
            continue
        outcome = lines[0].strip() if lines else ""
        title = lines[1].strip() if len(lines) > 1 else ""
        if outcome in (OUTCOME_EXISTING, OUTCOME_CANCELLED, OUTCOME_IMPORTED):
            return outcome, title
    return (OUTCOME_FAILED if _import_failed(path, is_format) else OUTCOME_IMPORTED), ""


def request_ingest_cancel(path):
    """Ask ingest_processor to stop processing this file (or skip it if it has not started yet)."""
    try:
        os.makedirs(INGEST_CANCEL_DIR, exist_ok=True)
        for name in _ingest_names(path):
            with open(os.path.join(INGEST_CANCEL_DIR, name), "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        return True
    except OSError:
        return False


def ingest_service_status():
    try:
        with open(INGEST_STATUS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def is_being_ingested(path):
    status = ingest_service_status()
    return any(status.startswith("processing:" + name + ":") for name in _ingest_names(path))


def discard_queued_ingest_file(path):
    """Remove a file the ingest service has not picked up yet. Returns True if nothing is left to process."""
    if is_being_ingested(path):
        return False
    for leftover in (path, path + ".uploading", path + ".cwa.json"):
        try:
            os.remove(leftover)
        except OSError:
            pass
    return not ingest_pending(path)


def _ingest_names(path):
    # ingest_processor renames over-long files before processing them
    p = Path(path)
    truncated = p.stem[:_INGEST_MAX_FILENAME - len(p.suffix)] + p.suffix
    return [p.name] if truncated == p.name else [p.name, truncated]


def _import_failed(path, is_format):
    # Format uploads: ingest_processor keeps the manifest as *.cwa.failed.json on failure
    if is_format:
        return os.path.exists(path + ".cwa.failed.json")
    # New books: ingest_processor records the file stem in cwa_import on success
    p = Path(path)
    stem = p.stem
    truncated = stem[:_INGEST_MAX_FILENAME - len(p.suffix)]
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(CWA_DB_PATH), uri=True, timeout=5)
        try:
            row = con.execute("SELECT 1 FROM cwa_import WHERE filename IN (?, ?) LIMIT 1",
                              (stem, truncated)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False  # outcome unknown: don't report a false failure
    return row is None


class TaskUpload(CalibreTask):
    """Task-list entry for an upload.

    Without ingest_path it is created already finished (synchronous uploads).
    With ingest_path, the file was handed to the separate cwa-ingest-service
    process: the task stays "Started" while that file is in the ingest folder,
    then reports the import outcome. It never runs on the worker thread.
    """

    def __init__(self, task_message, book_title, ingest_path=None, is_format=False):
        super(TaskUpload, self).__init__(task_message)
        self.book_title = book_title
        self.ingest_path = ingest_path
        self.is_format = is_format
        self.start_time = datetime.now()
        self._last_poll = 0.0
        self._cancel_requested = False
        if ingest_path:
            self.stat = STAT_STARTED
            self.end_time = None
            self.message = N_("%(title)s (queued for import)", title=book_title)
        else:
            self.end_time = self.start_time
            self.stat = STAT_FINISH_SUCCESS
            self.progress = 1

    def run(self, worker_thread):
        """Upload task doesn't have anything to do, it's simply a way to add information to the task list"""

    @property
    def stat(self):
        if self._stat == STAT_STARTED and getattr(self, "ingest_path", None):
            now = time.monotonic()
            if now - self._last_poll >= _POLL_INTERVAL:
                self._last_poll = now
                self._poll_ingest()
        return self._stat

    @stat.setter
    def stat(self, x):
        if x in (STAT_ENDED, STAT_CANCELLED) and getattr(self, "ingest_path", None) \
                and getattr(self, "_stat", None) == STAT_STARTED:
            self._cancel()
            return
        self._stat = x

    def _cancel(self):
        """Cancel requested from the Tasks page: stop the import and undo it (see ingest_processor CancelWatch)."""
        if self._cancel_requested:
            return
        self._cancel_requested = True
        request_ingest_cancel(self.ingest_path)
        if discard_queued_ingest_file(self.ingest_path):
            self._finish_cancelled()
        else:
            self.message = N_("%(title)s (cancelling...)", title=self.book_title)
            self._last_poll = 0.0

    def _finish_cancelled(self):
        self.end_time = datetime.now()
        self.progress = 1
        self._stat = STAT_CANCELLED
        self.message = N_("%(title)s (cancelled)", title=self.book_title)

    def _poll_ingest(self):
        path = self.ingest_path
        details = ingest_outcome_details(path, self.is_format, consume_marker=True)
        if details is None:
            if not self._cancel_requested and is_being_ingested(path):
                self.progress = 0.5
                self.message = N_("%(title)s (importing...)", title=self.book_title)
            return

        outcome, title = details
        if outcome == OUTCOME_CANCELLED or (self._cancel_requested and outcome == OUTCOME_FAILED):
            self._finish_cancelled()
            return
        self.end_time = datetime.now()
        self.progress = 1
        if outcome == OUTCOME_FAILED:
            self._stat = STAT_FAIL
            self.message = N_("%(title)s (import failed)", title=self.book_title)
            self.error = N_("See CWA Logs for details")
        elif outcome == OUTCOME_EXISTING:
            self._stat = STAT_FINISH_SUCCESS
            self.message = N_("%(title)s (already in library)", title=self.book_title)
        elif title:
            self._stat = STAT_FINISH_SUCCESS
            self.message = N_("%(file)s → \"%(title)s\" (imported)", file=self.book_title, title=escape(title))
        else:
            self._stat = STAT_FINISH_SUCCESS
            self.message = N_("%(title)s (imported)", title=self.book_title)

    @property
    def name(self):
        return N_("Upload")

    def __str__(self):
        return "Upload {}".format(self.book_title)

    @property
    def is_cancellable(self):
        return bool(self.ingest_path) and not getattr(self, "_cancel_requested", False)
