"""Page tools: merge, split, reorder, rotate, crop, resize, impose."""
from typing import Annotated, Literal

import pymupdf
from fastapi import Form
from PIL import Image

from ..core import PDF, Pages, ToolError, chunks, parse_pages, place, render, tool

Tolerance = Annotated[float, Form(ge=0, le=100, description="Percent of non-white pixels a page may have and still count as blank")]
Grid = Annotated[int, Form(ge=1, le=10)]


@tool("Merge", "Pages")
def merge(docs: list[PDF], mode: Literal["append", "interleave"] = "append", reverse_last: bool = False):
    """Combine PDFs into one, in upload order. Interleave alternates their pages; with "reverse last" it
    collates a double-sided stack scanned as two files (fronts, then backs in reverse)."""
    out = pymupdf.open()
    if mode == "append":
        for d in docs:
            out.insert_pdf(d)
        return out
    orders = [list(range(len(d))) for d in docs]
    if reverse_last:
        orders[-1].reverse()
    for i in range(max(map(len, docs))):
        for d, order in zip(docs, orders):
            if i < len(order):
                out.insert_pdf(d, from_page=order[i], to_page=order[i], final=False)
    return out


@tool("Split", "Pages")
def split(doc: PDF, after: Pages = "all"):
    """Cut the PDF after each of the given pages. "all" gives one file per page; "3,7" gives 1-3, 4-7 and 8-end."""
    n = len(doc)
    return chunks(doc, [0, *sorted({i + 1 for i in parse_pages(after, n)} - {n}), n])


