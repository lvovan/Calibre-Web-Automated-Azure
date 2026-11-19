# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Bounded, versioned access to EPUB members without unpacking the book to disk."""

import os
import posixpath
import re
import threading
import zipfile
from collections import OrderedDict
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_INSPECTION_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 20000
_CACHE_SIZE = 16
_cache = OrderedDict()
_cache_lock = threading.Lock()
_RASTER_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"}
_DOCUMENT_TYPES = {"application/xhtml+xml", "image/svg+xml"}
_INVISIBLE = {"head", "style", "script", "title", "desc", "metadata", "noscript"}
_ACTIVE = {"script", "video", "audio", "canvas", "iframe", "object", "embed", "form", "input", "foreignObject"}
# Share of reading-order pages that must be lone images before the image renderer is used.
_IMAGE_PAGE_SHARE = 0.8
# Inline styling that changes how the image is drawn cannot be dropped.
_TRANSFORM = re.compile(r"transform|rotate|writing-mode|clip-path|mask", re.IGNORECASE)


class EpubResourceError(Exception):
    pass


def revision(path):
    st = os.stat(path)
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def _resolve(base, href):
    url = urlsplit(href)
    if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
        raise EpubResourceError("EPUB resource must be an archive-relative path")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(url.path)))
    if path == ".." or path.startswith(("../", "/")) or "\\" in path or "\x00" in path:
        raise EpubResourceError("EPUB resource escapes the archive")
    return path


def _read(archive, name, limit):
    info = archive.getinfo(name)
    if info.is_dir() or info.flag_bits & 1 or info.file_size > limit:
        raise EpubResourceError("EPUB resource is encrypted or exceeds the size limit")
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise EpubResourceError("EPUB resource exceeds the size limit")
    return data


def _xml(archive, name):
    data = _read(archive, name, MAX_DOCUMENT_BYTES)
    # Internal entity expansion is unnecessary for publication metadata/content.
    if b"<!ENTITY" in data.upper():
        raise EpubResourceError("EPUB XML entity declarations are not supported")
    if name == "META-INF/container.xml":
        # Match the existing reader's repair for repeated XML declarations without rewriting the ZIP.
        declarations = list(re.finditer(br"<\?xml[^>]*\?>", data))
        if len(declarations) > 1:
            end = declarations[0].end()
            data = data[:end] + re.sub(br"<\?xml[^>]*\?>", b"", data[end:])
    return ET.fromstring(data)


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _visible_text(node):
    if _tag(node) in _INVISIBLE:
        return ""
    return (node.text or "") + "".join(_visible_text(child) + (child.tail or "") for child in node)


def _inspect_page(archive, item, resources):
    """Return (image_focused, image) where image is set only for a page that shows nothing but one raster.

    Comic/manga converters (KCC, Calibre) wrap each scan in styled XHTML, often inside an SVG
    <image>; such pages can be shown as the raster alone. Pages with any visible text, several distinct
    images or interactive/media content keep their XHTML.
    """
    if item["mime"] in _RASTER_TYPES:
        return True, item["href"]
    if item["mime"] not in _DOCUMENT_TYPES:
        return False, None
    doc = _xml(archive, item["href"])
    body = next((node for node in doc.iter() if _tag(node) == "body"), doc)
    images = [node for node in body.iter() if _tag(node) in ("img", "image")]
    text = " ".join(_visible_text(body).split())
    image_focused = bool(images) and len(text) <= 80
    if text or not images or any(_tag(node) in _ACTIVE or _TRANSFORM.search(node.get("style", "") + (
            node.text or "" if _tag(node) == "style" else "")) for node in doc.iter()):
        return image_focused, None
    # Kindle Panel View (KCC) repeats the page raster in hidden zoom panels: still one image.
    targets = set()
    for image in images:
        href = image.get("src") if _tag(image) == "img" else (
            image.get("{http://www.w3.org/1999/xlink}href") or image.get("href"))
        try:
            targets.add(_resolve(item["href"], href or ""))
        except EpubResourceError:
            return image_focused, None
    if len(targets) != 1:
        return image_focused, None
    target = targets.pop()
    return image_focused, target if resources.get(target) in _RASTER_TYPES else None


