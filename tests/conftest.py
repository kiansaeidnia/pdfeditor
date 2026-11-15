"""Shared fixtures: sample PDFs and a helper to call a tool through the HTTP API."""
import io
import os
import shutil
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.main import app


def make_pdf(pages=3, text="Page {n}", rotation=0, width=595, height=842) -> bytes:
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 100), text.format(n=n), fontsize=20)
        page.set_rotation(rotation)
    return doc.tobytes()


def rich_pdf() -> bytes:
    """Four pages with a picture, a table, bookmarks, an attachment, a form field, a comment and a script."""
    doc = pymupdf.open()
    for n in range(1, 5):
        page = doc.new_page()
        page.insert_text((72, 80), f"Chapter {n}", fontsize=24)
        page.insert_text((72, 120), f"Body text of page {n}. Secret code ABC-{n}23.", fontsize=11)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200))
    pix.set_rect(pix.irect, (200, 30, 30))
    doc[0].insert_image(pymupdf.Rect(72, 200, 192, 280), pixmap=pix)
    for r in range(4):  # 3x3 table on page 2
        doc[1].draw_line((72, 200 + 30 * r), (372, 200 + 30 * r))
        doc[1].draw_line((72 + 100 * r, 200), (72 + 100 * r, 290))
    for r in range(3):
        for c in range(3):
            doc[1].insert_text((80 + 100 * c, 220 + 30 * r), f"r{r}c{c}", fontsize=10)
    doc.set_toc([[1, "One", 1], [1, "Two", 3]])
    doc.embfile_add("note.txt", b"hello attachment")
    field = pymupdf.Widget()
    field.field_name, field.field_type, field.rect = "name", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(72, 300, 272, 320)
    doc[0].add_widget(field)
    doc[2].add_highlight_annot(doc[2].search_for("Body")[0])
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.beep)>>")
    doc.set_metadata({"title": "Sample", "author": "Tester"})
    return doc.tobytes()


def pdf(response) -> pymupdf.Document:
    return pymupdf.open("pdf", response.content)


def texts(doc) -> list[str]:
    return [" ".join(p.get_text().split()) for p in doc]


def unzip(response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        return {n: z.read(n) for n in z.namelist()}


def needs(*programs):
    """Skip when an external program is missing - unless REQUIRE_ALL is set (CI), where that is a failure."""
    missing = [p for p in programs if not shutil.which(p)]
    if missing and os.environ.get("REQUIRE_ALL"):
        pytest.fail(f"missing in this image: {missing}")
    return pytest.mark.skipif(bool(missing), reason=f"needs {missing}")


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture
def run(client):
    """run(tool, *uploads, expect=200, extra={field: (name, bytes)}, **settings) -> response."""
    def _run(tool, *uploads, expect=200, extra=None, **settings):
        parts = [("files", u if isinstance(u, tuple) else (f"doc{i + 1}.pdf", u)) for i, u in enumerate(uploads)]
        parts += [(field, f) for field, fs in (extra or {}).items() for f in (fs if isinstance(fs, list) else [fs])]
        data = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in settings.items()}
        r = client.post(f"/api/tools/{tool}", files=parts or None, data=data)
        assert r.status_code == expect, r.text[:500]
        return r
    return _run
