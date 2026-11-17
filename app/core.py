"""Tool registry, page-range parser and the helpers every tool shares."""
import inspect
import io
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Callable, Literal, get_args, get_origin

import pymupdf
from fastapi import Form, HTTPException, UploadFile
from PIL import Image
from pydantic import TypeAdapter

PDF = pymupdf.Document
EMPTY = inspect.Parameter.empty
MAX_PIXELS = 80_000_000  # per rendered page, so page size x DPI can't exhaust memory
Image.MAX_IMAGE_PIXELS = MAX_PIXELS // 2  # Pillow refuses uploaded pictures above twice this, same reason
FONT = pymupdf.Font("helv")  # TextWriter falls back to MuPDF's built-in fonts for glyphs Helvetica lacks
PERMS = {
    "print": pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ,
    "copy": pymupdf.PDF_PERM_COPY,
    "modify": pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ASSEMBLE,
    "annotate": pymupdf.PDF_PERM_ANNOTATE,
    "fill_forms": pymupdf.PDF_PERM_FORM,
}


class ToolError(Exception):
    """A problem the user can fix (bad input, wrong password, ...). Becomes HTTP 400."""


@dataclass
class Upload:
    name: str
    data: bytes


# Form-field types shared by the tools. The UI renders inputs from their JSON schema.
Pages = Annotated[str, Form(description="e.g. 1,3,5-9 · 7- · 2n+1 · odd · even · last · reverse · all")]
Dpi = Annotated[int, Form(ge=36, le=600, description="Resolution in DPI")]
Color = Annotated[str, Form(pattern="^#[0-9a-fA-F]{6}$", json_schema_extra={"format": "color"})]
Text = Annotated[str, Form(json_schema_extra={"format": "textarea"})]
Secret = Annotated[str, Form(json_schema_extra={"format": "password"})]
Opacity = Annotated[float, Form(ge=0.02, le=1)]
Anchor = Literal["top-left", "top-center", "top-right", "center-left", "center", "center-right",
                 "bottom-left", "bottom-center", "bottom-right"]

# annotation of an upload parameter -> (kind, required)
UPLOADS = {PDF: ("pdf", True), list[PDF]: ("pdfs", True), Upload: ("file", True),
           Upload | None: ("file", False), list[Upload]: ("files", True)}


@dataclass
class Tool:
    id: str
    title: str
    category: str
    fn: Callable
    input: str    # what the main upload is: pdf | pdfs | file | files | "" (nothing)
    accept: str   # file-picker filter for the main upload
    params: dict  # name -> (annotation, default): plain form fields
    files: dict   # name -> (kind, required, accept): extra uploads

    def describe(self) -> dict:
        return {
            "id": self.id, "title": self.title, "category": self.category,
            "description": inspect.getdoc(self.fn) or "", "input": self.input, "accept": self.accept,
            "params": [{"name": n, "required": d is EMPTY, "default": None if d is EMPTY else d,
                        **TypeAdapter(a).json_schema()} for n, (a, d) in self.params.items()],
            "files": [{"name": n, "kind": k, "required": r, "accept": a} for n, (k, r, a) in self.files.items()],
        }


TOOLS: dict[str, Tool] = {}


def tool(title: str, category: str, accept: str = ".pdf"):
    """Register a function as a tool. Its signature is the schema: the first parameter says what is
    uploaded (see UPLOADS), other upload-typed parameters are extra files, the rest are form fields.

    A tool returns a PDF, a dict (JSON), a `(suffix, data)` pair or a list of such pairs. The suffix is
    appended to the input file's name; a leading "=" makes it the whole name instead.
    """
    def register(fn):
        t = Tool(fn.__name__.replace("_", "-"), title, category, fn, "", accept, {}, {})
        for i, p in enumerate(inspect.signature(fn, eval_str=True).parameters.values()):
            base, *meta = get_args(p.annotation) if get_origin(p.annotation) is Annotated else (p.annotation,)
            if base not in UPLOADS:
                t.params[p.name] = (p.annotation, p.default)
            elif i == 0:
                t.input = UPLOADS[base][0]
            else:
                accept_extra = next((m for m in meta if isinstance(m, str)), ".pdf" if base is PDF else "")
                t.files[p.name] = (*UPLOADS[base], accept_extra)
        TOOLS[t.id] = t
        return fn
    return register


