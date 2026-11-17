"""Editing: stamps, page numbers, text replacement, forms, bookmarks, attachments, colour effects."""
import base64
import random
from typing import Annotated, Literal

import pymupdf
from fastapi import Form
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pydantic import BaseModel, Field, TypeAdapter

from ..core import (FONT, PDF, Anchor, Color, Dpi, Opacity, Pages, Text, ToolError, Upload, cover_scan, draw_image,
                    draw_text, find_text, font_for, image_pdf, layer_for, page_spans, parse_pages, place, put, rgb,
                    scan_look, tool, write_like)

Factor = Annotated[float, Form(ge=0, le=5, description="1 = unchanged")]


@tool("Add stamp", "Edit")
def stamp(doc: PDF, text: Annotated[str, Form(description="{n} = page number, {total} = page count")] = "",
          image: Annotated[Upload | None, "image/*"] = None, position: Anchor = "top-right",
          size: Annotated[float, Form(ge=4, le=500, description="Text size in points")] = 14,
          image_width: Annotated[float, Form(gt=0, le=100, description="Image width, in percent of the page width")] = 20,
          opacity: Opacity = 1, rotation: Annotated[float, Form(description="Degrees counter-clockwise")] = 0,
          color: Color = "#cc0000", margin: Annotated[float, Form(ge=0, description="Distance from the page edge in points")] = 36,
          pages: Pages = "all"):
    """Place text or an image - a logo, a scanned signature, a header or footer - at a fixed spot on each page."""
    for i in set(parse_pages(pages, len(doc))):
        put(doc[i], position, text=text.replace("{n}", str(i + 1)).replace("{total}", str(len(doc))),
            image=image and image.data, size=size, image_width=image_width, color=color, opacity=opacity,
            angle=rotation, margin=margin)
    return doc


@tool("Add page numbers", "Edit")
def page_numbers(doc: PDF, position: Anchor = "bottom-center",
                 text: Annotated[str, Form(description="{n} = number, {total} = last number. Bates numbering: ABC{n} with 6 digits")] = "{n}",
                 start: int = 1, digits: Annotated[int, Form(ge=0, le=12, description="Pad the number with zeros to this many digits")] = 0,
                 size: Annotated[float, Form(ge=4, le=200)] = 11, margin: Annotated[float, Form(ge=0)] = 24,
                 color: Color = "#000000", pages: Pages = "all"):
    """Number the selected pages in order."""
    chosen = sorted(set(parse_pages(pages, len(doc))))
    for k, i in enumerate(chosen):
        label = text.replace("{n}", str(start + k).zfill(digits)).replace("{total}", str(start + len(chosen) - 1).zfill(digits))
        put(doc[i], position, text=label, size=size, color=color, margin=margin)
    return doc


@tool("Find and replace text", "Edit")
def replace_text(doc: PDF, find: str, replace: str, pages: Pages = "all"):
    """Swap every occurrence of a phrase (ignoring case). The new text keeps the look of the text it replaces: its
    font (or the same font installed here, or a twin that measures the same), colour and baseline, at the original
    size or smaller if it is longer than the old text. It suits corrections rather than rewriting paragraphs."""
    total = 0
    for i in set(parse_pages(pages, len(doc))):
        page = doc[i]
        hits = []
        for q in page.search_for(find, quads=True):
            spans = [s for b in page.get_text("dict", clip=q.rect)["blocks"] for line in b.get("lines", []) for s in line["spans"]]
            # shortcut: assumes horizontal text; lines written at an angle get a level replacement
            span = dict(spans[0]) if spans else {"size": q.rect.height * 0.7, "color": 0, "font": "Helvetica", "flags": 0,
                                                 "origin": (0, q.rect.y1 - q.rect.height * 0.2)}
            span["origin"] = (q.rect.x0, span["origin"][1])  # the match may start part-way into the span
            if span.get("alpha") == 0:  # OCR text over a scan: what shows is the picture, so it gets painted over
                span.update(bbox=tuple(q.rect), size=q.rect.height, font="Helvetica", flags=0, scanned=True)
                span = scan_look(page, span)  # before the redaction below: it reads the picture only, which stays
            hits.append((q.rect, span))
            page.add_redact_annot(q, fill=False, cross_out=False)
        if not hits:
            continue
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        layer = layer_for(page)
        for box, span in hits:
            if span.get("scanned"):
                span = cover_scan(layer, page, span)  # the size and colours were read from the scan above
            font = font_for(span["font"], span["flags"])[0]
            size = span["size"] * min(1, box.width / max(font.text_length(replace, fontsize=span["size"]), 1e-6))
            write_like(layer, page, span, replace, size)
        place(page, page.rect, layer.parent, 0)
        total += len(hits)
    if not total:
        raise ToolError("No matching text found")
    return doc


