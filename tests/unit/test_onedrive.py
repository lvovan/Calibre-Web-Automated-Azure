# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""OneDrive import: Graph client, token handling and the persistent import queue."""

import json
import logging
import os
import sys
import threading
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CPS_DIR = Path(__file__).resolve().parents[2] / "cps"


@pytest.fixture
def mods(monkeypatch):
    """cps.onedrive_client / cps.onedrive_queue / cps.ub without initializing the cps app."""
    for name in list(sys.modules):
        if name == "cps" or name.startswith("cps."):
            monkeypatch.delitem(sys.modules, name)
    cps = types.ModuleType("cps")
    cps.__path__ = [str(CPS_DIR)]
    services = types.ModuleType("cps.services")
    services.__path__ = [str(CPS_DIR / "services")]
    logger = types.ModuleType("cps.logger")
    logger.create = lambda *a, **k: logging.getLogger("cwa-test")
    cw_login = types.ModuleType("cps.cw_login")
    cw_login.AnonymousUserMixin = type("AnonymousUserMixin", (), {})
    cw_login.current_user = None
    cw_login.user_logged_in = types.SimpleNamespace(connect=lambda f: None)
    for name, mod in (("cps", cps), ("cps.services", services), ("cps.logger", logger),
                      ("cps.cw_login", cw_login)):
        monkeypatch.setitem(sys.modules, name, mod)
    cps.logger = logger

    import cps.onedrive_client as client
    import cps.onedrive_queue as queue
    import cps.ub as ub
    added = []
    monkeypatch.setattr(queue.WorkerThread, "add", classmethod(lambda cls, user, task, hidden=False:
                                                               added.append((user, task))))
    return types.SimpleNamespace(client=client, queue=queue, ub=ub, added_tasks=added)


# ── Fake Microsoft Graph ─────────────────────────────────────────────────────

def _file(item_id, name, digest=None, size=10):
    entry = {"id": item_id, "name": name, "size": size, "file": {}}
    if digest:
        entry["file"]["hashes"] = {"quickXorHash": digest}
    return entry


def _folder(item_id, name, count=0):
    return {"id": item_id, "name": name, "folder": {"childCount": count}}


class FakeGraph:
    """Stands in for onedrive_client.GraphClient, backed by a folder tree."""

    def __init__(self, tree, items=None, contents=None, page_size=2):
        self.tree = tree            # folder id -> list of entries
        self.items = items or {}    # item id -> entry (single-file lookups)
        self.contents = contents or {}
        self.page_size = page_size
        self.pages_served = 0
        self.downloads = []

    def children_page(self, item_id=None, cursor=None):
        entries = self.tree[item_id or "root"]
        start = int(cursor.rsplit("=", 1)[1]) if cursor else 0
        self.pages_served += 1
        end = start + self.page_size
        nxt = "https://graph.microsoft.com/v1.0/next?skip={}".format(end) if end < len(entries) else None
        return entries[start:end], nxt

    def walk_files(self, folder_id, extensions, should_stop=lambda: False):
        from cps.onedrive_client import GraphClient
        return GraphClient.walk_files(self, folder_id, extensions, should_stop)

    def item(self, item_id):
        return self.items[item_id]

    def download(self, item_id, dest_path, should_stop=lambda: False):
        self.downloads.append(item_id)
        with open(dest_path, "wb") as f:
            f.write(self.contents.get(item_id, b"book"))
        return True


def test_walk_files_is_recursive_paged_depth_first_and_filters_extensions(mods):
    graph = FakeGraph({
        "root": [_folder("f2", "b-folder"), _file("1", "z.epub"), _folder("f1", "A-folder"), _file("2", "notes.pdf")],
        "f1": [_file("3", "c.EPUB"), _file("4", "a.epub"), _folder("f3", "empty")],
        "f2": [_file("5", "x.mobi"), _file("6", "y.epub")],
        "f3": [],
    })
    names = [e["name"] for e in graph.walk_files(None, {"epub"})]
    assert names == ["a.epub", "c.EPUB", "y.epub", "z.epub"]
    assert graph.pages_served > len(graph.tree)  # paging was followed