def parse_pages(spec: str, n: int) -> list[int]:
    """Page selection -> 0-based indices, in the order written (repeats allowed)."""
    out: list[int] = []
    for tok in filter(None, (spec.strip() or "all").lower().replace(" ", "").split(",")):
        if tok == "all":
            out += range(n)
        elif tok == "reverse":
            out += range(n - 1, -1, -1)
        elif tok in ("odd", "even"):
            out += range(tok == "even", n, 2)
        elif m := re.fullmatch(r"([1-9]\d*)?n([+-]\d+)?", tok):
            a, b = int(m[1] or 1), int(m[2] or 0)
            out += [p - 1 for k in range(n + 1) if 1 <= (p := a * k + b) <= n]
        elif m := re.fullmatch(r"(\d+|last)(-(\d+|last)?)?", tok):
            a = n if m[1] == "last" else int(m[1])
            b = a if not m[2] else n if m[3] in (None, "last") else int(m[3])
            if not (1 <= a <= n and 1 <= b <= n):
                raise ToolError(f"Page selection '{tok}' is outside this document ({n} pages)")
            out += range(a - 1, b) if a <= b else range(a - 1, b - 2, -1)
        else:
            raise ToolError(f"Can't read page selection '{tok}'")
    return out


def open_pdf(u: Upload, password: str = "") -> PDF:
    try:
        doc = pymupdf.open(stream=u.data, filetype="pdf")
    except Exception as e:
        raise ToolError(f"{u.name} is not a readable PDF") from e
    if doc.needs_pass and not doc.authenticate(password):
        raise ToolError(f"{u.name}: " + ("wrong password" if password else "this PDF needs a password"))
    return doc


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ()]+", "_", name).strip(". ") or "file"


MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))


def read(f: UploadFile) -> Upload:
    data = f.file.read(MAX_MB * 2**20 + 1)  # also caps chunked uploads, which carry no Content-Length
    if len(data) > MAX_MB * 2**20:
        raise HTTPException(413, f"{f.filename} is larger than {MAX_MB} MB")
    return Upload(safe_name(Path(f.filename or "file").name), data)


def _data(x) -> bytes:
    if isinstance(x, PDF):
        if not len(x):
            raise ToolError("The result would have no pages")
        return x.tobytes(garbage=3, deflate=True)  # always saved without encryption
    if isinstance(x, (dict, list)):
        return json.dumps(x, indent=2, default=str, ensure_ascii=False).encode()
    return x.encode() if isinstance(x, str) else x


def run_tool(t: Tool, uploads: list[Upload], kw: dict, password: str = "") -> list[Upload]:
    """Run a tool once over all uploads (many-file tools) or once per upload (single-file tools)."""
    if t.input and not uploads:
        raise ToolError("Upload at least one file")
    out = []
    for group in [[u] for u in uploads] if t.input in ("pdf", "file") else [uploads]:
        if t.input == "pdf":
            args = [open_pdf(group[0], password)]
        elif t.input == "pdfs":
            args = [[open_pdf(u, password) for u in group]]
        else:
            args = [group[0] if t.input == "file" else group] if t.input else []
        res = t.fn(*args, **kw)
        if isinstance(res, PDF):
            res = [(f"_{t.id}.pdf", res)]
        elif isinstance(res, dict):
            res = [(f"_{t.id}.json", res)]
        elif isinstance(res, tuple):
            res = [res]
        stem = Path(group[0].name).stem if group else "output"
        out += [Upload(safe_name(s[1:] if s.startswith("=") else stem + s), _data(d)) for s, d in res]
    return out


# --- drawing helpers (all coordinates are points on the page *as displayed*) ---------------------

def rgb(color: str) -> tuple:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def render(page: pymupdf.Page, dpi: int, **kw) -> pymupdf.Pixmap:
    if page.rect.width * page.rect.height * (dpi / 72) ** 2 > MAX_PIXELS:
        raise ToolError(f"Page {page.number + 1} is too large to render at {dpi} DPI")
    return page.get_pixmap(dpi=dpi, **kw)