@tool("Highlight text", "Edit")
def highlight(doc: PDF, find: Annotated[str, Form(description="Text to mark, one term per line", json_schema_extra={"format": "textarea"})],
              regex: bool = False, color: Color = "#ffe600", pages: Pages = "all"):
    """Mark every occurrence of the given text with a highlighter annotation."""
    hits = 0
    for i in set(parse_pages(pages, len(doc))):
        page = doc[i]  # keep the page alive: an annotation dies with its page object
        quads = find_text(page, find, regex)
        if quads:
            annot = page.add_highlight_annot(quads)
            annot.set_colors(stroke=rgb(color))
            annot.update()
        hits += len(quads)
    if not hits:
        raise ToolError("No matching text found")
    return doc


class Op(BaseModel):
    """One edit from the visual editor. Coordinates are points on the page as displayed."""
    type: Literal["text", "ink", "image", "redact", "whiteout", "highlight", "retext"]
    page: int = Field(ge=0)
    rect: tuple[float, float, float, float] | None = None  # image, redact, whiteout, highlight; retext: the text's box
    points: list[tuple[float, float]] = []                 # ink: the stroke; text: [top-left corner]
    text: str = Field("", max_length=2000)  # text; retext: the new text, written in the old text's font
    size: float = Field(14, gt=0, le=500)
    width: float = Field(1.5, gt=0, le=100)
    color: str = Field("#000000", pattern="^#[0-9a-fA-F]{6}$")
    image: str = ""  # data: URL


@tool("Apply edits", "Internal")
def edit(doc: PDF, ops: Text):
    """Apply a JSON list of edits (text, ink, image, redact, whiteout, highlight, retext). Used by the visual editor."""
    todo = TypeAdapter(list[Op]).validate_json(ops)
    if any(op.page >= len(doc) for op in todo):
        raise ToolError("An edit refers to a page this document doesn't have")
    boxes = {id(op): (pymupdf.Rect(op.rect) * doc[op.page].derotation_matrix).normalize() for op in todo if op.rect}
    # retext: find the text being changed while it is still there; it is removed below and written anew in its font
    retext, spans = [], {}
    for op in todo:
        if op.type == "retext" and op.rect:
            found = spans.setdefault(op.page, page_spans(doc[op.page]))
            best = max(found, key=lambda s: (pymupdf.Rect(s["bbox"]) & boxes[id(op)]).get_area(), default=None)
            if best is None or (pymupdf.Rect(best["bbox"]) & boxes[id(op)]).is_empty:
                raise ToolError(f"The text to change on page {op.page + 1} isn't there any more")
            retext.append((op, best))
    for op in todo:  # redact first, or it would also erase edits placed on top of the box
        if op.type == "redact" and op.rect:
            doc[op.page].add_redact_annot(boxes[id(op)], fill=rgb(op.color))
    for i in {op.page for op in todo if op.type == "redact"}:
        doc[i].apply_redactions()
    for op, span in retext:  # only the characters go: images and drawings under them stay
        r = pymupdf.Rect(span["bbox"])
        doc[op.page].add_redact_annot(r + (0.5, r.height * 0.25, -0.5, -r.height * 0.25), fill=False, cross_out=False)
    for i in {op.page for op, _ in retext}:
        doc[i].apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    layers = {}  # page number -> scratch page holding that page's drawings
    for op, span in retext:
        layer = layers.setdefault(op.page, layer_for(doc[op.page]))
        if span.get("scanned"):  # the letters that show are part of the scan's picture: paint over them first
            span = cover_scan(layer, doc[op.page], span)
        write_like(layer, doc[op.page], span, op.text)
    for op in todo:
        if op.type in ("redact", "retext"):
            continue
        layer = layers.setdefault(op.page, layer_for(doc[op.page]))
        if op.type == "text" and op.points and op.text.strip():
            x, y = op.points[0]
            draw_text(layer, x + FONT.text_length(op.text, fontsize=op.size) / 2, y + op.size / 2, op.text, op.size, op.color)
        elif op.type == "ink" and len(op.points) > 1:
            layer.draw_polyline(op.points, color=rgb(op.color), width=op.width, lineCap=1, lineJoin=1)
        elif op.type == "image" and op.rect and op.image.startswith("data:image/"):
            x0, y0, x1, y1 = op.rect
            draw_image(layer, (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0), abs(y1 - y0), base64.b64decode(op.image.partition(",")[2]))
        elif op.type == "whiteout" and op.rect:
            layer.draw_rect(pymupdf.Rect(op.rect).normalize(), color=None, fill=(1, 1, 1))
        elif op.type == "highlight" and op.rect:
            layer.draw_rect(pymupdf.Rect(op.rect).normalize(), color=None, fill=rgb(op.color), fill_opacity=0.35)
    for i, layer in layers.items():
        if layer.get_contents():
            place(doc[i], doc[i].rect, layer.parent, 0)
    return doc


