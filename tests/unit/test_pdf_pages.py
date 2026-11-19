# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Tests for page-by-page PDF access (cps/pdf_pages.py)."""

import importlib.util
import io
import os

import pytest

pytest.importorskip('pypdfium2')
Image = pytest.importorskip('PIL.Image')

_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'cps', 'pdf_pages.py')
_spec = importlib.util.spec_from_file_location('pdf_pages_under_test', _PATH)
pdf_pages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdf_pages)


def image_pdf(path, mode='RGB', pages=3, size=(300, 450)):
    images = [Image.new(mode, size, 128) for _ in range(pages)]
    images[0].save(path, 'PDF', save_all=True, append_images=images[1:], resolution=72)
    return path


def text_pdf(path, text='Chapter one. ' * 10, pages=3):
    objects = ['<< /Type /Catalog /Pages 2 0 R >>',
               '<< /Type /Pages /Kids [{}] /Count {} >>'.format(
                   ' '.join(f'{4 + 2 * i} 0 R' for i in range(pages)), pages),
               '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    stream = f'BT /F1 12 Tf 20 400 Td ({text}) Tj ET'
    for i in range(pages):
        objects.append('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 600] '
                       f'/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>')
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
    out = io.BytesIO()
    out.write(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f'{number} 0 obj\n{body}\nendobj\n'.encode('latin-1'))
    xref = out.tell()
    out.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        out.write(f'{offset:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    with open(path, 'wb') as f:
        f.write(out.getvalue())
    return path


def decoded_size(data):
    assert data[:3] == b'\xff\xd8\xff'
    return Image.open(io.BytesIO(data)).size


def test_lists_every_page_with_its_size(tmp_path):
    pages = pdf_pages.list_pages(image_pdf(str(tmp_path / 'scan.pdf')))
    assert [p['name'] for p in pages] == ['page-0001.jpg', 'page-0002.jpg', 'page-0003.jpg']
    assert pages[0]['mime'] == 'image/jpeg' and (pages[0]['w'], pages[0]['h']) == (300, 450)


def test_scanned_jpeg_page_is_sent_without_rendering(tmp_path):
    data, mime = pdf_pages.read_page(image_pdf(str(tmp_path / 'scan.pdf')), 1, size=800)
    assert mime == 'image/jpeg'
    assert decoded_size(data) == (300, 450)


def test_cmyk_jpeg_page_is_rendered(tmp_path):
    data, _ = pdf_pages.read_page(image_pdf(str(tmp_path / 'cmyk.pdf'), mode='CMYK'), 0, size=900)
    assert decoded_size(data) == (600, 900)


def test_text_page_is_rendered_at_the_requested_size(tmp_path):
    data, _ = pdf_pages.read_page(text_pdf(str(tmp_path / 'text.pdf')), 0, size=1200)
    assert decoded_size(data) == (800, 1200)


def test_render_size_is_clamped():
    assert pdf_pages.clamp_size('99999') == pdf_pages.MAX_SIZE
    assert pdf_pages.clamp_size('1') == pdf_pages.MIN_SIZE
    assert pdf_pages.clamp_size(None) == pdf_pages.DEFAULT_SIZE


def test_missing_page_raises_index_error(tmp_path):
    with pytest.raises(IndexError):
        pdf_pages.read_page(image_pdf(str(tmp_path / 'scan.pdf')), 3)


def test_detects_image_only_and_text_pdfs(tmp_path):
    assert pdf_pages.is_image_only(image_pdf(str(tmp_path / 'scan.pdf'), pages=5))
    assert not pdf_pages.is_image_only(text_pdf(str(tmp_path / 'text.pdf')))


def test_damaged_pdf_raises_pdf_page_error(tmp_path):
    path = tmp_path / 'broken.pdf'
    path.write_bytes(b'not a pdf')
    with pytest.raises(pdf_pages.PdfPageError):
        pdf_pages.list_pages(str(path))
