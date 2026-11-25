# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""The only adapter between the RT interface and application internals."""

from .. import calibre_db, config, db, ub, logger, constants
from .. import comic_pages, epub_resources, pdf_pages
from ..cw_login import current_user
from ..pagination import Pagination
from ..usermanagement import login_required_if_no_ano
from ..web import viewer_required, _epub_path, _comic_archive_path, oauth_bb, feature_support
from ..shelf import check_shelf_view_permissions
from ..search import library_search_error

__all__ = ["calibre_db", "config", "db", "ub", "constants", "comic_pages", "epub_resources",
           "pdf_pages", "current_user", "Pagination", "login_required_if_no_ano", "viewer_required",
           "_epub_path", "_comic_archive_path", "oauth_bb", "feature_support",
           "check_shelf_view_permissions", "library_search_error", "log", "bookmark", "shelves"]

log = logger.create()


def bookmark(book_id, book_format):
    if not current_user.is_authenticated:
        return ""
    value = ub.session.query(ub.Bookmark).filter(
        ub.Bookmark.user_id == int(current_user.id), ub.Bookmark.book_id == book_id,
        ub.Bookmark.format == book_format.upper()).first()
    return value.bookmark_key if value else ""


def shelves():
    from sqlalchemy import or_
    return ub.session.query(ub.Shelf).filter(or_(
        ub.Shelf.is_public == 1, ub.Shelf.user_id == current_user.id
    )).order_by(ub.Shelf.name).limit(100).all()