@tool("Text on a page", "Internal")
def text_spans(doc: PDF, page: Annotated[int, Form(ge=1)] = 1):
    """The page's text, piece by piece, with its font, size and colour, and the font new text in each piece will use.
    Boxes are in points on the page as displayed. Used by the editor's Edit text mode."""
    if page > len(doc):
        raise ToolError(f"This document has {len(doc)} pages")
    p = doc[page - 1]
    out = []
    for s in page_spans(p):
        r = (pymupdf.Rect(s["bbox"]) * p.rotation_matrix).normalize()
        if s.get("scanned"):  # the colours and size the new text will get, so the editor's preview shows them too
            s = scan_look(p, s)
        paper = s.get("paper")
        o = pymupdf.Point(s["origin"]) * p.rotation_matrix
        out.append({"text": s["text"], "rect": [round(v, 2) for v in r], "baseline": [round(o.x, 2), round(o.y, 2)],
                    "size": round(s["size"], 2),
                    "color": "#%06x" % s["color"], "font": s["font"].split("+", 1)[-1], "uses": font_for(s["font"], s["flags"])[1],
                    "bold": bool(s["flags"] & 16), "italic": bool(s["flags"] & 2), "serif": bool(s["flags"] & 4),
                    "mono": bool(s["flags"] & 8), "scanned": bool(s.get("scanned")),
                    "paper": paper and "#%02x%02x%02x" % tuple(round(v * 255) for v in paper)})
    return {"page": page, "spans": out}


@tool("Edit metadata", "Edit")
def metadata(doc: PDF, title: str = "", author: str = "", subject: str = "", keywords: str = "", creator: str = "",
             producer: str = "", clear_others: bool = False):
    """Set the document properties. Empty fields keep their current value unless "clear others" is ticked."""
    given = {"title": title, "author": author, "subject": subject, "keywords": keywords, "creator": creator, "producer": producer}
    if clear_others:
        doc.set_metadata({})  # an empty dict wipes everything
        doc.del_xml_metadata()
    if new := {k: v for k, v in given.items() if v}:
        doc.set_metadata(new)  # a partial dict leaves the other properties alone
    return doc


@tool("Edit bookmarks", "Edit")
def bookmarks(doc: PDF, outline: Annotated[str, Form(
        description='JSON list of [level, title, page], e.g. [[1, "Intro", 1], [2, "Scope", 2]]. [] removes all bookmarks. The Info tool shows the current list.',
        json_schema_extra={"format": "textarea"})]):
    """Replace the table of contents shown in the reader's sidebar."""
    doc.set_toc([list(item) for item in TypeAdapter(list[tuple[int, str, int]]).validate_json(outline)])
    return doc


