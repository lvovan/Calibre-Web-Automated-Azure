# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise the complete RT blueprint with isolated application dependencies."""

import ast
import gzip
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import types
import zipfile
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, abort, flash
from flask_babel import Babel
from flask_wtf.csrf import CSRFProtect
from PIL import Image
from sqlalchemy import Column, Integer, JSON, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base

REPO = Path(__file__).resolve().parents[2]
IE = "Mozilla/5.0 (Windows NT 6.3; ARM; Trident/7.0; Touch; rv:11.0) like Gecko"
pytestmark = pytest.mark.unit


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rt_app(tmp_path, monkeypatch):
    name = "_cwa_rt_test"
    parent = types.ModuleType(name)
    parent.__path__ = [str(REPO / "cps")]
    monkeypatch.setitem(sys.modules, name, parent)
    for key in list(sys.modules):
        if key.startswith(name + "."):
            monkeypatch.delitem(sys.modules, key)
    d = types.ModuleType(name + ".rt._deps")
    monkeypatch.setitem(sys.modules, name + ".rt._deps", d)
    d.constants = load(name + ".constants", REPO / "cps" / "constants.py")
    for module in ("epub_resources", "comic_pages", "pdf_pages"):
        setattr(d, module, load(name + "." + module, REPO / "cps" / (module + ".py")))
    d.Pagination = load(name + ".pagination", REPO / "cps" / "pagination.py").Pagination
    state = SimpleNamespace(viewer=True, authenticated=True, visible=True, download=True)
    user = SimpleNamespace(id=1, locale="en", view_settings={}, role_viewer=lambda: state.viewer,
                           role_download=lambda: state.download, is_authenticated=True)
    d.current_user = user
    d.ub = SimpleNamespace(session=SimpleNamespace(commit=lambda: None, rollback=lambda: None))
    d.config = SimpleNamespace(config_use_google_drive=False, config_rarfile_location="",
                               config_disable_standard_login=False, config_login_type=0,
                               config_remote_login=False, config_anonbrowse=0,
                               config_allow_reverse_proxy_header_login=False,
                               get_book_path=lambda: str(tmp_path))
    entry = SimpleNamespace(id=1, title="Test <book>", path="", authors=[SimpleNamespace(name="Author")],
                            comments=[SimpleNamespace(text='<p>Synopsis<script>alert(1)</script></p>')],
                            data=[SimpleNamespace(format=fmt) for fmt in ("EPUB", "CBZ", "TXT", "M4B", "DJVU")])
    shutil.copyfile(REPO / "tests" / "fixtures" / "sample_books" / "alice_in_wonderland.epub", tmp_path / "sample.epub")
    output = io.BytesIO()
    Image.new("RGB", (2400, 3200), "white").save(output, "JPEG")
    with zipfile.ZipFile(tmp_path / "sample.cbz", "w") as archive:
        archive.writestr("p1.jpg", output.getvalue())
        archive.writestr("p2.jpg", output.getvalue())
    Image.new("RGB", (400, 600), "blue").save(tmp_path / "cover.jpg")
    (tmp_path / "sample.txt").write_text("hello <script>alert(1)</script> " * 2000, encoding="utf-8")
    (tmp_path / "sample.m4b").write_bytes(b"audio")
    (tmp_path / "sample.djvu").write_bytes(b"djvu")
    d.calibre_db = SimpleNamespace(
        get_filtered_book=lambda book_id: entry if state.visible and book_id == 1 else None,
        get_book_format=lambda book_id, fmt: SimpleNamespace(name="sample"),
        fill_indexpage=lambda *args: ([entry], None, d.Pagination(1, 24, 1)),
        get_search_results=lambda *args, **kwargs: ([SimpleNamespace(Books=entry)], 1, d.Pagination(1, 24, 1)))
    column = SimpleNamespace(desc=lambda: None)
    d.db = SimpleNamespace(Books=SimpleNamespace(timestamp=column, sort=column))
    d.log = logging.getLogger(__name__)
    d.shelves = lambda: []
    d.magic_shelves = lambda: []
    d.magic_shelf = SimpleNamespace(build_query_from_rules=lambda *args, **kwargs: True)
    d.bookmark = lambda *args: ""
    d.oauth_bb = SimpleNamespace(oauth_check={})
    d.feature_support = {"oauth": False}
    d.library_search_error = lambda error: "Invalid search: " + str(error)
    d.check_shelf_view_permissions = lambda value: False

    def authenticated(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            if not state.authenticated:
                abort(401)
            return fn(*args, **kwargs)
        return guarded

    namespace = {"wraps": wraps, "abort": abort, "current_user": user, "config": d.config,
                 "login_required": authenticated, "os": os, "calibre_db": d.calibre_db,
                 "PAGED_FORMATS": ("cbz", "cbt", "cbr", "pdf")}
    for file, functions in [("web.py", {"viewer_required", "_epub_path", "_comic_archive_path"}),
                            ("usermanagement.py", {"login_required_if_no_ano"})]:
        tree = ast.parse((REPO / "cps" / file).read_text(encoding="utf-8"))
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
        exec(compile(ast.Module(body=body, type_ignores=[]), file, "exec"), namespace)
        for function in functions:
            setattr(d, function, namespace[function])

    app = Flask(__name__, static_folder=str(REPO / "cps" / "static"))
    app.config.update(SECRET_KEY="rt-test-only", TESTING=True, WTF_CSRF_ENABLED=False)
    Babel(app)
    CSRFProtect(app)
    def modern_index(page=1):
        return "modern"

    app.add_url_rule("/", "web.index", modern_index)
    app.add_url_rule("/page/<int:page>", "web.index", modern_index)
    app.add_url_rule("/search", "search.simple_search", lambda: "modern")
    app.add_url_rule("/advsearch", "search.advanced_search_form", lambda: "modern")
    app.add_url_rule("/newest/stored", "web.books_list", lambda: "modern")
    app.add_url_rule("/book/<int:book_id>", "web.show_book", lambda book_id: "modern")
    app.add_url_rule("/read/<int:book_id>/<book_format>", "web.read_book", lambda **kw: "modern")
    app.add_url_rule("/shelf/<int:shelf_id>", "shelf.show_shelf", lambda **kw: "modern")
    app.add_url_rule("/magicshelf/<int:shelf_id>", "web.render_magic_shelf", lambda **kw: "modern")
    app.add_url_rule("/login", "web.login", lambda: "modern")

    def failed_login():
        from flask import get_flashed_messages
        flash("Wrong password", "error")
        get_flashed_messages()
        return "modern failed login"

    app.add_url_rule("/login", "web.login_post", failed_login, methods=["POST"])
    app.add_url_rule("/logout", "web.logout", lambda: "logout")
    app.add_url_rule("/download/<int:book_id>/<book_format>", "web.download_link", lambda **kw: "download")
    app.add_url_rule("/show/<int:book_id>/<book_format>", "web.serve_book", lambda **kw: "media")
    app.add_url_rule("/comic/<int:book_id>/<book_format>/pages", "web.comic_pages",
                     lambda **kw: {"pages": d.comic_pages.list_pages(tmp_path / "sample.cbz")})
    app.add_url_rule("/ajax/bookmark/<int:book_id>/<book_format>", "web.set_bookmark", lambda **kw: ("", 204), methods=["POST"])

    @app.after_request
    def modern_csp(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response

    module = load(name + ".rt", REPO / "cps" / "rt" / "__init__.py")
    app.register_blueprint(module.rt)
    return SimpleNamespace(app=app, client=app.test_client(), state=state, deps=d, path=tmp_path,
                           routes=sys.modules[name + ".rt.routes"], routing=sys.modules[name + ".rt.routing"])


@pytest.fixture
def preferences_db(rt_app):
    base = declarative_base()

    class User(base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)
        view_settings = Column(JSON)

    engine = create_engine("sqlite://")
    base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            users = [User(id=1, view_settings={"table": {"columns": ["title"]}}),
                     User(id=2, view_settings={})]
            for user in users:
                for name, value in vars(rt_app.deps.current_user).items():
                    if name not in ("id", "view_settings"):
                        setattr(user, name, value)
            session.add_all(users)
            session.commit()
            rt_app.deps.ub.session = session
            rt_app.deps.current_user = users[0]
            yield SimpleNamespace(session=session, users=users)
    finally:
        engine.dispose()


def test_epub_font_persists_per_account_across_books_without_changing_other_settings(rt_app, preferences_db):
    first, second = preferences_db.users
    assert rt_app.client.post("/rt/preferences/epub", data={"font_size": "150", "user_id": "2"}).status_code == 204
    preferences_db.session.expire_all()
    assert first.view_settings == {"table": {"columns": ["title"]}, "rt_epub": {"font_size": 150}}
    assert second.view_settings == {}
    original = rt_app.deps.calibre_db.get_filtered_book(1)
    another = SimpleNamespace(**dict(vars(original), id=2))
    rt_app.deps.calibre_db.get_filtered_book = lambda book_id: {1: original, 2: another}.get(book_id)

    def font_size(book_id):
        response = rt_app.client.get(f"/rt/read/{book_id}/epub")
        assert response.status_code == 200
        payload = response.get_data(as_text=True).split('<script id="rt-config" type="application/json">')[1].split("</script>")[0]
        return json.loads(payload)["fontSize"]

    assert font_size(1) == font_size(2) == 150
    rt_app.deps.current_user = second
    assert font_size(1) == 100


def test_epub_font_validation_permissions_and_csrf(rt_app):
    for value in ("69", "201", "nan", "100.5", ""):
        assert rt_app.client.post("/rt/preferences/epub", data={"font_size": value}).status_code == 400
    assert rt_app.deps.current_user.view_settings == {}
    rt_app.state.viewer = False
    assert rt_app.client.post("/rt/preferences/epub", data={"font_size": "120"}).status_code == 403
    rt_app.state.viewer = True
    rt_app.state.authenticated = False
    assert rt_app.client.post("/rt/preferences/epub", data={"font_size": "120"}).status_code == 401
    rt_app.state.authenticated = True
    rt_app.deps.config.config_anonbrowse = 1
    rt_app.deps.current_user.is_authenticated = False
    assert rt_app.client.post("/rt/preferences/epub", data={"font_size": "120"}).status_code == 401
    rt_app.deps.current_user.is_authenticated = True
    rt_app.deps.config.config_anonbrowse = 0
    rt_app.app.config["WTF_CSRF_ENABLED"] = True
    assert rt_app.client.post("/rt/preferences/epub", data={"font_size": "120"}).status_code == 400
    assert rt_app.deps.current_user.view_settings == {}


def test_epub_font_database_failure_rolls_back_and_is_not_success(rt_app, preferences_db, monkeypatch, caplog):
    def failed_commit():
        raise SQLAlchemyError("test commit failure")

    monkeypatch.setattr(preferences_db.session, "commit", failed_commit)
    response = rt_app.client.post("/rt/preferences/epub", data={"font_size": "150"})
    assert response.status_code == 503
    assert preferences_db.users[0].view_settings == {"table": {"columns": ["title"]}}
    assert "Unable to save RT EPUB font size" in caplog.text


@pytest.mark.parametrize("path,target", [("/", "/rt/"), ("/page/2", "/rt/page/2"),
    ("/search?query=hello", "/rt/?query=hello"), ("/book/1", "/rt/book/1"),
    ("/read/1/epub", "/rt/read/1/epub"), ("/login", "/rt/login"), ("/shelf/2", "/rt/shelf/2"),
    ("/magicshelf/2", "/rt/magicshelf/2")])
def test_detection_redirects_only_legacy_gets(rt_app, path, target):
    assert rt_app.client.get(path).status_code == 200
    response = rt_app.client.get(path, headers={"User-Agent": IE})
    assert response.status_code == 302 and response.location.startswith(target)
    assert "User-Agent" in response.vary and "Cookie" in response.vary
    assert "no-store" in response.headers["Cache-Control"]
    assert rt_app.client.head(path, headers={"User-Agent": IE}).status_code == 302


def test_cookie_escape_and_direct_rt_have_no_loop(rt_app):
    client = rt_app.client
    client.set_cookie("cwa_ui", "full")
    assert client.get("/", headers={"User-Agent": IE}).data == b"modern"
    assert client.get("/rt/").status_code == 200
    client.set_cookie("cwa_ui", "rt")
    response = client.get("/", follow_redirects=True)
    assert response.status_code == 200 and len(response.history) == 1
    assert client.get("/rt/not-a-real-page").status_code == 404


@pytest.mark.parametrize("encoding,expected", [
    ("gzip, deflate", "gzip"), ("br, gzip;q=0.5, deflate", "gzip"),
    ("gzip;q=0, deflate", None), ("identity", None), ("", None),
])
def test_actual_app_compression_serves_trident_decodable_assets(rt_app, encoding, expected):
    from flask_compress import Compress
    Compress(rt_app.app)
    rt_app.app.add_url_rule("/modern-compressed", "modern_asset", lambda: "modern" * 200)
    headers = {"Accept-Encoding": encoding, "User-Agent": IE}
    for path, marker in [("/rt/static/rt.css", b"Segoe UI"),
                         ("/rt/static/reader.js", b"audioReader"),
                         ("/rt/read/1/epub", b"epub.legacy.js")]:
        response = rt_app.client.get(path, headers=headers)
        assert response.status_code == 200
        actual = response.headers.get("Content-Encoding")
        assert actual in (None, "gzip") if expected else actual is None
        if path == "/rt/read/1/epub":
            assert actual == expected
        assert "Accept-Encoding" in response.vary
        body = gzip.decompress(response.data) if actual == "gzip" else response.data
        assert marker in body
    response = rt_app.client.get("/modern-compressed", headers={"Accept-Encoding": "gzip, deflate"})
    assert response.headers["Content-Encoding"] == "gzip"
    response = rt_app.client.get("/static/js/libs/epub.min.js", headers={"Accept-Encoding": "gzip, deflate"})
    assert response.headers["Content-Encoding"] == "deflate"


def test_magic_shelf_stays_in_rt_and_enforces_visibility_and_rules(rt_app):
    value = SimpleNamespace(id=2, name="Magic shelf", user_id=1, is_public=0,
                            rules={"rules": [{"field": "title", "value": "Test"}]})
    rt_app.deps.ub = SimpleNamespace(MagicShelf=SimpleNamespace(id=2),
        session=SimpleNamespace(query=lambda model: SimpleNamespace(
            filter=lambda condition: SimpleNamespace(first=lambda: value if condition else None))))
    calls = []

    def fill(page, pagesize, model, query_filter, order):
        calls.append((page, pagesize, query_filter))
        return [rt_app.deps.calibre_db.get_filtered_book(1)], None, rt_app.deps.Pagination(page, pagesize, 49)

    rt_app.deps.calibre_db.fill_indexpage = fill
    response = rt_app.client.get("/rt/magicshelf/2")
    assert response.status_code == 200
    assert b"/rt/magicshelf/2?page=2" in response.data and b"/rt/book/1" in response.data
    assert calls == [(1, 24, True)]
    assert rt_app.client.get("/rt/magicshelf/3").status_code == 404
    value.user_id = 9
    assert rt_app.client.get("/rt/magicshelf/2").status_code == 403
    value.is_public = 1
    assert rt_app.client.get("/rt/magicshelf/2").status_code == 200
    value.rules = {}
    assert b"No books found" in rt_app.client.get("/rt/magicshelf/2").data
    value.rules = {"rules": [{}]}
    rt_app.deps.magic_shelf.build_query_from_rules = lambda *args, **kwargs: None
    assert rt_app.client.get("/rt/magicshelf/2").status_code == 422
    rt_app.state.authenticated = False
    assert rt_app.client.get("/rt/magicshelf/2").status_code == 401


def test_main_post_is_not_redirected_before_processing_and_flashes_survive(rt_app):
    response = rt_app.client.post("/login", data={"next": "/rt/book/1"}, headers={"User-Agent": IE})
    assert response.status_code == 303 and response.location.startswith("/rt/login?")
    rt_app.deps.current_user.is_authenticated = False
    result = rt_app.client.get(response.location)
    assert b"Wrong password" in result.data and b'action="/login"' in result.data
    rt_app.client.delete_cookie("cwa_ui")
    assert rt_app.client.post("/login").data == b"modern failed login"
    assert rt_app.client.post("/ajax/bookmark/1/EPUB", headers={"User-Agent": IE}).status_code == 204


def test_csrf_and_disabled_standard_login(rt_app):
    rt_app.app.config["WTF_CSRF_ENABLED"] = True
    assert rt_app.client.post("/login", headers={"User-Agent": IE}).status_code == 400
    rt_app.deps.current_user.is_authenticated = False
    rt_app.deps.config.config_disable_standard_login = True
    response = rt_app.client.get("/rt/login")
    assert b'id="password"' not in response.data
    assert b"Standard login is disabled" in response.data


def test_catalog_details_search_and_reader_render_escaped_content(rt_app):
    for path in ("/rt/", "/rt/?query=hello", "/rt/book/1", "/rt/read/1/epub", "/rt/read/1/cbz",
                 "/rt/read/1/txt", "/rt/read/1/m4b", "/rt/read/1/djvu"):
        response = rt_app.client.get(path)
        assert response.status_code == 200, (path, response.data)
        assert b"<script>alert(1)</script>" not in response.data
    assert rt_app.client.get("/rt/?query=" + "a" * 257).status_code == 200
    assert rt_app.client.get("/rt/read/1/txt?page=10000").status_code == 404


def test_cover_and_pages_are_bounded_jpeg_with_authorized_conditionals(rt_app):
    for path, long_edge in [("/rt/cover/1?w=100", 100), ("/rt/page/1/cbz/0?size=800", 800),
                            ("/rt/page/1/cbz/0?size=99999", 2400)]:
        response = rt_app.client.get(path)
        assert response.status_code == 200 and response.mimetype == "image/jpeg"
        with Image.open(io.BytesIO(response.data)) as image:
            assert max(image.size) == long_edge
        etag = response.headers["ETag"]
        assert rt_app.client.get(path, headers={"If-None-Match": etag}).status_code == 304
        rt_app.state.visible = False
        assert rt_app.client.get(path, headers={"If-None-Match": etag}).status_code == 404
        rt_app.state.visible = True


def test_epub_manifest_revision_policy_and_auth(rt_app):
    meta = rt_app.deps.epub_resources.describe(rt_app.path / "sample.epub")
    url = f'/rt/epub/1/epub/{meta["revision"]}/{meta["opf"]}'
    response = rt_app.client.get(url)
    assert response.status_code == 200
    assert "sandbox allow-same-origin" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Security-Policy"] == "sandbox allow-same-origin"
    assert rt_app.client.get(url, headers={"If-None-Match": response.headers["ETag"]}).status_code == 304
    assert rt_app.client.get(url.replace(meta["revision"], "stale")).status_code == 404
    assert rt_app.client.get(url.replace(meta["opf"], "unlisted.txt")).status_code == 404
    for denied, code in [("viewer", 403), ("visible", 404), ("authenticated", 401)]:
        setattr(rt_app.state, denied, False)
        for path in (url, "/rt/read/1/epub", "/rt/page/1/cbz/0"):
            assert rt_app.client.get(path, headers={"If-None-Match": response.headers["ETag"]}).status_code == code
        setattr(rt_app.state, denied, True)


def test_switch_cookie_is_scoped_secure_and_no_open_redirect(rt_app):
    response = rt_app.client.get("/rt/switch?to=full&next=https://evil.example/", base_url="https://localhost")
    cookie = response.headers["Set-Cookie"]
    assert response.location == "/"
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    assert rt_app.client.get("/rt/switch?to=unknown").status_code == 400


def test_download_links_follow_permission_and_missing_files_are_explicit(rt_app):
    rt_app.state.download = False
    assert b"/download/" not in rt_app.client.get("/rt/book/1").data
    rt_app.state.visible = False
    assert rt_app.client.get("/rt/book/1").status_code == 404
    assert rt_app.client.get("/rt/cover/1").status_code == 404


def test_direct_selection_cookie_respects_mount_prefix_and_static_is_not_opt_in(rt_app):
    response = rt_app.client.get("/rt/static/reader.js")
    assert "Set-Cookie" not in response.headers
    response = rt_app.client.get("/rt/", environ_overrides={"SCRIPT_NAME": "/cwa"})
    assert "cwa_ui=rt" in response.headers["Set-Cookie"]
    assert "Path=/cwa" in response.headers["Set-Cookie"]


def test_gwt_script_exception_is_limited_to_djvu_shell(rt_app):
    for book_format in ("epub", "cbz", "m4b", "txt", "djvu"):
        response = rt_app.client.get("/rt/read/1/" + book_format)
        assert ("'unsafe-eval'" in response.headers["Content-Security-Policy"]) == (book_format == "djvu")


def test_mapped_endpoints_exist_in_actual_source():
    # Guard the isolated endpoint allowlist against upstream renames.
    names = {"web": "web.py", "shelf": "shelf.py", "search": "search.py"}
    expected = {"web.index", "web.books_list", "web.show_book", "web.read_book", "web.login",
                "search.simple_search", "search.advanced_search_form", "shelf.show_shelf", "web.render_magic_shelf"}
    for prefix, file in names.items():
        functions = {node.name for node in ast.parse((REPO / "cps" / file).read_text(encoding="utf-8")).body
                     if isinstance(node, ast.FunctionDef)}
        assert {endpoint.split(".")[1] for endpoint in expected if endpoint.startswith(prefix + ".")} <= functions