def place(target: pymupdf.Page, rect, src: PDF, pno: int, clip=None, overlay: bool = True) -> None:
    """Draw page `pno` of `src` into `rect` of `target`, both as displayed."""
    page = src[pno]
    turn = page.rotation
    if clip is not None:
        clip = (clip * page.derotation_matrix).normalize()
    back = target.rotation
    box = (pymupdf.Rect(rect) * target.derotation_matrix).normalize()
    page.set_rotation(0)  # show_pdf_page ignores /Rotate, so pass the rotation explicitly instead
    target.set_rotation(0)
    try:
        target.show_pdf_page(box, src, pno, rotate=(back - turn) % 360, clip=clip, overlay=overlay)
    finally:
        page.set_rotation(turn)
        target.set_rotation(back)


def layer_for(page: pymupdf.Page) -> pymupdf.Page:
    """A blank scratch page the size of `page` as displayed. Draw on it, then `place` it on the page:
    that keeps all rotation and crop handling in `place` (PyMuPDF misplaces text on cropped pages)."""
    return pymupdf.open().new_page(width=page.rect.width, height=page.rect.height)


def draw_text(layer, cx: float, cy: float, text: str, size: float, color: str = "#000000",
              opacity: float = 1, angle: float = 0) -> None:
    """Write one line centred on (cx, cy) of a scratch page, turned `angle` degrees counter-clockwise."""
    writer = pymupdf.TextWriter(layer.rect, opacity=opacity, color=rgb(color))
    writer.append((cx - FONT.text_length(text, fontsize=size) / 2, cy + size * 0.35), text, font=FONT, fontsize=size)
    writer.write_text(layer, morph=(pymupdf.Point(cx, cy), pymupdf.Matrix(angle)))


@lru_cache(maxsize=4)
def _stamp_png(data: bytes, turn: float, opacity: float) -> tuple:
    """(PNG of the image turned and faded, original width, original height, turned width, turned height)."""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as e:
        raise ToolError("That image can't be read") from e
    if opacity < 1:
        im.putalpha(im.getchannel("A").point(lambda a: int(a * opacity)))
    out = im.rotate(turn, expand=True, resample=Image.Resampling.BICUBIC) if turn else im
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), im.width, im.height, out.width, out.height


def draw_image(layer, cx: float, cy: float, w: float, h: float, data: bytes,
               opacity: float = 1, angle: float = 0) -> None:
    """Fit an image inside a w x h box centred on (cx, cy) of a scratch page, turned counter-clockwise."""
    png, iw, ih, rw, rh = _stamp_png(data, angle % 360, opacity)
    k = min(w / iw, h / ih) / 2
    layer.insert_image(pymupdf.Rect(cx - rw * k, cy - rh * k, cx + rw * k, cy + rh * k), stream=png)


def put(page, anchor: str, *, text: str = "", image: bytes | None = None, size: float = 12,
        image_width: float = 30, color: str = "#000000", opacity: float = 1, angle: float = 0,
        margin: float = 36) -> None:
    """Stamp text or an image at an anchor ("top-left" ... "bottom-right", "center") or tiled over the page."""
    layer = _stamp_layer(round(page.rect.width, 2), round(page.rect.height, 2), anchor, text, image, size,
                         image_width, color, opacity, angle, margin)
    place(page, page.rect, layer.parent, 0)


