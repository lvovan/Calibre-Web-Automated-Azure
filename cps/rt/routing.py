# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from flask import g, request, redirect, url_for, session, get_flashed_messages

from . import rt

MAPPED = {"web.index", "web.books_list", "web.show_book", "web.read_book", "web.render_magic_shelf",
          "search.simple_search", "search.advanced_search_form", "shelf.show_shelf", "web.login"}
EPUB_POLICY = ("default-src 'none'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
               "font-src 'self' data:; media-src 'none'; base-uri 'self'; form-action 'none'; "
               "sandbox allow-same-origin")


def is_legacy_client():
    if not hasattr(g, "rt_client"):
        choice = request.cookies.get("cwa_ui")
        g.rt_client = choice == "rt" if choice in ("full", "rt") else "Trident/" in request.user_agent.string
    return g.rt_client


@rt.before_app_request
def select_interface():
    if request.blueprint == "rt":
        # Trident's stylesheet/script loader cannot decode Flask-Compress deflate.
        request.environ["HTTP_ACCEPT_ENCODING"] = "gzip" if request.accept_encodings["gzip"] > 0 else "identity"
    if request.method not in ("GET", "HEAD") or request.endpoint not in MAPPED or not is_legacy_client():
        return None
    args = request.view_args or {}
    endpoint = request.endpoint
    if endpoint == "web.read_book":
        target = url_for("rt.reader", book_id=args["book_id"], book_format=args["book_format"])
    elif endpoint == "web.show_book":
        target = url_for("rt.book", book_id=args["book_id"])
    elif endpoint == "web.login":
        target = url_for("rt.login", next=request.args.get("next", url_for("rt.index")))
    elif endpoint == "shelf.show_shelf":
        target = url_for("rt.shelf", shelf_id=args["shelf_id"], page=args.get("page", 1))
    elif endpoint == "web.render_magic_shelf":
        target = url_for("rt.magic_shelf", shelf_id=args["shelf_id"], page=args.get("page", 1))
    elif endpoint.startswith("search.") or (endpoint == "web.books_list" and args.get("data") == "search"):
        target = url_for("rt.index", query=request.args.get("query", ""), page=args.get("page", 1))
    else:
        target = url_for("rt.index", page=args.get("page", 1))
    response = redirect(target)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def response_headers(response):
    if request.endpoint in MAPPED or request.endpoint == "web.login_post":
        response.vary.update(("User-Agent", "Cookie"))
    if (request.endpoint == "web.login_post" and is_legacy_client()
            and response.status_code == 200 and not response.is_json):
        # The main login renderer consumes flashes. Preserve them across the bounce.
        session["_flashes"] = get_flashed_messages(with_categories=True)
        response = redirect(url_for("rt.login", next=request.form.get("next", url_for("rt.index"))), 303)
        response.vary.update(("User-Agent", "Cookie"))
        response.headers["Cache-Control"] = "private, no-store"
    if request.blueprint == "rt":
        response.headers["X-UA-Compatible"] = "IE=edge"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.vary.add("Cookie")
        if request.endpoint in {"rt.index", "rt.book", "rt.reader", "rt.shelf", "rt.magic_shelf", "rt.login"}:
            if request.cookies.get("cwa_ui") != "rt":
                response.set_cookie("cwa_ui", "rt", max_age=31536000, path=request.script_root or "/",
                                    httponly=True, secure=request.is_secure, samesite="Lax")
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "private, no-store"
        if request.endpoint == "rt.epub_resource":
            response.headers["Content-Security-Policy"] = EPUB_POLICY
            response.headers["X-Content-Security-Policy"] = "sandbox allow-same-origin"
        else:
            script_policy = "'self'"
            if request.endpoint == "rt.reader" and (request.view_args or {}).get("book_format", "").lower() in ("djvu", "djv"):
                # The existing GWT bootstrap evaluates its own compiled code.
                script_policy += " 'unsafe-inline' 'unsafe-eval'"
            response.headers["Content-Security-Policy"] = (
                f"default-src 'self'; script-src {script_policy}; style-src 'self' 'unsafe-inline' blob:; "
                "img-src 'self' data: blob:; font-src 'self' data: blob:; "
                "frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'")
    return response


@rt.record_once
def install_headers(state):
    # Flask executes application response hooks in reverse order. Run after the
    # main site's CSP writer so it cannot overwrite publication sandbox headers.
    state.app.after_request_funcs.setdefault(None, []).insert(0, response_headers)
