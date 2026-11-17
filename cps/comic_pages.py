# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Page-by-page access to comic archives (CBZ/CBT/CBR) for the web comic reader.

The reader asks for the list of pages, then fetches each page image on demand, so browsers never
download or unpack the whole archive (which exhausts memory on phones for large books).
"""

import os
import re
import struct
import tarfile
import threading
import zipfile
from collections import OrderedDict

_DIGITS = re.compile(r'(\d+)')


def _natural_key(name):
    return [(0, int(part), '') if part.isdigit() else (1, 0, part.lower())
            for part in _DIGITS.split(name.replace('\\', '/'))]


def _sort_names(names):
    return sorted(names, key=_natural_key)


try:
    import rarfile
except (ImportError, SyntaxError):  # pragma: no cover
    rarfile = None

IMAGE_MIME_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.jfif': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.avif': 'image/avif',
}
MAX_PAGE_BYTES = 64 * 1024 * 1024
_HEADER_READS = (64 * 1024, 1024 * 1024)
_CACHE_SIZE = 32


class ComicArchiveError(Exception):
    pass


def image_size(data):
    """Returns (width, height) from the start of a JPEG/PNG/GIF/WebP/BMP file, or None."""
    if len(data) >= 24 and data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR':
        return struct.unpack('>II', data[16:24])
    if len(data) >= 10 and data[:6] in (b'GIF87a', b'GIF89a'):
        return struct.unpack('<HH', data[6:10])
    if len(data) >= 26 and data[:2] == b'BM':
        width, height = struct.unpack('<ii', data[18:26])
        return abs(width), abs(height)
    if len(data) >= 30 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X':
            return (int.from_bytes(data[24:27], 'little') + 1, int.from_bytes(data[27:30], 'little') + 1)
        if chunk == b'VP8L' and data[20] == 0x2f:
            bits = int.from_bytes(data[21:25], 'little')
            return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
        if chunk == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a':
            width, height = struct.unpack('<HH', data[26:30])
            return width & 0x3fff, height & 0x3fff
        return None
    if data[:2] == b'\xff\xd8':
        return _jpeg_size(data)
    return None


def _jpeg_size(data):
    i = 2
    length = len(data)
    while i + 4 <= length:
        if data[i] != 0xff:
            return None
        marker = data[i + 1]
        if marker == 0xff:
            i += 1
            continue
        if marker in (0x01, 0xd8) or 0xd0 <= marker <= 0xd7:
            i += 2
            continue
        segment = struct.unpack('>H', data[i + 2:i + 4])[0]
        # Start-of-frame markers, excluding DHT (c4), JPG (c8) and DAC (cc).
        if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
            if i + 9 > length:
                return None
            height, width = struct.unpack('>HH', data[i + 5:i + 9])
            return width, height
        i += 2 + segment
    return None


def _is_page(name):
    parts = name.replace('\\', '/').split('/')
    if any(p.startswith('.') or p == '__MACOSX' for p in parts):
        return False
    return os.path.splitext(name)[1].lower() in IMAGE_MIME_TYPES


class _ZipReader:
    def __init__(self, path):
        self._zf = zipfile.ZipFile(path)

    def entries(self):
        return [(i.filename, i.file_size) for i in self._zf.infolist() if not i.is_dir()]

    def read(self, name, limit=-1):
        with self._zf.open(name) as f:
            return f.read(limit)

    def close(self):
        self._zf.close()


class _TarReader:
    def __init__(self, path):
        self._tf = tarfile.open(path)

    def entries(self):
        return [(m.name, m.size) for m in self._tf.getmembers() if m.isfile()]

    def read(self, name, limit=-1):
        f = self._tf.extractfile(name)
        return f.read(limit) if f else b''

    def close(self):
        self._tf.close()


class _RarReader:
    # Page sizes are not probed for RAR: every read spawns unrar. The reader learns them.
    probe_sizes = False

    def __init__(self, path, rar_tool):
        if rarfile is None:
            raise ComicArchiveError('rarfile is not installed')
        if rar_tool:
            rarfile.UNRAR_TOOL = rar_tool
        self._rf = rarfile.RarFile(path)

    def entries(self):
        return [(i.filename, i.file_size) for i in self._rf.infolist() if not i.is_dir()]

    def read(self, name, limit=-1):
        with self._rf.open(name) as f:
            return f.read(limit)

    def close(self):
        self._rf.close()


def _open(path, rar_tool=None):
    with open(path, 'rb') as f:
        magic = f.read(8)
    try:
        if magic[:4] == b'PK\x03\x04' or magic[:4] == b'PK\x05\x06':
            return _ZipReader(path)
        if magic[:4] == b'Rar!':
            return _RarReader(path, rar_tool)
        if tarfile.is_tarfile(path):
            return _TarReader(path)
    except ComicArchiveError:
        raise
    except Exception as ex:
        raise ComicArchiveError(f'Cannot open comic archive: {ex}') from ex
    raise ComicArchiveError('Unsupported comic archive format')


def _probe_size(reader, name, size):
    for limit in _HEADER_READS:
        data = reader.read(name, min(limit, size))
        dims = image_size(data)
        if dims or len(data) >= size:
            return dims
    return None


_cache = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(path):
    st = os.stat(path)
    return path, st.st_mtime_ns, st.st_size


def list_pages(path, rar_tool=None):
    """Returns the ordered pages of a comic archive: [{'name', 'mime', 'w', 'h'}].

    Width and height are 0 when unknown. Results are cached per file version.
    """
    key = _cache_key(path)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    reader = _open(path, rar_tool)
    try:
        entries = {name: size for name, size in reader.entries()
                   if _is_page(name) and 0 < size <= MAX_PAGE_BYTES}
        probe = getattr(reader, 'probe_sizes', True)
        pages = []
        for name in _sort_names(list(entries)):
            dims = None
            if probe:
                try:
                    dims = _probe_size(reader, name, entries[name])
                except Exception:
                    dims = None
            pages.append({
                'name': name,
                'mime': IMAGE_MIME_TYPES[os.path.splitext(name)[1].lower()],
                'w': dims[0] if dims else 0,
                'h': dims[1] if dims else 0,
            })
    except ComicArchiveError:
        raise
    except Exception as ex:
        raise ComicArchiveError(f'Cannot read comic archive: {ex}') from ex
    finally:
        reader.close()

    with _cache_lock:
        _cache[key] = pages
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return pages


def read_page(path, index, rar_tool=None):
    """Returns (bytes, mime type) of the page at a zero-based index. Raises IndexError."""
    pages = list_pages(path, rar_tool)
    if index < 0 or index >= len(pages):
        raise IndexError(index)
    page = pages[index]
    reader = _open(path, rar_tool)
    try:
        return reader.read(page['name']), page['mime']
    except Exception as ex:
        raise ComicArchiveError(f'Cannot read page {index}: {ex}') from ex
    finally:
        reader.close()
