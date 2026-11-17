# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Tests for page-by-page comic archive access (cps/comic_pages.py)."""

import importlib.util
import io
import os
import struct
import tarfile
import zipfile
import zlib

import pytest

_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'cps', 'comic_pages.py')
_spec = importlib.util.spec_from_file_location('comic_pages_under_test', _PATH)
comic_pages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(comic_pages)


def png(width, height):
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    chunk = b'IHDR' + ihdr
    return (b'\x89PNG\r\n\x1a\n' + struct.pack('>I', len(ihdr)) + chunk +
            struct.pack('>I', zlib.crc32(chunk)) + b'\x00' * 32)


def jpeg(width, height, exif_bytes=0):
    app1 = b''
    while exif_bytes > 0:
        chunk = min(exif_bytes, 60000)
        app1 += b'\xff\xe1' + struct.pack('>H', chunk + 2) + b'E' * chunk
        exif_bytes -= chunk
    sof = b'\xff\xc2' + struct.pack('>HBHHB', 11, 8, height, width, 1) + b'\x01\x11\x00'
    return b'\xff\xd8' + app1 + sof + b'\xff\xd9'


def make_cbz(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return str(path)


@pytest.mark.parametrize('data,expected', [
    (png(640, 480), (640, 480)),
    (jpeg(800, 1200), (800, 1200)),
    (jpeg(800, 1200, exif_bytes=70000), (800, 1200)),
    (b'GIF89a' + struct.pack('<HH', 31, 17) + b'\x00' * 8, (31, 17)),
    (b'BM' + b'\x00' * 16 + struct.pack('<ii', 100, -50) + b'\x00' * 8, (100, 50)),
    (b'RIFF\x00\x00\x00\x00WEBPVP8X' + b'\x00' * 8 + (99).to_bytes(3, 'little') +
     (149).to_bytes(3, 'little'), (100, 150)),
    (b'RIFF\x00\x00\x00\x00WEBPVP8L\x00\x00\x00\x00\x2f' +
     ((199) | (299 << 14)).to_bytes(4, 'little') + b'\x00' * 8, (200, 300)),
    (b'not an image', None),
])
def test_image_size(data, expected):
    assert comic_pages.image_size(data) == expected


def test_lists_images_in_natural_order_and_skips_junk(tmp_path):
    path = make_cbz(tmp_path / 'book.cbz', {
        'p10.jpg': jpeg(10, 20),
        'P2.png': png(30, 40),
        'p1.jpg': jpeg(50, 60),
        'ComicInfo.xml': b'<x/>',
        '__MACOSX/._p1.jpg': b'junk',
        '.hidden.jpg': jpeg(1, 1),
        'sub/': b'',
        'empty.jpg': b'',
    })
    pages = comic_pages.list_pages(path)
    assert [p['name'] for p in pages] == ['p1.jpg', 'P2.png', 'p10.jpg']
    assert pages[0] == {'name': 'p1.jpg', 'mime': 'image/jpeg', 'w': 50, 'h': 60}
    assert pages[1]['mime'] == 'image/png' and (pages[1]['w'], pages[1]['h']) == (30, 40)


def test_jpeg_size_found_beyond_first_header_read(tmp_path):
    path = make_cbz(tmp_path / 'big.cbz', {'a.jpg': jpeg(1000, 1500, exif_bytes=65000)})
    assert comic_pages.list_pages(path)[0]['w'] == 1000


def test_read_page_returns_bytes_and_bounds_checks(tmp_path):
    first = jpeg(5, 6)
    path = make_cbz(tmp_path / 'book.cbz', {'b.png': png(1, 2), 'a.jpg': first})
    data, mime = comic_pages.read_page(path, 0)
    assert (data, mime) == (first, 'image/jpeg')
    with pytest.raises(IndexError):
        comic_pages.read_page(path, 2)
    with pytest.raises(IndexError):
        comic_pages.read_page(path, -1)


def test_tar_archives_are_supported(tmp_path):
    path = tmp_path / 'book.cbt'
    with tarfile.open(path, 'w') as tf:
        for name, data in (('02.png', png(3, 4)), ('01.jpg', jpeg(7, 8))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    pages = comic_pages.list_pages(str(path))
    assert [(p['name'], p['w'], p['h']) for p in pages] == [('01.jpg', 7, 8), ('02.png', 3, 4)]
    assert comic_pages.read_page(str(path), 1)[1] == 'image/png'


def test_zip_detected_by_content_not_extension(tmp_path):
    path = make_cbz(tmp_path / 'misnamed.cbr', {'a.jpg': jpeg(1, 1)})
    assert len(comic_pages.list_pages(path)) == 1


def test_unsupported_archive_raises(tmp_path):
    path = tmp_path / 'bad.cbz'
    path.write_bytes(b'garbage' * 100)
    with pytest.raises(comic_pages.ComicArchiveError):
        comic_pages.list_pages(str(path))


def test_listing_is_cached_until_file_changes(tmp_path, monkeypatch):
    path = make_cbz(tmp_path / 'book.cbz', {'a.jpg': jpeg(1, 1)})
    opened = []
    real_open = comic_pages._open
    monkeypatch.setattr(comic_pages, '_open', lambda *a: opened.append(a) or real_open(*a))
    comic_pages.list_pages(path)
    comic_pages.list_pages(path)
    assert len(opened) == 1

    make_cbz(tmp_path / 'book.cbz', {'a.jpg': jpeg(1, 1), 'b.jpg': jpeg(1, 1)})
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert len(comic_pages.list_pages(path)) == 2
    assert len(opened) == 2