@lru_cache(maxsize=16)  # the same stamp on same-sized pages is drawn once and reused
def _stamp_layer(W, H, anchor, text, image, size, image_width, color, opacity, angle, margin) -> pymupdf.Page:
    page = pymupdf.open().new_page(width=W, height=H)
    if image:
        _, iw, ih, _, _ = _stamp_png(image, 0, 1)
        bw = W * image_width / 100
        bh = bw * ih / iw
    elif text.strip():
        bw, bh = FONT.text_length(text, fontsize=size), size
    else:
        raise ToolError("Enter some text or choose an image")
    if anchor == "tile":
        sx, sy = bw * 1.4 + 20, bh + bw * 0.5 + 20
        centers = [(x * sx + sx / 2, y * sy + sy / 2) for y in range(int(H // sy) + 1) for x in range(int(W // sx) + 1)]
        if len(centers) > 400:
            raise ToolError("Too small to tile - increase the size")
    else:
        v, _, hz = anchor.partition("-")
        centers = [({"left": margin + bw / 2, "right": W - margin - bw / 2}.get(hz or v, W / 2),
                    {"top": margin + bh / 2, "bottom": H - margin - bh / 2}.get(v, H / 2))]
    for cx, cy in centers:
        if image:
            draw_image(page, cx, cy, bw, bh, image, opacity, angle)
        else:
            draw_text(page, cx, cy, text, size, color, opacity, angle)
    return page


def find_text(page, find: str, regex: bool = False) -> list:
    """Quads of every occurrence of the search terms (one per line). Matching ignores case."""
    terms = [t.strip() for t in find.splitlines() if t.strip()]
    if regex:
        # shortcut: patterns run with no time limit, so a pathological one can hang this worker; use the `regex` package's timeout if untrusted users get access
        try:
            terms = {" ".join(m.group().split()) for t in terms for m in re.finditer(t, page.get_text())}
        except re.error as e:
            raise ToolError(f"Bad regular expression: {e}") from e
    return [q for t in terms if t for q in page.search_for(t, quads=True)]


# --- writing text in the document's own font ------------------------------------------------------
# Fonts embedded in most PDFs are subsets without a character map, so new text can't be typed in them. New text uses
# the embedded font when it can, else the same font installed here, else a metric-compatible twin, else a lookalike.

FONT_DIRS = (os.environ["FONT_DIRS"].split(os.pathsep) if "FONT_DIRS" in os.environ
             else ["/usr/share/fonts", "/usr/local/share/fonts", "C:/Windows/Fonts"])
_STYLE = re.compile(r"(semibold|demibold|extrabold|bold|black|heavy|demi|bd|italic|oblique|ita|regular|roman|book|light|medium|psmt|mt|ps)")
# a family -> families that look and measure the same, best first (names as fonts call themselves)
_TWINS = {
    "arial": ["helvetica", "liberationsans", "nimbussans"], "helvetica": ["arial", "liberationsans", "nimbussans"],
    "helveticaneue": ["arial", "liberationsans"],
    "timesnewroman": ["times", "liberationserif", "nimbusroman"], "times": ["timesnewroman", "liberationserif", "nimbusroman"],
    "couriernew": ["courier", "liberationmono", "nimbusmonops"], "courier": ["couriernew", "liberationmono", "nimbusmonops"],
    "calibri": ["carlito"], "cambria": ["caladea"], "arialnarrow": ["nimbussansnarrow"],
    "palatino": ["p052"], "palatinolinotype": ["p052"], "bookantiqua": ["p052"], "centuryschoolbook": ["c059"],
    "bookmanoldstyle": ["urwbookman"], "centurygothic": ["urwgothic"], "avantgarde": ["urwgothic"],
    "verdana": ["dejavusans"], "tahoma": ["dejavusans"], "segoeui": ["liberationsans"], "georgia": ["dejavuserif"],
    "consolas": ["dejavusansmono"], "lucidaconsole": ["dejavusansmono"],
}


def _family(name: str) -> tuple[str, bool, bool]:
    """'ABCDEF+Arial-BoldItalicMT' -> ('arial', True, True)."""
    n = re.sub(r"[^a-z0-9]", "", name.split("+", 1)[-1].lower())
    bold = bool(re.search(r"semibold|demibold|extrabold|bold|black|heavy|demi|bd", n))
    italic = bool(re.search(r"italic|oblique|ita", n))
    return _STYLE.sub("", n), bold, italic


TWINS = {_family(k)[0]: [_family(v)[0] for v in vs] for k, vs in _TWINS.items()}  # both sides spelled as _family() does


@lru_cache(maxsize=1)
def _installed() -> dict:
    """(family, bold, italic) -> font file, for the fonts on this machine."""
    out = {}
    for d in FONT_DIRS:
        for path in sorted(Path(d).rglob("*")) if Path(d).is_dir() else []:
            if path.suffix.lower() in (".ttf", ".otf"):
                try:  # the name inside the file says the style; file names often don't ("timesi.ttf")
                    out.setdefault(_family(pymupdf.Font(fontfile=str(path)).name), str(path))
                except Exception:  # a file FreeType can't read is skipped
                    continue
                out.setdefault(_family(path.stem), str(path))
    return out


@lru_cache(maxsize=64)
def _font_file(path: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=path)


def font_for(name: str, flags: int = 0) -> tuple[pymupdf.Font, str]:
    """The font to write new text in for a span set in `name`, and what it is called, e.g. 'Liberation Sans Bold'."""
    family, bold, italic = _family(name)
    bold, italic = bold or bool(flags & 16), italic or bool(flags & 2)
    kind = "liberationmono" if flags & 8 else "liberationserif" if flags & 4 else "liberationsans"
    fonts = _installed()
    for style in ((bold, italic), (bold, False), (False, False)):  # a twin in the right style beats the family in the wrong one
        for fam in [family, *TWINS.get(family, []), kind]:
            if (fam, *style) in fonts:
                f = _font_file(fonts[(fam, *style)])
                return f, f.name
    base = {(False, False): "helv", (True, False): "hebo", (False, True): "heit", (True, True): "hebi"}[(bold, italic)]
    if flags & 4:
        base = {"helv": "tiro", "hebo": "tibo", "heit": "tiit", "hebi": "tibi"}[base]
    elif flags & 8:
        base = {"helv": "cour", "hebo": "cobo", "heit": "coit", "hebi": "cobi"}[base]
    f = pymupdf.Font(base)
    return f, f.name


def embedded_font(page: pymupdf.Page, name: str) -> pymupdf.Font | None:
    """The page's own copy of a font, if it is embedded and loads."""
    for xref, _, _, basefont, *_ in page.get_fonts(full=True):
        if basefont.split("+", 1)[-1] == name.split("+", 1)[-1]:
            try:
                buf = page.parent.extract_font(xref)[3]
                return pymupdf.Font(fontbuffer=buf) if buf else None
            except Exception:  # a font MuPDF can't load: use the installed match instead
                return None
    return None


def write_like(layer: pymupdf.Page, page: pymupdf.Page, span: dict, text: str, size: float | None = None) -> None:
    """Write `text` on a scratch layer where `span` (from page.get_text('dict')) sits, as close to its look as this
    server can: its font, size, colour and baseline. Each character uses the first font that has it."""
    if not text:
        return
    size = size or span["size"]
    own = embedded_font(page, span["font"])
    fonts = [f for f in (own, font_for(span["font"], span["flags"])[0], FONT) if f is not None]
    origin = pymupdf.Point(span["origin"]) * page.rotation_matrix  # the layer is the page as displayed
    writer = pymupdf.TextWriter(layer.rect, color=pymupdf.sRGB_to_pdf(span["color"]))
    pos, run, current = origin, "", None
    for ch in text + " ":  # runs of characters that share a font; the extra space ends the last run
        if ch.isspace():
            # a gap, not a space glyph: some fonts share that glyph with the no-break space, which then comes back
            # out of the PDF and breaks search and copy; readers turn the gap into a space themselves
            if run:
                pos = writer.append(pos, run, font=current, fontsize=size)[1]
                run = ""
            pos = pos + ((current or fonts[0]).text_length(" ", fontsize=size), 0)
            continue
        f = next((f for f in fonts if f.has_glyph(ord(ch))), FONT)
        if run and f is not current:
            pos = writer.append(pos, run, font=current, fontsize=size)[1]
            run = ""
        run, current = run + ch, f
    writer.write_text(layer, morph=(origin, pymupdf.Matrix(-page.rotation)))


def page_spans(page: pymupdf.Page) -> list[dict]:
    """The text of a page in page.get_text('dict') span form (unrotated coordinates), level lines only.
    Invisible text - the OCR layer over a scan - comes a word at a time, sized wrongly and with no real font: each line
    of it becomes one span, sized by its box, marked "scanned", in a plain sans-serif."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            if abs(line["dir"][1]) > 0.05:  # scans lean a little; text set at an angle is left alone
                continue
            spans = [s for s in line["spans"] if s["text"].strip()]
            hidden = [s for s in spans if s.get("alpha") == 0]
            if hidden:
                box = pymupdf.Rect(hidden[0]["bbox"])
                for s in hidden[1:]:
                    box |= s["bbox"]
                out.append({**hidden[0], "text": "".join(s["text"] for s in hidden).strip(), "bbox": tuple(box),
                            "size": box.height, "font": "Helvetica", "flags": 0, "scanned": True})
            out += [s for s in spans if s.get("alpha") != 0]
    return out


def _grown(span: dict) -> pymupdf.Rect:
    r = pymupdf.Rect(span["bbox"])
    return r + (-1.5, -0.15 * r.height, 1.5, 0.15 * r.height)  # anti-aliased edges and accents of a scanned line


def scan_look(page: pymupdf.Page, span: dict) -> dict:
    """A scanned line's span with what its picture shows: the paper colour (0-1 RGB, as "paper"), the ink colour and
    the letter size. OCR programs guess sizes differently, but the baseline they give is right, and capitals and tall
    letters reach about 0.7 of the size above it."""
    clip = _grown(span)
    turn = page.rotation
    page.set_rotation(0)  # read the picture the way the text's coordinates run
    try:
        clip &= page.rect
        pix = page.get_pixmap(clip=clip, dpi=144, colorspace=pymupdf.csRGB, alpha=False)
    finally:
        page.set_rotation(turn)
    px = list(zip(*[iter(pix.samples)] * 3))
    paper = Counter(tuple(v // 16 * 16 + 8 for v in c) for c in px).most_common(1)[0][0]  # the commonest colour, give or take noise
    gap = [sum((a - b) ** 2 for a, b in zip(c, paper)) for c in px]
    far = sorted(range(len(px)), key=gap.__getitem__)[-max(1, len(px) // 20):]  # the 5% of pixels least like the paper
    ink = [sum(px[i][k] for i in far) // len(far) for k in range(3)]
    inky = [sum(gap[y * pix.width + x] > gap[far[-1]] / 4 for x in range(pix.width)) for y in range(pix.height)]
    top = next((y for y, n in enumerate(inky) if n >= max(2, pix.width // 100)), None)  # a speck is not a letter
    height = pymupdf.Rect(span["bbox"]).height
    size = height if top is None else (span["origin"][1] - (clip.y0 + top * clip.height / pix.height)) / 0.7
    return {**span, "paper": tuple(v / 255 for v in paper), "color": (ink[0] << 16) | (ink[1] << 8) | ink[2],
            "size": min(max(size, 0.6 * height), 1.6 * height)}


def cover_scan(layer: pymupdf.Page, page: pymupdf.Page, span: dict) -> dict:
    """Paint over a scanned line in its paper colour, so new text can be written there; returns the span with the
    scan's ink colour and letter size, for the new text."""
    span = scan_look(page, span)
    layer.draw_rect((_grown(span) * page.rotation_matrix).normalize(), color=None, fill=span["paper"])
    return span


def chunks(doc: PDF, bounds: list[int]) -> list:
    """Cut at the page indices in `bounds` (first 0, last len) into (suffix, pdf) pairs."""
    out = []
    for a, b in zip(bounds, bounds[1:]):
        part = pymupdf.open()
        part.insert_pdf(doc, from_page=a, to_page=b - 1)
        out.append((f"_{a + 1}-{b}.pdf" if b - a > 1 else f"_{a + 1}.pdf", part))
    return out


def image_pdf(doc: PDF, dpi: int, fx=None, quality: int = 85) -> PDF:
    """Rebuild the document from pictures of its pages, optionally filtered by `fx` (PIL image -> PIL image)."""
    out = pymupdf.open()
    for page in doc:
        pix = render(page, dpi)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        (fx(im) if fx else im).save(buf, "JPEG", quality=quality)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=buf.getvalue())
    return out
