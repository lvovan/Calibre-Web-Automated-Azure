# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import ast
import hashlib
import importlib.util
import logging
import os
import zipfile
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask, abort, make_response, request

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("epub_resources", REPO / "cps" / "epub_resources.py")
epub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(epub)
pytestmark = pytest.mark.unit


def make_epub(path, bodies=None, metadata="", attributes="", extras=None, container=None, order=None):
    bodies = bodies or [f'<img src="images/p{i}.png"/>' for i in range(5)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", container or
                         '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        items = []
        for i, body in enumerate(bodies):
            items.extend([f'<item id="p{i}" href="p{i}.xhtml" media-type="application/xhtml+xml"/>',
                          f'<item id="i{i}" href="images/p{i}.png" media-type="image/png"/>'])
            archive.writestr(f"OEBPS/p{i}.xhtml",
                             '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Page</title></head><body>' + body + '</body></html>')
            archive.writestr(f"OEBPS/images/p{i}.png", b"image-" + str(i).encode())
        archive.writestr("OEBPS/book.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata>' + metadata +
                         '</metadata><manifest>' + ''.join(items) + '</manifest><spine ' + attributes + '>' +
                         ''.join(f'<itemref idref="p{i}"/>' for i in (order if order is not None else range(len(bodies)))) + '</spine></package>')
        for name, content in (extras or {}).items():
            archive.writestr(name, content)
    return str(path)


def test_image_epub_uses_spine_order_and_does_not_read_images(tmp_path, monkeypatch):
    order = [4, 2, 0, 3, 1]
    path = make_epub(tmp_path / "manga.epub", attributes='page-progression-direction="rtl"', order=order)
    read = []
    original = epub._read

    def tracked(archive, name, limit):
        read.append(name)
        return original(archive, name, limit)

    monkeypatch.setattr(epub, "_read", tracked)
    data = epub.describe(path)
    assert data["manga"] and data["direction"] == "rtl"
    assert [page["image"] for page in data["pages"]] == [f"OEBPS/images/p{i}.png" for i in order]
    assert not any(name.endswith(".png") for name in read)
    assert epub.read_resource(path, data["revision"], "OEBPS/images/p2.png") == (b"image-2", "image/png")


@pytest.mark.parametrize("body", ["<p>" + "Novel text. " * 100 + "</p>",
                                 '<div style="transform:rotate(90deg)"><img src="images/p0.png"/></div>',
                                 '<img src="images/p0.png"/><p>Caption</p>',
                                 '<img src="images/p0.png"/><img src="images/p1.png"/>'])
def test_complex_pages_are_not_flattened(tmp_path, body):
    path = make_epub(tmp_path / "complex.epub", bodies=[body] * 5)
    assert epub.describe(path)["pages"] == []


def test_converter_comic_pages_use_the_image_renderer(tmp_path):
    # KCC/Calibre comic output: stylesheets, viewport sizing, SVG wrappers and explicit spreads.
    kcc = ('<div style="text-align:center;top:0.0%;"><img width="1072" height="1448" src="images/p{0}.png"/></div>')
    calibre_svg = ('<div><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                   'viewBox="0 0 10 10"><title>Page</title><image width="10" height="10" xlink:href="images/p{0}.png"/></svg></div>')
    bodies = [(kcc if i % 2 else calibre_svg).format(i) for i in range(9)] + ["<h1>Credits</h1><p>Scans by someone</p>"]
    path = make_epub(tmp_path / "kcc.epub", bodies=bodies, metadata='<meta property="rendition:layout">pre-paginated</meta>')
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("OEBPS/style.css", "img{max-width:100%}")
    pages = epub.describe(path)["pages"]
    assert [page.get("image") for page in pages[:9]] == [f"OEBPS/images/p{i}.png" for i in range(9)]
    assert "image" not in pages[9] and pages[9]["href"] == "OEBPS/p9.xhtml"


def test_kindle_panel_view_pages_use_the_image_renderer(tmp_path):
    # KCC Panel View repeats the page raster in hidden zoom panels behind href-less magnify links.
    panel = '<div id="PV-{1}"><a class="app-amzn-magnify" style="display:inline-block"></a></div>' \
            '<div class="PV-P" id="PV-{1}-P"><img style="position:absolute;left:0" src="images/p{0}.png"/></div>'
    page = '<div><img src="images/p{0}.png"/></div><div id="PV">' + "".join(
        panel.replace("{1}", corner) for corner in ("TL", "TR", "BL", "BR")) + "</div>"
    path = make_epub(tmp_path / "panels.epub", bodies=[page.format(i) for i in range(5)])
    assert [page.get("image") for page in epub.describe(path)["pages"]] == [f"OEBPS/images/p{i}.png" for i in range(5)]

    mixed = make_epub(tmp_path / "mixed.epub", bodies=['<img src="images/p0.png"/><img src="images/p1.png"/>'] * 5)
    assert epub.describe(mixed)["pages"] == []


def test_novel_with_cover_is_not_manga(tmp_path):
    path = make_epub(tmp_path / "novel.epub", bodies=['<img src="images/p0.png"/>'] +
                     ['<p>' + 'Chapter prose. ' * 100 + '</p>'] * 20)
    metadata = epub.describe(path)
    assert not metadata["manga"]
    assert [section["index"] for section in metadata["sections"]] == list(range(21))
    assert metadata["sections"][1]["size"] > metadata["sections"][0]["size"]


def test_declared_fixed_layout_is_respected(tmp_path):
    path = make_epub(tmp_path / "fixed.epub", bodies=["<p>Text on fixed pages</p>"] * 5,
                     metadata='<meta property="rendition:layout">pre-paginated</meta>')
    metadata = epub.describe(path)
    assert metadata["manga"] and metadata["layout"] == "pre-paginated"
    assert not metadata["pages"]


def test_ibooks_fixed_layout_options_are_available_on_demand(tmp_path):
    options = '<display_options><platform name="*"><option name="fixed-layout">true</option></platform></display_options>'
    path = make_epub(tmp_path / "ibooks.epub", extras={"META-INF/com.apple.ibooks.display-options.xml": options})
    metadata = epub.describe(path)
    assert metadata["layout"] == "pre-paginated"
    assert epub.read_resource(path, metadata["revision"], "META-INF/com.apple.ibooks.display-options.xml")[0] == options.encode()


@pytest.mark.parametrize("name", ["alice_in_wonderland.epub", "sherlock_holmes.epub"])
def test_existing_text_fixtures_remain_text_books(name):
    metadata = epub.describe(str(REPO / "tests" / "fixtures" / "sample_books" / name))
    assert not metadata["manga"] and not metadata["pages"]


def test_version_and_manifest_restrict_member_access(tmp_path):
    path = make_epub(tmp_path / "book.epub", extras={"private.txt": "secret"})
    metadata = epub.describe(path)
    for version, name in [("stale", "OEBPS/p0.xhtml"), (metadata["revision"], "private.txt"),
                          (metadata["revision"], "../private.txt")]:
        with pytest.raises(KeyError):
            epub.read_resource(path, version, name)


@pytest.mark.parametrize("href", ["../../etc/passwd", "/etc/passwd", "https://example.com/image.jpg", "..%2f..%2fprivate", "..\\private"])
def test_archive_references_cannot_escape(href):
    with pytest.raises(epub.EpubResourceError):
        epub._resolve("OEBPS/book.opf", href)


def test_member_limits_are_enforced(tmp_path, monkeypatch):
    path = make_epub(tmp_path / "book.epub")
    metadata = epub.describe(path)
    monkeypatch.setattr(epub, "MAX_RESOURCE_BYTES", 2)
    with pytest.raises(epub.EpubResourceError, match="size limit"):
        epub.read_resource(path, metadata["revision"], "OEBPS/images/p0.png")


def test_repeated_container_declaration_does_not_rewrite_archive(tmp_path):
    container = '<?xml version="1.0"?><?xml version="1.0"?><container><rootfile full-path="OEBPS/book.opf"/></container>'
    path = make_epub(tmp_path / "book.epub", container=container)
    before = Path(path).read_bytes()
    assert epub.describe(path)["opf"] == "OEBPS/book.opf"
    assert Path(path).read_bytes() == before


def test_metadata_cache_invalidates_on_book_replacement(tmp_path):
    path = make_epub(tmp_path / "book.epub")
    first = epub.describe(path)
    assert epub.describe(path) is first
    make_epub(Path(path), bodies=["<p>new content</p>"])
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    updated = epub.describe(path)
    assert updated["revision"] != first["revision"]
    with pytest.raises(KeyError):
        epub.read_resource(path, first["revision"], "OEBPS/p0.xhtml")


@pytest.fixture
def resource_client(tmp_path):
    path = make_epub(tmp_path / "book.epub")
    app = Flask(__name__)
    web = Blueprint("web", __name__)
    user = SimpleNamespace(viewer=True, authenticated=True)
    library = SimpleNamespace(visible=True)

    def login_required_if_no_ano(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            if not user.authenticated:
                abort(401)
            return fn(*args, **kwargs)
        return guarded

    namespace = {"app": app, "web": web, "os": os, "hashlib": hashlib, "request": request,
                     "wraps": wraps, "abort": abort, "make_response": make_response, "epub_resources": epub,
                     "current_user": SimpleNamespace(role_viewer=lambda: user.viewer),
                     "login_required_if_no_ano": login_required_if_no_ano,
                     "config": SimpleNamespace(config_use_google_drive=False, config_trustedhosts="",
                                            get_book_path=lambda: str(tmp_path)),
                     "calibre_db": SimpleNamespace(get_filtered_book=lambda _: SimpleNamespace(path="") if library.visible else None,
                                                get_book_format=lambda *_: SimpleNamespace(name="book")),
                     "log": logging.getLogger(__name__)}
    tree = ast.parse((REPO / "cps" / "web.py").read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and
                 node.name in {"viewer_required", "_epub_path", "epub_resource", "add_security_headers"}]
    exec(compile(ast.Module(body=functions, type_ignores=[]), "web.py", "exec"), namespace)  # noqa: S102
    app.register_blueprint(web)
    version = epub.describe(path)["revision"]
    url = f"/epub/1/epub/{version}/OEBPS/images/p0.png"
    return app.test_client(), user, library, url


def test_resource_route_is_conditional_private_and_sandboxed(resource_client, monkeypatch):
    client, _, _, url = resource_client
    response = client.get(url)
    assert response.status_code == 200 and response.data == b"image-0"
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, no-cache"
    assert "sandbox allow-same-origin" in response.headers["Content-Security-Policy"]

    def unexpected(*args):
        pytest.fail("Conditional request should not read image bytes")

    monkeypatch.setattr(epub, "read_resource", unexpected)
    assert client.get(url, headers={"If-None-Match": response.headers["ETag"]}).status_code == 304


def test_resource_authorization_is_checked_even_for_conditional_requests(resource_client):
    client, user, library, url = resource_client
    etag = client.get(url).headers["ETag"]
    library.visible = False
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 404
    library.visible = True
    user.viewer = False
    assert client.get(url).status_code == 403
    user.viewer = True
    user.authenticated = False
    assert client.get(url).status_code == 401


def test_resource_route_rejects_stale_and_unlisted_resources(resource_client):
    client, _, _, url = resource_client
    assert client.get(url.replace("OEBPS/images/p0.png", "private.txt")).status_code == 404
    assert client.get(url.replace("/epub/1/epub/", "/epub/1/pdf/")).status_code == 404
    assert client.get("/epub/1/epub/stale/OEBPS/images/p0.png").status_code == 404