def test_cursor_and_item_id_validation(mods):
    c = mods.client
    assert c.valid_cursor("https://graph.microsoft.com/v1.0/me/drive/items/A!1/children?$skiptoken=x")
    assert not c.valid_cursor("https://evil.example.com/v1.0/")
    assert not c.valid_cursor("https://graph.microsoft.com/v1.0.evil.com/x")
    assert c.valid_item_id("8E1AB2C3D4!123")
    assert not c.valid_item_id("../me")
    graph = c.GraphClient(tokens=None, user_id=1)
    with pytest.raises(c.OneDriveError):
        graph.children_page(cursor="https://example.com/steal-token")


def test_content_hash_prefers_quickxor_then_sha(mods):
    c = mods.client
    assert c.content_hash({"file": {"hashes": {"quickXorHash": "AbC=", "sha1Hash": "FF"}}}) == "qx:AbC="
    assert c.content_hash({"file": {"hashes": {"sha1Hash": "FF"}}}) == "sha1:ff"
    assert c.content_hash({"file": {}}) is None


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def close(self):
        pass


def test_token_refresh_persists_rotated_refresh_token(mods, monkeypatch):
    c = mods.client
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
    monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "secret")
    store = {7: "refresh-1"}
    posts = []

    def fake_post(url, data, timeout):
        posts.append(data)
        return _Resp(200, {"access_token": "access-%d" % len(posts), "expires_in": 3600,
                           "refresh_token": "refresh-2"})

    monkeypatch.setattr(c.requests, "post", fake_post)
    tokens = c.TokenManager(store.get, store.__setitem__)
    assert tokens.get(7) == "access-1"
    assert tokens.get(7) == "access-1"  # cached
    assert store[7] == "refresh-2"
    assert posts[0]["refresh_token"] == "refresh-1" and posts[0]["client_secret"] == "secret"
    assert tokens.get(7, force_refresh=True) == "access-2"
    assert posts[1]["refresh_token"] == "refresh-2"


def test_revoked_refresh_token_raises_auth_error(mods, monkeypatch):
    c = mods.client
    monkeypatch.setattr(c.requests, "post", lambda url, data, timeout: _Resp(400, {"error": "invalid_grant"}))
    tokens = c.TokenManager(lambda uid: "old", lambda uid, t: None)
    with pytest.raises(c.OneDriveAuthError):
        tokens.get(1)
    with pytest.raises(c.OneDriveAuthError):
        c.TokenManager(lambda uid: None, lambda uid, t: None).get(1)


def test_graph_retries_throttling_and_refreshes_once_on_401(mods, monkeypatch):
    c = mods.client
    responses = [_Resp(429, headers={"Retry-After": "7"}), _Resp(401), _Resp(200, {"value": [], "x": 1})]
    calls = []

    def fake_get(url, params=None, stream=False, timeout=None, headers=None):
        calls.append(headers["Authorization"])
        return responses.pop(0)

    class Tokens:
        def __init__(self):
            self.forced = 0

        def get(self, user_id, force_refresh=False):
            self.forced += force_refresh
            return "fresh" if force_refresh else "stale"

    slept = []
    monkeypatch.setattr(c.requests, "get", fake_get)
    tokens = Tokens()
    graph = c.GraphClient(tokens, 1, sleep=slept.append)
    assert graph.get(c.GRAPH + "/me").json()["x"] == 1
    assert slept == [7]
    assert tokens.forced == 1
    assert calls == ["Bearer stale", "Bearer stale", "Bearer fresh"]


# ── Import queue ─────────────────────────────────────────────────────────────

class FakeIngest:
    """Plays cwa-ingest-service: a file disappears from the ingest folder, then has an outcome."""

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}

    def __call__(self, path, is_format=False, consume_marker=False):
        if os.path.exists(path):
            os.remove(path)
            return None
        name = os.path.basename(path).split("_", 2)[2]
        outcome = self.outcomes.get(name, "imported")
        return outcome, ("Title of " + name if outcome == "imported" else "")