def describe(path):
    """Return small metadata only. Never read image bytes or retain open ZIP handles."""
    version = revision(path)
    key = (os.fspath(path), version)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    try:
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_ENTRIES:
                raise EpubResourceError("EPUB contains too many resources")
            container = _xml(archive, "META-INF/container.xml")
            root = next((node for node in container.iter() if _tag(node) == "rootfile"), None)
            if root is None:
                raise EpubResourceError("EPUB has no package document")
            opf = _resolve("", root.get("full-path", ""))
            package = _xml(archive, opf)
            manifest = {}
            resources = {opf: "application/oebps-package+xml"}
            for node in package.findall("{*}manifest/{*}item"):
                try:
                    href = _resolve(opf, node.get("href", ""))
                except EpubResourceError:
                    # External resources are never proxied by the reader.
                    continue
                mime = node.get("media-type", "application/octet-stream")
                manifest[node.get("id")] = {"href": href, "mime": mime}
                resources[href] = mime
            spine_node = package.find("{*}spine")
            if spine_node is None:
                raise EpubResourceError("EPUB has no reading order")
            spine = []
            for index, node in enumerate(spine_node):
                if node.get("linear", "yes") == "no":
                    continue
                item = manifest.get(node.get("idref"))
                if item is None:
                    raise EpubResourceError("EPUB reading order references a missing resource")
                spine.append(dict(item, index=index, properties=node.get("properties", "")))
            if not spine:
                raise EpubResourceError("EPUB reading order is empty")
            layout = next((node.text for node in package.findall("{*}metadata/{*}meta")
                           if node.get("property") == "rendition:layout"), "reflowable")
            display_options = "META-INF/com.apple.ibooks.display-options.xml"
            names = set(archive.namelist())
            if display_options in names:
                resources[display_options] = "application/xml"
                options = _xml(archive, display_options)
                if any(node.get("name") == "fixed-layout" and node.text == "true" for node in options.iter()):
                    layout = "pre-paginated"
            sample_indices = sorted({0, len(spine) // 4, len(spine) // 2,
                                     3 * len(spine) // 4, len(spine) - 1})
            inspected = {}
            for index in sample_indices:
                try:
                    inspected[index] = _inspect_page(archive, spine[index], resources)
                except (KeyError, ET.ParseError, EpubResourceError):
                    inspected[index] = (False, None)
            recommended = (layout == "pre-paginated" or
                           sum(result[0] for result in inspected.values()) / len(inspected) >= 0.8)
            pages = []
            if recommended and sum(bool(result[1]) for result in inspected.values()) / len(inspected) >= 0.6:
                inspected_bytes = 0
                for index, item in enumerate(spine):
                    if item["mime"] not in _RASTER_TYPES:
                        inspected_bytes += archive.getinfo(item["href"]).file_size
                    if inspected_bytes > MAX_INSPECTION_BYTES:
                        pages = []
                        break
                    result = inspected.get(index)
                    if result is None:
                        try:
                            result = _inspect_page(archive, item, resources)
                        except (KeyError, ET.ParseError, EpubResourceError):
                            result = (False, None)
                    page = {"index": item["index"], "href": item["href"]}
                    if result[1]:
                        page["image"] = result[1]
                    elif item["mime"] not in _DOCUMENT_TYPES:
                        pages = []
                        break
                    pages.append(page)
                # Mostly-text books keep the EPUB renderer; a few text pages (credits, ads) are framed.
                if pages and sum("image" in page for page in pages) / len(pages) < _IMAGE_PAGE_SHARE:
                    pages = []
            result = {"revision": version, "opf": opf, "resources": resources,
                      "layout": layout, "direction": spine_node.get("page-progression-direction", "ltr"),
                      "manga": recommended, "pages": pages,
                      # Uncompressed size per reading-order document: a cheap length estimate for positions.
                      "sections": [{"index": item["index"], "size": archive.getinfo(item["href"]).file_size}
                                   for item in spine if item["href"] in names]}
    except (KeyError, ET.ParseError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as ex:
        raise EpubResourceError(f"Cannot read EPUB: {ex}") from ex
    with _cache_lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def read_resource(path, version, name):
    metadata = describe(path)
    if version != metadata["revision"] or name not in metadata["resources"]:
        raise KeyError(name)
    try:
        with zipfile.ZipFile(path) as archive:
            return _read(archive, name, MAX_RESOURCE_BYTES), metadata["resources"][name]
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as ex:
        raise EpubResourceError(f"Cannot read EPUB resource: {ex}") from ex
