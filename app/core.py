"""Tool registry, page-range parser and the helpers every tool shares."""
import inspect
import io
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Callable, Literal, get_args, get_origin

import pymupdf
from fastapi import Form
from PIL import Image
from pydantic import TypeAdapter

PDF = pymupdf.Document
EMPTY = inspect.Parameter.empty
MAX_PIXELS = 80_000_000  # per rendered page, so page size x DPI can't exhaust memory
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
