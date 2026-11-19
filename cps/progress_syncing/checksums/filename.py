#!/usr/bin/env python3
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
File-name document identifiers.

Besides the partial MD5 of the file contents, KOReader can identify a document by the MD5 of its
file name, and CrossPoint e-readers do so by default. Such hashes cannot be computed from the
library files, so the names a device is likely to have saved a book under are derived from its
metadata instead: CrossPoint's OPDS download names ("Author - Title", "Title - Author", "Title")
and the names CWA itself serves downloads under.
"""

import hashlib
from collections.abc import Iterable

FILENAME_CHECKSUM_VERSION = "filename"

_RESERVED = set('/\\:*?"<>|')
_MAX_BYTES = 100


def sanitize_filename(name: str, max_bytes: int = _MAX_BYTES) -> str:
    """CrossPoint's sanitizeFilename: the base name it saves OPDS downloads under."""
    name = name.lstrip(" .")
    out = []
    size = 0
    for char in name:
        if char in _RESERVED:
            char = "_"
        elif ord(char) < 32 or ord(char) == 127:
            continue
        length = len(char.encode("utf-8"))
        if size + length > max_bytes:
            break
        out.append(char)
        size += length
    return "".join(out).rstrip(" .") or "book"


def filename_hash(name: str) -> str:
    """KOReader / CrossPoint file-name document id: MD5 of the base name."""
    return hashlib.md5(name.rsplit("/", 1)[-1].encode("utf-8")).hexdigest()


def _author_variants(authors: list[str]) -> list[str]:
    # Calibre stores commas inside an author name as "|" and OPDS feeds emit it as is.
    names = [author for author in authors if author]
    variants = []
    if names:
        variants += [names[0], names[0].replace("|", ",")]
    if len(names) > 1:
        for sep in (", ", " & ", "; "):
            variants += [sep.join(names), sep.join(name.replace("|", ",") for name in names)]
    return list(dict.fromkeys(variants))


def _extensions(book_format: str) -> list[str]:
    ext = book_format.lower()
    return [".kepub.epub", ".kepub"] if ext == "kepub" else ["." + ext]


def candidate_filenames(title: str, authors: list[str], book_format: str,
                        data_name: str | None = None, book_id: int | None = None) -> set[str]:
    """Names a device may have stored this book format under."""
    title = title or ""
    bases = [title]
    for author in _author_variants(authors):
        bases += [f"{author} - {title}", f"{title} - {author}"]
    if data_name:
        bases.append(data_name)
    if book_id is not None:
        bases.append(str(book_id))
    names = set()
    for base in bases:
        for ext in _extensions(book_format):
            names.add(sanitize_filename(base) + ext)
            if base:
                names.add(base + ext)
    return names


def filename_hashes(title: str, authors: list[str], formats: Iterable[str],
                    data_name: str | None = None, book_id: int | None = None) -> dict[str, str]:
    """{file-name hash: format} for every format of a book."""
    hashes = {}
    for book_format in formats:
        for name in candidate_filenames(title, authors, book_format, data_name, book_id):
            hashes.setdefault(filename_hash(name), book_format.upper())
    return hashes
