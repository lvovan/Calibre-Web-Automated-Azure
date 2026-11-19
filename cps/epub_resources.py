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


def _inspect_page(archive, item, resources):
    """Only flatten unstyled, single-raster wrappers; preserve everything else as XHTML."""
    if item["mime"] in _RASTER_TYPES:
        return True, item["href"]
    if item["mime"] not in _DOCUMENT_TYPES:
        return False, None
    doc = _xml(archive, item["href"])
    body = next((node for node in doc.iter() if _tag(node) == "body"), doc)
    images = [node for node in body.iter() if _tag(node) in ("img", "image")]
    text = "".join(body.itertext()).strip()
    image_focused = bool(images) and len(text) <= 80
    simple = (not text and len(images) == 1 and _tag(doc) == "html"
              and all(_tag(node) in ("body", "div", "p", "img", "span") for node in body.iter())
              and not any(_tag(node) in ("style", "link", "script") or
                          any(key in node.attrib for key in ("style", "hidden"))
                          for node in doc.iter()))
    if simple:
        image = _resolve(item["href"], images[0].get("src", ""))
        if resources.get(image) in _RASTER_TYPES:
            return image_focused, image
    return image_focused, None


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
            if display_options in archive.namelist():
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
            if (recommended and all(result[1] for result in inspected.values()) and
                    all(node.get("linear", "yes") != "no" for node in spine_node)):
                inspected_bytes = 0
                for index, item in enumerate(spine):
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
                    if not result[1] or item["properties"]:
                        pages = []
                        break
                    pages.append({"index": item["index"], "href": item["href"], "image": result[1]})
            result = {"revision": version, "opf": opf, "resources": resources,
                      "layout": layout, "direction": spine_node.get("page-progression-direction", "ltr"),
                      "manga": recommended, "pages": pages}
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