@pytest.fixture
def env(mods, tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    ub = mods.ub
    engine = create_engine("sqlite:///{}".format(tmp_path / "app.db"))
    ub.Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    session = maker()
    session.add(ub.User(id=5, name="reader", email="r@example.com", role=0))
    session.commit()
    session.close()

    ingest = tmp_path / "ingest"
    ingest.mkdir()
    counter = iter(range(1, 10000))
    fake_ingest = FakeIngest()
    monkeypatch.setattr(mods.queue, "ingest_outcome_details", fake_ingest)
    upload = sys.modules["cps.tasks.upload"]
    monkeypatch.setattr(upload, "INGEST_CANCEL_DIR", str(tmp_path / "cancel"))
    monkeypatch.setattr(upload, "INGEST_STATUS_FILE", str(tmp_path / "status"))

    def new_path(name, user_id):
        return str(ingest / "onedrive_{}_{}".format(next(counter), name))

    q = mods.queue.ImportQueue(tokens=None, session_factory=maker, ingest_dir=lambda: str(ingest),
                               new_ingest_path=new_path, allowed_formats=lambda: {"epub", "pdf"},
                               poll_interval=0, sleep=lambda s: None)
    return types.SimpleNamespace(q=q, maker=maker, ingest=ingest, ub=ub, fake_ingest=fake_ingest,
                                 added=mods.added_tasks, queue_mod=mods.queue)


def _use_graph(env, monkeypatch, graph):
    monkeypatch.setattr(env.queue_mod, "GraphClient", lambda tokens, user_id, sleep=None: graph)


def _items(env, job_id):
    session = env.maker()
    try:
        rows = (session.query(env.ub.OneDriveImportItem).filter(env.ub.OneDriveImportItem.job_id == job_id)
                .order_by(env.ub.OneDriveImportItem.seq).all())
        return [(r.name, r.status) for r in rows]
    finally:
        session.close()


def _job(env, job_id):
    session = env.maker()
    try:
        job = session.get(env.ub.OneDriveImportJob, job_id)
        return job.status, job.error
    finally:
        session.close()


def test_import_dedups_skips_known_hashes_and_filters_formats(env, monkeypatch):
    session = env.maker()
    session.add(env.ub.OneDriveImportedHash(content_hash="qx:H4", name="old.epub"))
    session.commit()
    session.close()
    graph = FakeGraph(
        tree={
            "F": [_file("b", "b.epub", "H1"), _file("c", "c.epub", "H2"), _file("n", "notes.pdf", "H9"),
                  _folder("S", "sub")],
            "S": [_file("d", "d.epub", "H4"), _file("e", "e.epub")],
        },
        items={"a": _file("a", "a.epub", "H1"), "x": _file("x", "tool.exe", "H5"),
               "p": _file("p", "paper.pdf", "H6")},
    )
    _use_graph(env, monkeypatch, graph)
    env.fake_ingest.outcomes = {"c.epub": "existing", "paper.pdf": "failed"}

    job_id = env.q.submit(5, "reader", [
        {"id": "a", "name": "a.epub", "folder": False},
        {"id": "F", "name": "Books", "folder": True},
        {"id": "x", "name": "tool.exe", "folder": False},
        {"id": "p", "name": "paper.pdf", "folder": False},
    ])
    assert env.q._claim_next() == job_id
    env.q._run_job(job_id)

    assert _items(env, job_id) == [
        ("a.epub", "imported"),     # first occurrence of H1
        ("b.epub", "duplicate"),    # same content as a.epub
        ("c.epub", "existing"),     # calibredb refused it: already in the library
        ("notes.pdf", "imported"),  # any allowed format is imported from folders
        ("d.epub", "existing"),     # hash imported by an earlier job: never downloaded
        ("e.epub", "imported"),     # no hash: imported anyway
        ("tool.exe", "failed"),     # unsupported single file
        ("paper.pdf", "failed"),    # ingest failure
    ]
    assert graph.downloads == ["a", "c", "n", "e", "p"]  # readme.xyz ignored inside folders
    assert _job(env, job_id) == ("done", None)
    session = env.maker()
    known = {h.content_hash for h in session.query(env.ub.OneDriveImportedHash).all()}
    session.close()
    assert known == {"qx:H1", "qx:H2", "qx:H4", "qx:H9"}
    assert env.added and env.added[0][0] == "reader"
    summary = env.q.summary(job_id)
    assert (summary["imported"], summary["existing"], summary["duplicate"], summary["failed"]) == (3, 2, 1, 2)
    assert summary["last_book"] == "Title of e.epub" and summary["current"] is None


def test_cancel_running_job_stops_files_being_ingested(env, monkeypatch):
    session = env.maker()
    job = env.ub.OneDriveImportJob(user_id=5, status="running", selection="[]", enumerated=True)
    session.add(job)
    session.commit()
    busy = env.ingest / "onedrive_1_busy.epub"
    busy.write_bytes(b"book")
    queued = env.ingest / "onedrive_2_queued.epub"
    queued.write_bytes(b"book")
    Item = env.ub.OneDriveImportItem
    session.add_all([
        Item(job_id=job.id, seq=1, drive_item_id="1", name="busy.epub", status="ingesting", ingest_path=str(busy)),
        Item(job_id=job.id, seq=2, drive_item_id="2", name="queued.epub", status="ingesting",
             ingest_path=str(queued)),
        Item(job_id=job.id, seq=3, drive_item_id="3", name="later.epub", status="pending"),
    ])
    session.commit()
    job_id = job.id

    assert env.q.summary(job_id, session)["current"] == "busy.epub"
    requested = []
    monkeypatch.setattr(env.queue_mod, "request_ingest_cancel", requested.append)
    # busy.epub is being processed by the ingest service, queued.epub not yet
    monkeypatch.setattr(env.queue_mod, "discard_queued_ingest_file",
                        lambda path: path == str(queued) and (os.remove(path) or True))
    env.fake_ingest.outcomes = {"busy.epub": "cancelled"}
    assert env.q.cancel(job_id, user_id=5) is True

    env.q._process(session, session.get(env.ub.OneDriveImportJob, job_id), graph=None)
    session.close()

    assert requested == [str(busy), str(queued)]
    assert _items(env, job_id) == [("busy.epub", "cancelled"), ("queued.epub", "cancelled"),
                                   ("later.epub", "cancelled")]
    assert _job(env, job_id)[0] == "cancelled"


def test_auth_failure_fails_job(env, monkeypatch):
    client = sys.modules["cps.onedrive_client"]

    class Expired(FakeGraph):
        def item(self, item_id):
            raise client.OneDriveAuthError("revoked")

    _use_graph(env, monkeypatch, Expired({}))
    job_id = env.q.submit(5, "reader", [{"id": "a", "name": "a.epub", "folder": False}])
    env.q._claim_next()
    env.q._run_job(job_id)
    status, error = _job(env, job_id)
    assert status == "failed" and "reconnect" in error


def test_backpressure_waits_for_ingest_folder_to_drain(env, monkeypatch):
    graph = FakeGraph({}, items={"a": _file("a", "a.epub", "H1")})
    _use_graph(env, monkeypatch, graph)
    backlog = [env.queue_mod.MAX_PENDING_INGEST_FILES, env.queue_mod.MAX_PENDING_INGEST_FILES, 0]
    monkeypatch.setattr(env.q, "_ingest_backlog", lambda d: backlog.pop(0) if backlog else 0)
    sleeps = []
    env.q._sleep = sleeps.append
    job_id = env.q.submit(5, "reader", [{"id": "a", "name": "a.epub", "folder": False}])
    env.q._claim_next()
    env.q._run_job(job_id)
    assert len(sleeps) >= 2 and graph.downloads == ["a"]
    assert _items(env, job_id) == [("a.epub", "imported")]


def test_recover_requeues_interrupted_jobs_and_removes_partial_downloads(env):
    session = env.maker()
    job = env.ub.OneDriveImportJob(user_id=5, status="running", selection="[]", enumerated=True)
    done = env.ub.OneDriveImportJob(user_id=5, status="done", selection="[]")
    session.add_all([job, done])
    session.commit()
    job_id, done_id = job.id, done.id
    session.close()
    partial = env.ingest / "onedrive_5_x_book.epub.uploading"
    partial.write_bytes(b"part")
    other = env.ingest / "upload.epub.uploading"
    other.write_bytes(b"part")

    env.q.recover()

    assert _job(env, job_id)[0] == "queued"
    assert _job(env, done_id)[0] == "done"
    assert not partial.exists() and other.exists()
    assert [(u, t.job_id) for u, t in env.added] == [("reader", job_id)]


def test_dispatcher_runs_at_most_two_jobs_in_fifo_order(env, monkeypatch):
    release = threading.Event()
    started = []

    def fake_run(job_id):
        started.append(job_id)
        release.wait(5)

    monkeypatch.setattr(env.q, "_run_job", fake_run)
    ids = [env.q.submit(5, "reader", [{"id": "a", "name": "a", "folder": False}]) for _ in range(3)]

    env.q._dispatch_once()
    for t in list(env.q._running.values()):
        t.join(0.2)
    assert sorted(started) == ids[:2]
    assert [_job(env, i)[0] for i in ids] == ["running", "running", "queued"]
    env.q._dispatch_once()
    assert len(started) == 2  # cap reached

    release.set()
    for t in list(env.q._running.values()):
        t.join(5)
    env.q._dispatch_once()
    for t in list(env.q._running.values()):
        t.join(5)
    assert started[2] == ids[2]


def test_cancel_queued_job(env):
    job_id = env.q.submit(5, "reader", [{"id": "a", "name": "a", "folder": False}])
    assert env.q.cancel(job_id, user_id=99) is False  # someone else's job
    assert env.q.cancel(job_id, user_id=5) is True
    assert _job(env, job_id)[0] == "cancelled"
    assert env.q._claim_next() is None


def test_cancel_running_job_stops_before_next_download(env, monkeypatch):
    graph = FakeGraph({"F": [_file("a", "a.epub", "H1"), _file("b", "b.epub", "H2")]})
    _use_graph(env, monkeypatch, graph)
    job_id = env.q.submit(5, "reader", [{"id": "F", "name": "F", "folder": True}])
    env.q._claim_next()
    original = graph.download

    def download_then_cancel(item_id, dest_path, should_stop=lambda: False):
        result = original(item_id, dest_path, should_stop)
        env.q.cancel(job_id)
        return result

    graph.download = download_then_cancel
    env.q._run_job(job_id)
    assert graph.downloads == ["a"]
    # a.epub was downloaded but not yet picked up by the ingest service: discarded too
    assert _items(env, job_id) == [("a.epub", "cancelled"), ("b.epub", "cancelled")]
    assert _job(env, job_id)[0] == "cancelled"
    assert not list(env.ingest.iterdir())


def test_task_proxy_reports_job_progress(env, monkeypatch):
    graph = FakeGraph({}, items={"a": _file("a", "a.epub", "H1")})
    _use_graph(env, monkeypatch, graph)
    job_id = env.q.submit(5, "reader", [{"id": "a", "name": "a.epub", "folder": False}])
    task = env.added[-1][1]
    worker = sys.modules["cps.services.worker"]
    assert task.stat == worker.STAT_STARTED and str(task.message) == "Queued"
    env.q._claim_next()
    env.q._run_job(job_id)
    task._last_poll = 0
    assert task.stat == worker.STAT_FINISH_SUCCESS
    assert task.progress == 1
    assert "1 imported" in str(task.message)
