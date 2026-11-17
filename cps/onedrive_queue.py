# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Persistent queue for OneDrive imports.

Jobs and their files live in app.db (ub.OneDriveImportJob / OneDriveImportItem), so
an import resumes after a restart. A dispatcher runs up to MAX_PARALLEL_JOBS jobs
(FIFO). Each job first expands the selection (selected files + .epub files found
recursively in selected folders), deduplicating by OneDrive content hash, then
downloads files one at a time into the ingest folder and follows each file
through cwa-ingest-service.

This does not use WorkerThread, which runs every background task sequentially.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone

from flask_babel import lazy_gettext as N_
from markupsafe import Markup, escape
from sqlalchemy import func

from . import logger, ub
from .onedrive_client import GraphClient, OneDriveAuthError, OneDriveError, content_hash, extension
from .services.worker import CalibreTask, WorkerThread, STAT_WAITING, STAT_STARTED, STAT_FINISH_SUCCESS, \
    STAT_FAIL, STAT_ENDED, STAT_CANCELLED
from .tasks.upload import (ingest_outcome_details, request_ingest_cancel, discard_queued_ingest_file,
                           OUTCOME_EXISTING, OUTCOME_FAILED, OUTCOME_CANCELLED)

log = logger.create()

MAX_PARALLEL_JOBS = 2
MAX_PENDING_INGEST_FILES = 10
DISK_HEADROOM = 256 * 1024 * 1024
INGEST_PREFIX = "onedrive"

# Item states
PENDING, INGESTING, IMPORTED, EXISTING, DUPLICATE, FAILED, CANCELLED = (
    "pending", "ingesting", "imported", "existing", "duplicate", "failed", "cancelled")
# Job states
J_QUEUED, J_RUNNING, J_DONE, J_FAILED, J_CANCELLED = "queued", "running", "done", "failed", "cancelled"
_FINISHED_JOB_STATES = (J_DONE, J_FAILED, J_CANCELLED)


def _now():
    return datetime.now(timezone.utc)


