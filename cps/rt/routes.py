# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import io
import os
import warnings
from pathlib import Path

import bleach
import chardet
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import abort, flash, make_response, redirect, render_template, request, url_for
from flask_babel import gettext as _
from werkzeug.exceptions import HTTPException

from . import rt, _deps as d

PAGE_SIZE = 24
TEXT_PAGE = 20000
MAX_TEXT = 8 * 1024 * 1024
FORMATS = {"epub", "kepub", "pdf", "cbz", "cbr", "cbt", "txt", "djvu", "djv"}
FORMATS.update(d.constants.EXTENSIONS_AUDIO)


def render(template, **context):
    return render_template("rt/" + template, user=d.current_user, config=d.config, **context)


def get_book(book_id):
    book = d.calibre_db.get_filtered_book(book_id)
    if not book:
        abort(404)
    return book


def local_path(book_id, book_format):
    book = get_book(book_id)
    if d.config.config_use_google_drive:
        abort(422, description=_("The RT reader requires a locally mounted library."))
    if book_format not in FORMATS:
        abort(404)
    data = d.calibre_db.get_book_format(book_id, book_format.upper())
    if not data:
        abort(404)
    root = Path(d.config.get_book_path()).resolve()
    path = (root / book.path / (data.name + "." + book_format)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        abort(404)
    return path


@rt.errorhandler(HTTPException)
def error_page(error):
    return render("error.html", title=_("Unable to open this page"), error=error), error.code


@rt.route("/login")
def login():
    if d.current_user.is_authenticated:
        return redirect(url_for("rt.index"))
    return render("login.html", title=_("Login"), next_url=request.args.get("next", url_for("rt.index")),
                  oauth_check=d.oauth_bb.oauth_check if d.feature_support["oauth"] else {})


@rt.route("/switch")
def switch():
    choice = request.args.get("to")
    if choice not in ("full", "rt"):
        abort(400)
    response = redirect(url_for("web.index" if choice == "full" else "rt.index"))
    response.set_cookie("cwa_ui", choice, max_age=31536000, path=request.script_root or "/",
                        httponly=True, secure=request.is_secure, samesite="Lax")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@rt.route("/", defaults={"page": 1})
@rt.route("/page/<int:page>")
@d.login_required_if_no_ano
def index(page=1):
    page = max(1, request.args.get("page", page, type=int))
    query = request.args.get("query", "").strip()
    if query:
        from ..library_search import parse_library_search, SearchSyntaxError
        try:
            parsed = parse_library_search(query)
            entries, count, pagination = d.calibre_db.get_search_results(
                query, d.config, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE, parsed=parsed)
            entries = [row.Books for row in entries]
        except SearchSyntaxError as error:
            flash(d.library_search_error(error), "error")
            entries, pagination = [], d.Pagination(1, PAGE_SIZE, 0)
    else:
        entries, random_books, pagination = d.calibre_db.fill_indexpage(
            page, PAGE_SIZE, d.db.Books, True, [d.db.Books.timestamp.desc()])
    return render("catalog.html", title=_("Search") if query else _("Books"), entries=entries,
                  pagination=pagination, query=query, shelves=d.shelves(), magic_shelves=d.magic_shelves(),
                  shelf_id=None, shelf_endpoint="rt.shelf")


@rt.route("/shelf/<int:shelf_id>")
@d.login_required_if_no_ano
def shelf(shelf_id):
    value = d.ub.session.query(d.ub.Shelf).filter(d.ub.Shelf.id == shelf_id).first()
    if not value or not d.check_shelf_view_permissions(value):
        abort(404)
    page = max(1, request.args.get("page", 1, type=int))
    ids = [row.book_id for row in d.ub.session.query(d.ub.BookShelf).filter(
        d.ub.BookShelf.shelf == shelf_id).order_by(d.ub.BookShelf.order).all()]
    entries, random_books, pagination = d.calibre_db.fill_indexpage(
        page, PAGE_SIZE, d.db.Books, d.db.Books.id.in_(ids), [d.db.Books.sort])
    return render("catalog.html", title=value.name, entries=entries, pagination=pagination,
                  query="", shelves=d.shelves(), magic_shelves=d.magic_shelves(),
                  shelf_id=shelf_id, shelf_endpoint="rt.shelf")


@rt.route("/magicshelf/<int:shelf_id>")
@d.login_required_if_no_ano
def magic_shelf(shelf_id):
    value = d.ub.session.query(d.ub.MagicShelf).filter(d.ub.MagicShelf.id == shelf_id).first()
    if not value:
        abort(404)
    if value.user_id != d.current_user.id and value.is_public != 1:
        abort(403)
    page = max(1, request.args.get("page", 1, type=int))
    entries, pagination = [], d.Pagination(page, PAGE_SIZE, 0)
    if value.rules and value.rules.get("rules"):
        query_filter = d.magic_shelf.build_query_from_rules(value.rules, user_id=value.user_id)
        if query_filter is None:
            d.log.warning("RT magic shelf %s has invalid rules", shelf_id)
            abort(422, description=_("Error loading magic shelf"))
        entries, random_books, pagination = d.calibre_db.fill_indexpage(
            page, PAGE_SIZE, d.db.Books, query_filter, [d.db.Books.sort])
    return render("catalog.html", title=value.name, entries=entries, pagination=pagination,
                  query="", shelves=d.shelves(), magic_shelves=d.magic_shelves(),
                  shelf_id=shelf_id, shelf_endpoint="rt.magic_shelf")


@rt.route("/book/<int:book_id>")
@d.login_required_if_no_ano
def book(book_id):
    entry = get_book(book_id)
    description = "\n".join(bleach.clean(item.text, tags=[], strip=True) for item in entry.comments)
    return render("book.html", title=entry.title, entry=entry, description=description, formats=FORMATS)


def jpeg(data, size):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            original.thumbnail((size, size), Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(original).convert("RGB")
            output = io.BytesIO()
            image.save(output, "JPEG", quality=82)
    return output.getvalue()


def cached_image(data, etag):
    response = make_response("", 304) if request.if_none_match.contains(etag) else make_response(data)
    response.mimetype = "image/jpeg"
    response.set_etag(etag)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@rt.route("/cover/<int:book_id>")
@d.login_required_if_no_ano
def cover(book_id):
    entry = get_book(book_id)
    root = Path(d.config.get_book_path()).resolve()
    path = (root / entry.path / "cover.jpg").resolve()
    size = max(80, min(600, request.args.get("w", 200, type=int)))
    try:
        if not path.is_relative_to(root):
            abort(404)
        if path.is_file():
            stamp = path.stat()
            etag = f"rt-cover-{book_id}-{stamp.st_mtime_ns}-{stamp.st_size}-{size}"
            if request.if_none_match.contains(etag):
                return cached_image(b"", etag)
            with path.open("rb") as source:
                data = source.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                abort(413)
            return cached_image(jpeg(data, size), etag)
        output = io.BytesIO()
        Image.new("RGB", (120, 160), "#dddddd").save(output, "JPEG")
        return cached_image(output.getvalue(), "rt-empty-cover-1")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        d.log.warning("RT cover failed for book %s: %s", book_id, error)
        abort(422)


@rt.route("/page/<int:book_id>/<book_format>/<int:index>")
@d.login_required_if_no_ano
@d.viewer_required
def page_image(book_id, book_format, index):
    book_format = book_format.lower()
    path = d._comic_archive_path(book_id, book_format)
    if not path:
        abort(404)
    size = max(800, min(2400, request.args.get("size", 1600, type=int)))
    try:
        stamp = os.stat(path)
        etag = f"rt-page-{book_id}-{book_format}-{stamp.st_mtime_ns}-{stamp.st_size}-{index}-{size}"
        if request.if_none_match.contains(etag):
            return cached_image(b"", etag)
        if book_format == "pdf":
            data, _ = d.pdf_pages.read_page(path, index, size)
        else:
            data, _ = d.comic_pages.read_page(path, index, d.config.config_rarfile_location)
        return cached_image(jpeg(data, size), etag)
    except IndexError:
        abort(404)
    except (OSError, ValueError, d.comic_pages.ComicArchiveError, d.pdf_pages.PdfPageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        d.log.warning("RT page %s failed for book %s: %s", index, book_id, error)
        abort(422)


@rt.route("/epub/<int:book_id>/<book_format>/<version>/<path:resource>")
@d.login_required_if_no_ano
@d.viewer_required
def epub_resource(book_id, book_format, version, resource):
    path = d._epub_path(book_id, book_format)
    if not path:
        abort(404)
    try:
        meta = d.epub_resources.describe(path)
        if version != meta["revision"] or resource not in meta["resources"]:
            abort(404)
        etag = hashlib.sha256((str(book_id) + version + resource).encode()).hexdigest()
        if request.if_none_match.contains(etag):
            response = make_response("", 304)
        else:
            data, mime = d.epub_resources.read_resource(path, version, resource)
            response = make_response(data)
            response.mimetype = mime
        response.set_etag(etag)
        response.headers["Cache-Control"] = "private, no-cache"
        return response
    except KeyError:
        abort(404)
    except (d.epub_resources.EpubResourceError, OSError) as error:
        d.log.warning("RT EPUB resource failed for book %s: %s", book_id, error)
        abort(422)


@rt.route("/read/<int:book_id>/<book_format>")
@d.login_required_if_no_ano
@d.viewer_required
def reader(book_id, book_format):
    entry = get_book(book_id)
    book_format = book_format.lower()
    if book_format not in FORMATS:
        flash(_("This format is not supported by the RT reader. Use Download or the full site."), "error")
        return redirect(url_for("rt.book", book_id=book_id))
    path = local_path(book_id, book_format)
    data = {"format": book_format, "bookmark": d.bookmark(book_id, book_format),
            "saveUrl": url_for("web.set_bookmark", book_id=book_id, book_format=book_format.upper())
            if d.current_user.is_authenticated else None}
    text, pagination = "", None
    if book_format in ("epub", "kepub"):
        try:
            meta = d.epub_resources.describe(path)
            data.update(kind="epub", opf=url_for("rt.epub_resource", book_id=book_id,
                        book_format=book_format, version=meta["revision"], resource=meta["opf"]))
        except (d.epub_resources.EpubResourceError, OSError) as error:
            d.log.warning("RT EPUB failed for book %s: %s", book_id, error)
            abort(422)
    elif book_format in ("pdf", "cbz", "cbt", "cbr"):
        data.update(kind="pages", listUrl=url_for("web.comic_pages", book_id=book_id, book_format=book_format),
                    pageUrl=url_for("rt.page_image", book_id=book_id, book_format=book_format, index=0))
    elif book_format == "txt":
        if path.stat().st_size > MAX_TEXT:
            abort(413, description=_("This text file is too large for the RT reader. Use Download instead."))
        with path.open("rb") as source:
            raw = source.read(MAX_TEXT + 1)
        if len(raw) > MAX_TEXT:
            abort(413)
        encoding = chardet.detect(raw[:65536])["encoding"] or "utf-8"
        content = raw.decode(encoding, errors="replace")
        page = max(1, request.args.get("page", 1, type=int))
        if "page" not in request.args:
            saved = data["bookmark"]
            page = int(saved) + 1 if saved.isdigit() else 1
        pagination = d.Pagination(page, TEXT_PAGE, len(content))
        if page > max(1, pagination.pages):
            abort(404)
        text = content[(page - 1) * TEXT_PAGE:page * TEXT_PAGE]
        data.update(kind="txt", page=page - 1)
    elif book_format in ("djvu", "djv"):
        data.update(kind="djvu")
    else:
        data.update(kind="audio")
    return render("reader.html", title=entry.title, entry=entry, reader=data, text=text,
                  pagination=pagination, book_format=book_format)


@rt.route("/<path:unknown>")
def fallback(unknown):
    abort(404)