@tool("Split by size or count", "Pages")
def split_by(doc: PDF, mode: Literal["pages-per-file", "number-of-files", "max-megabytes"] = "pages-per-file",
             value: Annotated[float, Form(gt=0)] = 10):
    """Cut into files of N pages each, into N equal files, or into files no larger than N megabytes."""
    n = len(doc)
    if mode == "pages-per-file":
        return chunks(doc, [*range(0, n, max(1, int(value))), n])
    if mode == "number-of-files":
        k = min(max(1, int(value)), n)
        return chunks(doc, [i * n // k for i in range(k + 1)])
    # shortcut: re-serialises the growing part after every page (quadratic); estimate sizes instead if huge PDFs get slow
    bounds, part = [0], pymupdf.open()
    for i in range(n):
        part.insert_pdf(doc, from_page=i, to_page=i)
        if len(part) > 1 and len(part.tobytes(garbage=3, deflate=True)) > value * 2**20:
            bounds.append(i)
            part = pymupdf.open()
            part.insert_pdf(doc, from_page=i, to_page=i)
    return chunks(doc, [*bounds, n])


@tool("Split by chapters", "Pages")
def split_chapters(doc: PDF, level: Annotated[int, Form(ge=1, le=9, description="Deepest bookmark level to cut at")] = 1):
    """Cut at the bookmarks; each file is named after its chapter."""
    starts: dict[int, str] = {}
    for lvl, title, page, *_ in doc.get_toc():
        if lvl <= level and 1 <= page <= len(doc):
            starts.setdefault(page - 1, title)
    if not starts:
        raise ToolError("This PDF has no bookmarks at that level")
    starts.setdefault(0, "start")
    first = sorted(starts)
    parts = chunks(doc, [*first, len(doc)])
    return [(f"_{k + 1:02d}_{starts[i][:60]}.pdf", part) for k, (i, (_, part)) in enumerate(zip(first, parts))]


@tool("Split pages into sections", "Pages")
def split_sections(doc: PDF, columns: Grid = 2, rows: Grid = 1, pages: Pages = "all"):
    """Cut each page into a grid of equal parts, each becoming its own page (e.g. a scanned two-page spread)."""
    chosen = set(parse_pages(pages, len(doc)))
    out = pymupdf.open()
    for i in range(len(doc)):
        if i not in chosen:
            out.insert_pdf(doc, from_page=i, to_page=i)
            continue
        w, h = doc[i].rect.width / columns, doc[i].rect.height / rows
        for r in range(rows):
            for c in range(columns):
                page = out.new_page(width=w, height=h)
                place(page, page.rect, doc, i, clip=pymupdf.Rect(c * w, r * h, (c + 1) * w, (r + 1) * h))
    return out


def is_blank(page: pymupdf.Page, tolerance: float) -> bool:
    """No text, and at most `tolerance` percent of pixels darker than near-white."""
    if page.get_text().strip():
        return False
    pix = render(page, 50, colorspace=pymupdf.csGRAY)
    return len(pix.samples.translate(None, bytes(range(230, 256)))) * 100 <= tolerance * len(pix.samples)


@tool("Split at blank pages", "Pages")
def split_blank(doc: PDF, tolerance: Tolerance = 0.1):
    """Cut a scanned stack into documents wherever a blank separator sheet was inserted. The blank pages are dropped."""
    out, start = [], 0
    for i in range(len(doc) + 1):
        if i == len(doc) or is_blank(doc[i], tolerance):
            if i > start:
                out += chunks(doc, [start, i])
            start = i + 1
    if not out:
        raise ToolError("Every page looks blank")
    return out


@tool("Remove blank pages", "Pages")
def remove_blank(doc: PDF, tolerance: Tolerance = 0.1):
    """Drop pages with no text and (almost) no ink - the empty backs in a double-sided scan."""
    blank = [i for i in range(len(doc)) if is_blank(doc[i], tolerance)]
    if len(blank) == len(doc):
        raise ToolError("Every page looks blank")
    if blank:
        doc.delete_pages(blank)
    return doc


@tool("Extract or reorder pages", "Pages")
def organize(doc: PDF, pages: Pages):
    """Keep only the listed pages, in the order written: extract (5-9), reorder (3,1,2), duplicate (1,1,2) or reverse."""
    doc.select(parse_pages(pages, len(doc)))
    return doc


@tool("Remove pages", "Pages")
def remove_pages(doc: PDF, pages: Pages):
    """Delete the listed pages."""
    doc.delete_pages(sorted(set(parse_pages(pages, len(doc)))))
    return doc


@tool("Rotate", "Pages")
def rotate(doc: PDF, angle: Literal["90", "180", "270"] = "90", pages: Pages = "all"):
    """Turn pages clockwise."""
    for i in set(parse_pages(pages, len(doc))):
        doc[i].set_rotation(doc[i].rotation + int(angle))
    return doc


@tool("Insert blank pages", "Pages")
def insert_blank(doc: PDF, after: Pages = "last", count: Annotated[int, Form(ge=1, le=100)] = 1):
    """Add empty pages after each of the given pages, matching that page's size."""
    for i in sorted(set(parse_pages(after, len(doc))), reverse=True):
        w, h = doc[i].rect.width, doc[i].rect.height
        for _ in range(count):
            doc.new_page(pno=i + 1, width=w, height=h)
    return doc


def _crop(page: pymupdf.Page, box: pymupdf.Rect) -> None:
    """Make `box` (in displayed-page coordinates) the visible area."""
    box &= page.rect
    if box.is_empty:
        raise ToolError("Nothing would be left of the page")
    box = (box * page.derotation_matrix).normalize()
    pos = page.cropbox_position
    page.set_cropbox(box + (pos.x, pos.y, pos.x, pos.y))


@tool("Crop", "Pages")
def crop(doc: PDF, left: float = 0, top: float = 0, right: float = 0, bottom: float = 0, pages: Pages = "all"):
    """Trim each edge by a distance in points (72 pt = 1 inch, 28.35 pt = 1 cm)."""
    for i in set(parse_pages(pages, len(doc))):
        r = doc[i].rect
        _crop(doc[i], pymupdf.Rect(left, top, r.width - right, r.height - bottom))
    return doc


@tool("Auto-crop margins", "Pages")
def auto_crop(doc: PDF, margin: float = 10, pages: Pages = "all"):
    """Trim empty white margins, keeping `margin` points around the content."""
    for i in set(parse_pages(pages, len(doc))):
        pix = render(doc[i], 72, colorspace=pymupdf.csGRAY)  # at 72 DPI one pixel is one point
        ink = Image.frombytes("L", (pix.width, pix.height), pix.samples).point(lambda v: 255 * (v < 245)).getbbox()
        if ink:
            _crop(doc[i], pymupdf.Rect(ink) + (-margin, -margin, margin, margin))
    return doc


@tool("Resize pages", "Pages")
def resize(doc: PDF, size: Literal["keep", "A3", "A4", "A5", "Letter", "Legal"] = "A4",
           content_scale: Annotated[float, Form(gt=0, le=10, description="1 = fit the page, 0.9 = add a margin, 1.1 = zoom in")] = 1):
    """Put every page on a new paper size (orientation is kept) and/or scale the content around the page centre.
    Bookmarks survive; links and form fields do not."""
    out = pymupdf.open()
    for i in range(len(doc)):
        w, h = doc[i].rect.width, doc[i].rect.height
        if size != "keep":
            short, long = sorted(pymupdf.paper_size(size.lower()))
            w, h = (long, short) if w > h else (short, long)
        dx, dy = w * (1 - content_scale) / 2, h * (1 - content_scale) / 2
        place(out.new_page(width=w, height=h), pymupdf.Rect(dx, dy, w - dx, h - dy), doc, i)
    out.set_toc(doc.get_toc())
    return out


@tool("Multi-page layout (N-up)", "Pages")
def n_up(doc: PDF, per_sheet: Literal["2", "3", "4", "6", "8", "9", "16"] = "4", border: bool = False):
    """Put several pages on each sheet. Sheets take the first page's size, turned landscape for 2, 3, 6 and 8 up."""
    cols, rows = {"2": (2, 1), "3": (3, 1), "4": (2, 2), "6": (3, 2), "8": (4, 2), "9": (3, 3), "16": (4, 4)}[per_sheet]
    short, long = sorted((doc[0].rect.width, doc[0].rect.height))
    w, h = (long, short) if cols > rows else (short, long)
    out = pymupdf.open()
    for i in range(len(doc)):
        k = i % (cols * rows)
        if k == 0:
            sheet = out.new_page(width=w, height=h)
        x, y = k % cols * w / cols, k // cols * h / rows
        cell = pymupdf.Rect(x, y, x + w / cols, y + h / rows)
        place(sheet, cell, doc, i)
        if border:
            sheet.draw_rect(cell, width=0.5)
    return out


@tool("Booklet", "Pages")
def booklet(doc: PDF):
    """Arrange pages for a folded booklet: print double-sided (flip on the short edge) and fold the stack in half."""
    n = -(-len(doc) // 4) * 4
    order = [p for s in range(n // 4) for p in (n - 1 - 2 * s, 2 * s, 2 * s + 1, n - 2 - 2 * s)]
    w, h = doc[0].rect.width, doc[0].rect.height
    out = pymupdf.open()
    for k, p in enumerate(order):
        if k % 2 == 0:
            sheet = out.new_page(width=2 * w, height=h)
        if p < len(doc):
            place(sheet, pymupdf.Rect(k % 2 * w, 0, k % 2 * w + w, h), doc, p)
    return out


@tool("Single long page", "Pages")
def single_page(doc: PDF):
    """Stack all pages into one tall page."""
    width, height = max(p.rect.width for p in doc), sum(p.rect.height for p in doc)
    if height > 14400:
        raise ToolError("Too long for one page: PDF pages are limited to 200 inches")
    out = pymupdf.open()
    page, y = out.new_page(width=width, height=height), 0
    for i in range(len(doc)):
        r = doc[i].rect
        place(page, pymupdf.Rect(0, y, r.width, y + r.height), doc, i)
        y += r.height
    return out


@tool("Overlay", "Pages")
def overlay(doc: PDF, overlay_pdf: PDF, position: Literal["foreground", "background"] = "foreground", repeat: bool = True):
    """Draw the pages of a second PDF over (or under) this one, e.g. a letterhead. With repeat off, overlay
    page N only goes on page N."""
    for i in range(len(doc)):
        if repeat or i < len(overlay_pdf):
            place(doc[i], doc[i].rect, overlay_pdf, i % len(overlay_pdf), overlay=position == "foreground")
    return doc
