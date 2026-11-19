# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Serves the pages of a PDF one at a time as images, for the page-by-page (comic) reader.

Scanned pages that are a single JPEG are sent byte for byte without decoding. Other pages are
rendered with PDFium and sent as JPEG. PDFium is not thread-safe, so every call holds one lock.
"""

import io
import os
import threading
from collections import OrderedDict

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    from PIL import Image  # noqa: F401 - needed by PdfBitmap.to_pil()
    available = True
except ImportError:  # pragma: no cover - optional dependency
    pdfium = pdfium_c = None
    available = False

DEFAULT_SIZE = 1600
MIN_SIZE = 400
MAX_SIZE = 3200
JPEG_QUALITY = 82
# Image-only pages: little or no extractable text (page numbers, a scanner watermark).
MAX_TEXT_CHARS = 40
# A scan may be letterboxed onto a page of another aspect ratio.
MIN_IMAGE_COVER = 0.8
_OPEN_DOCUMENTS = 4
# FPDF_COLORSPACE_DEVICEGRAY, DEVICERGB, CALGRAY, CALRGB, ICCBASED: colors browsers show as-is.
_PASSTHROUGH_COLORSPACES = {1, 2, 4, 5, 7}
_TEXT_RENDER_INVISIBLE = 3

_lock = threading.RLock()
_documents = OrderedDict()
_kinds = {}


class PdfPageError(Exception):
    pass


def _key(path):
    stat = os.stat(path)
    return path, stat.st_mtime_ns, stat.st_size


def _document(path):
    key = _key(path)
    document = _documents.pop(key, None)
    if document is None:
        try:
            document = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as ex:
            raise PdfPageError(str(ex)) from ex
        while len(_documents) >= _OPEN_DOCUMENTS:
            _documents.popitem(last=False)[1].close()
    _documents[key] = document
    return key, document


def _require():
    if not available:
        raise PdfPageError("pypdfium2 is not installed")


def list_pages(path):
    """Returns the pages of a PDF: [{'name', 'mime', 'w', 'h'}], sizes in points (for the aspect ratio)."""
    _require()
    with _lock:
        _, document = _document(path)
        pages = []
        for index in range(len(document)):
            width, height = document.get_page_size(index)
            pages.append({'name': f"page-{index + 1:04d}.jpg", 'mime': 'image/jpeg',
                          'w': round(width), 'h': round(height)})
        return pages


def _sample_pages(total):
    return sorted({min(total - 1, 1), total // 2, max(0, total - 2)}) if total else []


def _visible_text_chars(page):
    textpage = page.get_textpage()
    try:
        return textpage.count_chars()
    finally:
        textpage.close()


def is_image_only(path):
    """True if sampled pages are pictures with (almost) no text, e.g. scanned comics and manga."""
    _require()
    with _lock:
        key, document = _document(path)
        if key not in _kinds:
            result = len(document) > 0
            for index in _sample_pages(len(document)):
                page = document[index]
                try:
                    has_image = any(True for _ in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2))
                    if not has_image or _visible_text_chars(page) > MAX_TEXT_CHARS:
                        result = False
                        break
                finally:
                    page.close()
            _kinds[key] = result
        return _kinds[key]


def _single_jpeg(page):
    """Returns the bytes of the page's only visible object if it is a plain, upright JPEG covering the page."""
    if page.get_rotation():
        return None
    image = None
    for obj in page.get_objects(max_depth=2):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE and image is None:
            image = obj
        elif not (obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT
                  and pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw) == _TEXT_RENDER_INVISIBLE):
            return None
    if image is None or image.get_filters() != ['DCTDecode']:
        return None
    matrix = image.get_matrix()
    if matrix.b or matrix.c or matrix.a <= 0 or matrix.d <= 0:
        return None
    metadata = image.get_metadata()
    if metadata.colorspace not in _PASSTHROUGH_COLORSPACES or metadata.bits_per_pixel not in (8, 24):
        return None
    width, height = page.get_size()
    left, bottom, right, top = image.get_bounds()
    if (right - left) * (top - bottom) < MIN_IMAGE_COVER * width * height:
        return None
    data = bytes(image.get_data(decode_simple=False))
    return data if data[:3] == b'\xff\xd8\xff' else None


def _render(page, size):
    width, height = page.get_size()
    if page.get_rotation() % 180:
        width, height = height, width
    bitmap = page.render(scale=size / max(width, height, 1))
    try:
        image = bitmap.to_pil()
    finally:
        bitmap.close()
    if image.mode != 'RGB':
        image = image.convert('RGB')
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=JPEG_QUALITY, optimize=False)
    return out.getvalue()


def clamp_size(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return DEFAULT_SIZE
    return max(MIN_SIZE, min(MAX_SIZE, size))


def read_page(path, index, size=DEFAULT_SIZE):
    """Returns (JPEG bytes, mime type) of a zero-based page, at most `size` pixels on its long side
    when rendered. Raises IndexError for pages that don't exist."""
    _require()
    with _lock:
        _, document = _document(path)
        if index < 0 or index >= len(document):
            raise IndexError(index)
        try:
            page = document[index]
        except pdfium.PdfiumError as ex:
            raise PdfPageError(str(ex)) from ex
        try:
            data = _single_jpeg(page)
            return (data or _render(page, clamp_size(size))), 'image/jpeg'
        except pdfium.PdfiumError as ex:
            raise PdfPageError(str(ex)) from ex
        finally:
            page.close()