class ImportQueue:
    def __init__(self, tokens, session_factory, ingest_dir, new_ingest_path, allowed_formats,
                 max_parallel=MAX_PARALLEL_JOBS, poll_interval=2.0, sleep=time.sleep):
        """
        tokens: onedrive_client.TokenManager
        session_factory(): new SQLAlchemy session on app.db (one per thread)
        ingest_dir(): ingest folder path
        new_ingest_path(name, user_id): unique ingest path for a downloaded file
        allowed_formats(): set of lowercase extensions accepted for single files
        """
        self.tokens = tokens
        self._session_factory = session_factory
        self._ingest_dir = ingest_dir
        self._new_ingest_path = new_ingest_path
        self._allowed_formats = allowed_formats
        self.max_parallel = max_parallel
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._running = {}
        self._cancel_requested = set()
        # job id -> name of the file being downloaded
        self._downloading = {}
        self._started = False

    # ── public API ──────────────────────────────────────────────────────────

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            self.recover()
        except Exception as e:
            log.error("OneDrive import: could not resume pending imports: %s", e)
        threading.Thread(target=self._dispatch_loop, name="onedrive-dispatcher", daemon=True).start()

    def wake(self):
        self._wake.set()

    def submit(self, user_id, username, selection):
        session = self._session_factory()
        try:
            job = ub.OneDriveImportJob(user_id=user_id, status=J_QUEUED, selection=json.dumps(selection))
            session.add(job)
            session.commit()
            job_id = job.id
        finally:
            session.close()
        self._add_proxy(job_id, username)
        self.wake()
        return job_id

    def cancel(self, job_id, user_id=None):
        session = self._session_factory()
        try:
            job = session.get(ub.OneDriveImportJob, job_id)
            if job is None or (user_id is not None and job.user_id != user_id):
                return False
            claimed = session.query(ub.OneDriveImportJob).filter(
                ub.OneDriveImportJob.id == job_id, ub.OneDriveImportJob.status == J_QUEUED).update(
                {"status": J_CANCELLED, "finished_at": _now()}, synchronize_session=False)
            if claimed:
                session.query(ub.OneDriveImportItem).filter(
                    ub.OneDriveImportItem.job_id == job_id, ub.OneDriveImportItem.status == PENDING).update(
                    {"status": CANCELLED}, synchronize_session=False)
                session.commit()
                return True
            session.rollback()
            session.refresh(job)
            if job.status == J_RUNNING:
                with self._lock:
                    self._cancel_requested.add(job_id)
                return True
            return False
        finally:
            session.close()

    def summary(self, job_id, session=None):
        own = session is None
        session = session or self._session_factory()
        try:
            job = session.get(ub.OneDriveImportJob, job_id)
            if job is None:
                return None
            return self._summarize(session, job)
        finally:
            if own:
                session.close()

    def jobs_for_user(self, user_id, limit=10):
        session = self._session_factory()
        try:
            jobs = (session.query(ub.OneDriveImportJob).filter(ub.OneDriveImportJob.user_id == user_id)
                    .order_by(ub.OneDriveImportJob.id.desc()).limit(limit).all())
            return [self._summarize(session, job) for job in jobs]
        finally:
            session.close()

    # ── recovery / dispatch ─────────────────────────────────────────────────

    def recover(self):
        """After a restart: interrupted jobs go back to the queue, partial downloads are removed."""
        session = self._session_factory()
        try:
            session.query(ub.OneDriveImportJob).filter(ub.OneDriveImportJob.status == J_RUNNING).update(
                {"status": J_QUEUED}, synchronize_session=False)
            session.commit()
            unfinished = (session.query(ub.OneDriveImportJob, ub.User.name)
                          .outerjoin(ub.User, ub.User.id == ub.OneDriveImportJob.user_id)
                          .filter(ub.OneDriveImportJob.status == J_QUEUED)
                          .order_by(ub.OneDriveImportJob.id).all())
            for job, username in unfinished:
                self._add_proxy(job.id, username)
        finally:
            session.close()
        try:
            ingest_dir = self._ingest_dir()
            for entry in os.scandir(ingest_dir):
                if entry.name.startswith(INGEST_PREFIX + "_") and entry.name.endswith(".uploading"):
                    os.remove(entry.path)
        except OSError as e:
            log.debug("OneDrive import: could not clean partial downloads: %s", e)

    def _add_proxy(self, job_id, username):
        try:
            WorkerThread.add(username, OneDriveImportTask(self, job_id))
        except Exception as e:
            log.debug("OneDrive import: could not add task entry: %s", e)

    def _dispatch_loop(self):
        while True:
            self._wake.wait(timeout=30)
            self._wake.clear()
            try:
                self._dispatch_once()
            except Exception as e:
                log.error("OneDrive import dispatcher error: %s", e)

    def _dispatch_once(self):
        with self._lock:
            for job_id in [j for j, t in self._running.items() if not t.is_alive()]:
                self._running.pop(job_id)
            free = self.max_parallel - len(self._running)
        while free > 0:
            job_id = self._claim_next()
            if job_id is None:
                return
            thread = threading.Thread(target=self._run_job, args=(job_id,), name="onedrive-job-%d" % job_id,
                                      daemon=True)
            with self._lock:
                self._running[job_id] = thread
            thread.start()
            free -= 1

    def _claim_next(self):
        session = self._session_factory()
        try:
            while True:
                with self._lock:
                    running = list(self._running)
                query = session.query(ub.OneDriveImportJob.id).filter(ub.OneDriveImportJob.status == J_QUEUED)
                if running:
                    query = query.filter(~ub.OneDriveImportJob.id.in_(running))
                row = query.order_by(ub.OneDriveImportJob.id).first()
                if row is None:
                    return None
                claimed = session.query(ub.OneDriveImportJob).filter(
                    ub.OneDriveImportJob.id == row.id, ub.OneDriveImportJob.status == J_QUEUED).update(
                    {"status": J_RUNNING, "started_at": _now()}, synchronize_session=False)
                session.commit()
                if claimed:
                    return row.id
        finally:
            session.close()

    # ── job execution ───────────────────────────────────────────────────────

    def _cancelled(self, job_id):
        with self._lock:
            return job_id in self._cancel_requested

    def _run_job(self, job_id):
        session = self._session_factory()
        try:
            job = session.get(ub.OneDriveImportJob, job_id)
            graph = GraphClient(self.tokens, job.user_id, sleep=self._sleep)
            if not job.enumerated:
                self._enumerate(session, job, graph)
            self._process(session, job, graph)
        except OneDriveAuthError as e:
            log.warning("OneDrive import %s: authorization failed: %s", job_id, e)
            self._finish(session, job_id, J_FAILED, "OneDrive connection expired: reconnect OneDrive and retry")
        except Exception as e:
            log.error("OneDrive import %s failed: %s", job_id, e)
            self._finish(session, job_id, J_FAILED, str(e)[:500])
        finally:
            with self._lock:
                self._cancel_requested.discard(job_id)
            session.close()
            self.wake()

    def _finish(self, session, job_id, status, error=None):
        try:
            session.rollback()
            job = session.get(ub.OneDriveImportJob, job_id)
            if job is None:
                return
            if status != J_DONE:
                session.query(ub.OneDriveImportItem).filter(
                    ub.OneDriveImportItem.job_id == job_id, ub.OneDriveImportItem.status == PENDING).update(
                    {"status": CANCELLED if status == J_CANCELLED else FAILED}, synchronize_session=False)
            job.status = status
            job.error = error
            job.finished_at = _now()
            session.commit()
        except Exception as e:
            log.error("OneDrive import %s: could not record final state: %s", job_id, e)

    def _imported_hashes(self, session, hashes):
        if not hashes:
            return set()
        rows = session.query(ub.OneDriveImportedHash.content_hash).filter(
            ub.OneDriveImportedHash.content_hash.in_(list(hashes))).all()
        return {r.content_hash for r in rows}

    def _enumerate(self, session, job, graph):
        session.query(ub.OneDriveImportItem).filter(ub.OneDriveImportItem.job_id == job.id).delete(
            synchronize_session=False)
        session.commit()
        allowed = set(self._allowed_formats())
        seen = set()
        batch = []
        seq = 0
        stop = lambda: self._cancelled(job.id)

        def add(entry, status=PENDING, error=None):
            nonlocal seq
            digest = content_hash(entry)
            if status == PENDING and digest:
                if digest in seen:
                    status = DUPLICATE
                seen.add(digest)
            seq += 1
            batch.append(ub.OneDriveImportItem(
                job_id=job.id, seq=seq, drive_item_id=entry["id"], name=entry.get("name") or entry["id"],
                size=entry.get("size") or 0, content_hash=digest, status=status, error=error))
            if len(batch) >= 100:
                flush()

        def flush():
            known = self._imported_hashes(session, {i.content_hash for i in batch
                                                    if i.status == PENDING and i.content_hash})
            for item in batch:
                if item.status == PENDING and item.content_hash in known:
                    item.status = EXISTING
            session.add_all(batch)
            session.commit()
            batch.clear()

        for selected in json.loads(job.selection):
            if stop():
                break
            if selected.get("folder"):
                if "epub" not in allowed:
                    continue
                for entry in graph.walk_files(selected["id"], {"epub"}, should_stop=stop):
                    add(entry)
            else:
                try:
                    entry = graph.item(selected["id"])
                except OneDriveAuthError:
                    raise
                except OneDriveError as e:
                    add({"id": selected["id"], "name": selected.get("name")}, FAILED, str(e)[:500])
                    continue
                if "file" not in entry:
                    continue
                if extension(entry.get("name", "")) not in allowed:
                    add(entry, FAILED, "Unsupported format")
                else:
                    add(entry)
        flush()
        job.enumerated = True
        session.commit()

    def _process(self, session, job, graph):
        Item = ub.OneDriveImportItem
        stop = lambda: self._cancelled(job.id)
        while True:
            if stop():
                session.query(Item).filter(Item.job_id == job.id, Item.status == PENDING).update(
                    {"status": CANCELLED}, synchronize_session=False)
                session.commit()
                self._cancel_ingesting(session, job.id)
            self._resolve_ingesting(session, job.id)

            item = (session.query(Item).filter(Item.job_id == job.id, Item.status == PENDING)
                    .order_by(Item.seq).first())
            if item is None:
                if session.query(Item.id).filter(Item.job_id == job.id, Item.status == INGESTING).first():
                    self._sleep(self.poll_interval)
                    continue
                self._finish(session, job.id, J_CANCELLED if stop() else J_DONE)
                return

            if item.content_hash and self._imported_hashes(session, {item.content_hash}):
                item.status = EXISTING
                session.commit()
                continue

            ingest_dir = self._ingest_dir()
            if self._ingest_backlog(ingest_dir) >= MAX_PENDING_INGEST_FILES:
                self._sleep(self.poll_interval)
                continue
            if shutil.disk_usage(ingest_dir).free < (item.size or 0) + DISK_HEADROOM:
                if self._ingest_backlog(ingest_dir):
                    self._sleep(self.poll_interval)  # space is released as the ingest folder drains
                    continue
                item.status = FAILED
                item.error = "Not enough free disk space"
                session.commit()
                continue

            self._download(session, item, job.user_id, graph, stop)

    def _download(self, session, item, user_id, graph, stop):
        path = self._new_ingest_path(item.name, user_id)
        tmp_path = path + ".uploading"
        with self._lock:
            self._downloading[item.job_id] = item.name
        try:
            completed = graph.download(item.drive_item_id, tmp_path, should_stop=stop)
        except OneDriveAuthError:
            _remove(tmp_path)
            raise
        except (OneDriveError, OSError) as e:
            _remove(tmp_path)
            item.status = FAILED
            item.error = str(e)[:500]
            session.commit()
            return
        finally:
            with self._lock:
                self._downloading.pop(item.job_id, None)
        if not completed:
            _remove(tmp_path)
            return
        os.replace(tmp_path, path)
        item.ingest_path = path
        item.status = INGESTING
        session.commit()

    def _cancel_ingesting(self, session, job_id):
        """Ask cwa-ingest-service to stop (and undo) the files of a cancelled job it has not finished."""
        Item = ub.OneDriveImportItem
        for item in session.query(Item).filter(Item.job_id == job_id, Item.status == INGESTING).all():
            if item.error == "cancel requested":
                continue
            request_ingest_cancel(item.ingest_path)
            if discard_queued_ingest_file(item.ingest_path):
                item.status = CANCELLED
                item.error = None
            else:
                item.error = "cancel requested"
            session.commit()

    def _resolve_ingesting(self, session, job_id):
        Item = ub.OneDriveImportItem
        for item in session.query(Item).filter(Item.job_id == job_id, Item.status == INGESTING).all():
            details = ingest_outcome_details(item.ingest_path, consume_marker=True)
            if details is None:
                continue
            outcome, title = details
            cancel_requested = item.error == "cancel requested"
            item.error = None
            if outcome == OUTCOME_CANCELLED or (cancel_requested and outcome == OUTCOME_FAILED):
                item.status = CANCELLED
            elif outcome == OUTCOME_FAILED:
                item.status = FAILED
                item.error = "Import failed, see CWA logs"
            else:
                item.status = EXISTING if outcome == OUTCOME_EXISTING else IMPORTED
                item.book_title = title or None
                if item.content_hash:
                    session.merge(ub.OneDriveImportedHash(content_hash=item.content_hash, name=item.name))
            session.commit()

    @staticmethod
    def _ingest_backlog(ingest_dir):
        try:
            return sum(1 for e in os.scandir(ingest_dir) if not e.name.startswith("."))
        except OSError:
            return 0

    # ── reporting ───────────────────────────────────────────────────────────

    def _summarize(self, session, job):
        Item = ub.OneDriveImportItem
        counts = dict(session.query(Item.status, func.count(Item.id)).filter(Item.job_id == job.id)
                      .group_by(Item.status).all())
        total = sum(counts.values())
        processed = sum(counts.get(s, 0) for s in (IMPORTED, EXISTING, DUPLICATE, FAILED, CANCELLED))
        try:
            selection = json.loads(job.selection)
        except ValueError:
            selection = []
        current = None
        if job.status == J_RUNNING:
            with self._lock:
                current = self._downloading.get(job.id)
            if current is None:
                ingesting = (session.query(Item.name).filter(Item.job_id == job.id, Item.status == INGESTING)
                             .order_by(Item.seq).first())
                current = ingesting[0] if ingesting else None
        latest = (session.query(Item.book_title).filter(Item.job_id == job.id, Item.status == IMPORTED,
                                                        Item.book_title.isnot(None))
                  .order_by(Item.seq.desc()).first())
        return {
            "id": job.id,
            "status": job.status,
            "enumerated": bool(job.enumerated),
            "error": job.error,
            "created": job.created_at.isoformat() if job.created_at else None,
            "finished": job.finished_at.isoformat() if job.finished_at else None,
            "selection": [s.get("name") for s in selection][:5],
            "selection_count": len(selection),
            "total": total,
            "processed": processed,
            "pending": counts.get(PENDING, 0) + counts.get(INGESTING, 0),
            "imported": counts.get(IMPORTED, 0),
            "existing": counts.get(EXISTING, 0),
            "duplicate": counts.get(DUPLICATE, 0),
            "failed": counts.get(FAILED, 0),
            "cancelled": counts.get(CANCELLED, 0),
            "current": current,
            "last_book": latest[0] if latest else None,
        }


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class OneDriveImportTask(CalibreTask):
    """Tasks-page entry mirroring a persistent OneDrive import job. Never runs on the worker thread."""

    _POLL_INTERVAL = 2

    def __init__(self, queue, job_id):
        super().__init__(N_("Queued"))
        self.queue = queue
        self.job_id = job_id
        self.start_time = datetime.now()
        self._stat = STAT_STARTED
        self._last_poll = 0.0
        self._refresh(force=True)

    def run(self, worker_thread):
        """Progress comes from the import queue."""

    def _refresh(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_poll < self._POLL_INTERVAL:
            return
        self._last_poll = now
        try:
            info = self.queue.summary(self.job_id)
        except Exception as e:
            log.debug("OneDrive import task %s: %s", self.job_id, e)
            return
        if info is None:
            self._set_final(STAT_FAIL)
            self.error = N_("Import job not found")
            return
        status = info["status"]
        if status == J_QUEUED:
            self.message = N_("Queued")
        elif status == J_RUNNING and not info["enumerated"]:
            self.message = N_("Scanning OneDrive...")
        else:
            self.message = N_("%(processed)s/%(total)s processed: %(imported)s imported, %(skipped)s skipped, "
                              "%(failed)s failed", processed=info["processed"], total=info["total"],
                              imported=info["imported"], skipped=info["existing"] + info["duplicate"],
                              failed=info["failed"])
            details = []
            if info.get("current"):
                details.append(str(N_("Current file: %(name)s", name=escape(info["current"]))))
            if info.get("last_book"):
                details.append(str(N_("Last imported: %(title)s", title=escape(info["last_book"]))))
            if details:
                self.message = Markup("{}<br>{}").format(self.message, Markup("<br>").join(details))
        if info["total"]:
            self._progress = min(1.0, info["processed"] / info["total"])
        if status == J_DONE:
            self._set_final(STAT_FINISH_SUCCESS)
        elif status == J_CANCELLED:
            self._set_final(STAT_CANCELLED)
        elif status == J_FAILED:
            self._set_final(STAT_FAIL)
            self.error = info["error"]

    def _set_final(self, stat):
        if self._stat in (STAT_STARTED, STAT_WAITING):
            self._stat = stat
            self.end_time = datetime.now()
            if stat == STAT_FINISH_SUCCESS:
                self._progress = 1

    @property
    def stat(self):
        if getattr(self, "queue", None) is not None and self._stat == STAT_STARTED:
            self._refresh()
        return self._stat

    @stat.setter
    def stat(self, x):
        # Only the Tasks page cancel button changes the state from outside (WorkerThread.end_task)
        if x in (STAT_ENDED, STAT_CANCELLED) and getattr(self, "queue", None) is not None:
            self.queue.cancel(self.job_id)
            self._last_poll = 0.0
        elif getattr(self, "queue", None) is None:
            self._stat = x

    @property
    def name(self):
        return N_("Import from OneDrive")

    @property
    def is_cancellable(self):
        return self._stat == STAT_STARTED

    def __str__(self):
        return "OneDrive import {}".format(self.job_id)
