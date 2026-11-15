"""Inspecting and optimising: info, scripts, compare, compress, repair, image extraction, previews."""
import base64
import difflib
import re
from typing import Annotated, Literal

import pymupdf
from fastapi import Form

from ..core import PDF, PERMS, Pages, ToolError, parse_pages, render, tool


def scripts(doc: PDF) -> list[str]:
    """Every JavaScript action in the file."""
    out = []
    for xref in range(1, doc.xref_length()):
        try:
            source = doc.xref_object(xref, compressed=True)
        except Exception:  # free or broken object
            continue
        for ref in re.findall(r"/JS\s+(\d+)\s+0\s+R", source):  # script stored in its own object
            out.append((doc.xref_stream(int(ref)) or doc.xref_object(int(ref)).encode()).decode(errors="replace"))
        if re.search(r"/JS\s*(\((?!\))|<(?!>))", source):  # non-empty script written inline: show its action
            out.append(source)
    return [s for s in out if s.strip()]


def strip_scripts(doc: PDF) -> None:
    """Remove script actions written inline (scrub() only blanks the ones stored as separate objects)."""
    for xref in range(1, doc.xref_length()):
        try:
            for key in ("OpenAction", "A"):
                if doc.xref_get_key(xref, f"{key}/S")[1] == "/JavaScript":
                    doc.xref_set_key(xref, key, "null")
            for key in ("AA", "Names/JavaScript"):  # event handlers and named scripts are always scripts
                if doc.xref_get_key(xref, key)[0] != "null":
                    doc.xref_set_key(xref, key, "null")
        except Exception:  # not a dictionary
            continue


@tool("Info", "Inspect")
def info(doc: PDF):
    """Everything about the document as JSON: properties, permissions, bookmarks, form fields, attachments,
    fonts, scripts and per-page details."""
    pages = [{"page": i + 1, "width_pt": round(p.rect.width, 1), "height_pt": round(p.rect.height, 1),
              "rotation": p.rotation, "images": len(p.get_images()), "annotations": len(list(p.annots())),
              "characters": len(p.get_text())} for i, p in enumerate(doc)]
    return {
        "pages": len(doc),
        "metadata": doc.metadata,
        "permissions": {name: bool(doc.permissions & bit) for name, bit in PERMS.items()},
        "has_text": any(p["characters"] for p in pages),
        "signed": doc.get_sigflags() > 0,
        "javascript_actions": len(scripts(doc)),
        "attachments": doc.embfile_names(),
        "fonts": sorted({font[3] for i in range(len(doc)) for font in doc.get_page_fonts(i)}),
        "bookmarks": [item[:3] for item in doc.get_toc()],
        "form_fields": {w.field_name: w.field_value for p in doc for w in p.widgets()},
        "page_details": pages,
    }


@tool("Show JavaScript", "Inspect")
def javascript(doc: PDF):
    """List the scripts embedded in the PDF. Sanitize removes them."""
    return {"scripts": scripts(doc)}


@tool("Compare", "Inspect")
def compare(docs: list[PDF]):
    """Side-by-side report of the text differences between two PDFs."""
    if len(docs) != 2:
        raise ToolError("Upload exactly two PDFs")
    a, b = ([line for page in d for line in page.get_text().splitlines()] for d in docs)
    # shortcut: difflib is quadratic on long, very different texts; use a faster diff if whole books get compared
    return ("_compare.html", difflib.HtmlDiff(wrapcolumn=70).make_file(a, b, "First PDF", "Second PDF", context=True, numlines=2))


@tool("Compress", "Inspect")
def compress(doc: PDF, level: Literal["low", "medium", "high"] = "medium", grayscale: bool = False):
    """Shrink the file: downsample and recompress pictures, embed only the used characters of each font,
    and drop unused data. Higher levels trade picture quality for size."""
    dpi, quality = {"low": (200, 85), "medium": (150, 70), "high": (96, 45)}[level]
    doc.rewrite_images(dpi_threshold=int(dpi * 1.2), dpi_target=dpi, quality=quality, set_to_gray=grayscale)
    doc.subset_fonts()
    return ("_compressed.pdf", doc.tobytes(garbage=4, deflate=True, use_objstms=1))


@tool("Repair", "Inspect")
def repair(doc: PDF):
    """Rebuild a damaged file. Whatever could be recovered on opening is saved as a clean PDF."""
    return ("_repaired.pdf", doc.tobytes(garbage=4, clean=True, deflate=True))


@tool("Extract images", "Inspect")
def extract_images(doc: PDF, pages: Pages = "all"):
    """Save the pictures embedded in the PDF, in their original format."""
    xrefs = dict.fromkeys(image[0] for i in parse_pages(pages, len(doc)) for image in doc.get_page_images(i))
    found = [(x, doc.extract_image(x)) for x in xrefs]
    if not any(image for _, image in found):
        raise ToolError("No images found")
    return [(f"_image{x}.{image['ext']}", image["image"]) for x, image in found if image]


@tool("Auto-rename", "Inspect")
def auto_rename(doc: PDF):
    """Name the file after its title: the largest text on the first page."""
    spans = [s for b in doc[0].get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"] if s["text"].strip()]
    if not spans:
        raise ToolError("There is no text on the first page - run OCR first")
    largest = max(s["size"] for s in spans)
    title = " ".join(s["text"].strip() for s in spans if s["size"] >= largest - 0.5)
    return (f"={title[:80]}.pdf", doc)


@tool("Preview", "Internal")
def preview(doc: PDF, dpi: Annotated[int, Form(ge=20, le=200)] = 40, pages: Pages = "all"):
    """Page pictures as data URLs, for the visual tools."""
    # shortcut: one response carries every requested page; the visual tools cap at 500 pages rather than paging
    chosen = parse_pages(pages, len(doc))
    if len(chosen) > 500:
        raise ToolError("Too many pages for the visual tools (limit 500)")
    return {"count": len(doc), "pages": [
        {"index": i, "width": doc[i].rect.width, "height": doc[i].rect.height,
         "image": "data:image/jpeg;base64," + base64.b64encode(render(doc[i], dpi).tobytes("jpg", jpg_quality=70)).decode()}
        for i in chosen]}