@tool("Fill form", "Edit")
def fill_form(doc: PDF, values: Annotated[str, Form(
        description='JSON object of field name and value, e.g. {"name": "Ada", "agree": true}. The Info tool lists the field names.',
        json_schema_extra={"format": "textarea"})], flatten: bool = False):
    """Fill in form fields. Flatten then burns the answers into the page so they can't be changed."""
    data = TypeAdapter(dict[str, bool | int | float | str]).validate_json(values)
    seen = set()
    for page in doc:
        for widget in page.widgets():
            if widget.field_name in data:
                value = data[widget.field_name]
                # shortcut: radio groups need each button's own "on" name, so only text, choice and check boxes are filled
                widget.field_value = value if isinstance(value, bool) else str(value)
                widget.update()
                seen.add(widget.field_name)
    if missing := data.keys() - seen:
        raise ToolError(f"No such form field: {', '.join(sorted(missing))}")
    if flatten:
        doc.bake(annots=False, widgets=True)
    return doc


@tool("Unlock form fields", "Edit")
def unlock_forms(doc: PDF):
    """Make read-only form fields editable again."""
    for xref in range(1, doc.xref_length()):  # every object, so flags inherited from parent fields are cleared too
        try:
            kind, flags = doc.xref_get_key(xref, "Ff")
        except Exception:  # not a dictionary
            continue
        if kind == "int" and int(flags) & pymupdf.PDF_FIELD_IS_READ_ONLY:
            doc.xref_set_key(xref, "Ff", str(int(flags) & ~pymupdf.PDF_FIELD_IS_READ_ONLY))
    return doc


@tool("Flatten", "Edit")
def flatten(doc: PDF, forms_only: bool = False):
    """Burn form fields and annotations into the page so they can no longer be edited."""
    doc.bake(annots=not forms_only, widgets=True)
    return doc


@tool("Add attachments", "Edit")
def attach(doc: PDF, attachments: list[Upload]):
    """Embed files inside the PDF."""
    for a in attachments:
        doc.embfile_add(a.name, a.data, filename=a.name)
    return doc


@tool("Extract attachments", "Edit")
def extract_attachments(doc: PDF):
    """Save the files embedded in the PDF."""
    names = doc.embfile_names()
    if not names:
        raise ToolError("This PDF has no attachments")
    return [(f"_{name}", doc.embfile_get(name)) for name in names]


@tool("Remove annotations", "Edit")
def remove_annotations(doc: PDF, links: bool = False):
    """Delete comments, highlights and other mark-up, and optionally links. Form fields are kept."""
    for page in doc:
        annot = page.first_annot
        while annot:
            annot = page.delete_annot(annot)
        for link in page.get_links() if links else []:
            page.delete_link(link)
    return doc


@tool("Remove images", "Edit")
def remove_images(doc: PDF, pages: Pages = "all"):
    """Blank out the pictures on the selected pages, leaving text and drawings."""
    for i in set(parse_pages(pages, len(doc))):
        for image in doc[i].get_images():
            doc[i].delete_image(image[0])
    return doc


@tool("Adjust colours", "Edit")
def adjust_colors(doc: PDF, brightness: Factor = 1, contrast: Factor = 1, saturation: Factor = 1, dpi: Dpi = 150):
    """Change brightness, contrast and saturation. Pages become pictures."""
    def fx(im):
        for enhancer, amount in ((ImageEnhance.Brightness, brightness), (ImageEnhance.Contrast, contrast), (ImageEnhance.Color, saturation)):
            im = enhancer(im).enhance(amount)
        return im
    return image_pdf(doc, dpi, fx)


@tool("Invert colours", "Edit")
def invert_colors(doc: PDF, dpi: Dpi = 150):
    """Turn pages into their negative, for reading in the dark. Pages become pictures."""
    return image_pdf(doc, dpi, ImageOps.invert)


@tool("Grayscale", "Edit")
def grayscale(doc: PDF):
    """Convert all colours to gray. Text and drawings stay sharp."""
    doc.recolor(1)
    return doc


@tool("Scanner effect", "Edit")
def scan_effect(doc: PDF, dpi: Dpi = 150, skew: Annotated[float, Form(ge=0, le=5, description="Largest tilt in degrees")] = 0.8,
                grayscale: bool = True):
    """Make a PDF look printed and scanned: slightly tilted, soft and grainy."""
    def fx(im):
        im = im.convert("L" if grayscale else "RGB")
        im = im.rotate(random.uniform(-skew, skew), resample=Image.Resampling.BICUBIC, fillcolor="white")
        im = im.filter(ImageFilter.GaussianBlur(0.5))
        return Image.blend(im, Image.effect_noise(im.size, 40).convert(im.mode), 0.12)
    return image_pdf(doc, dpi, fx, quality=60)
