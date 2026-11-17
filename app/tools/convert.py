"""Conversions to and from PDF.

LibreOffice, OCRmyPDF (Tesseract + Ghostscript) and WeasyPrint are looked up when a tool runs, so the
rest of the app works without them. The Docker image ships all three.
"""
import csv
import email
import email.policy
import html
import io
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import markdown
import pymupdf
import pymupdf4llm
from fastapi import Form

from ..core import PDF, Dpi, Pages, ToolError, Upload, image_pdf, open_pdf, parse_pages, render, tool

TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT", "300"))
MAX_FETCH = 25 * 2**20  # per web resource
NAT64 = ipaddress.ip_network("64:ff9b::/96")
MUPDF_TYPES = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "jp2", "jpx", "pnm", "pgm", "ppm", "pbm",
               "svg", "epub", "mobi", "fb2", "cbz", "xps", "txt"}
PAGE_CSS = ("<style>@page{size:A4;margin:18mm}body{font:11pt/1.5 sans-serif}pre,code{font-family:monospace;"
            "background:#f4f4f4}pre{padding:8px;white-space:pre-wrap}table{border-collapse:collapse}"
            "td,th{border:1px solid #999;padding:4px 8px}img{max-width:100%}</style>")


def run(cmd: list[str], what: str) -> None:
    if not shutil.which(cmd[0]):
        raise ToolError(f"{what} is not installed on this server")
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{what} took longer than {TIMEOUT} seconds") from e
    if p.returncode:
        raise ToolError(f"{what} failed: {p.stderr.decode(errors='replace').strip()[-400:]}")


def office(data: bytes, ext: str, target: str, infilter: str = "") -> bytes:
    """Convert a file with headless LibreOffice. `ext` and `target` are file extensions."""
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        raise ToolError("That file type is not supported")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, f"in.{ext}")
        src.write_bytes(data)
        run(["soffice", f"-env:UserInstallation={Path(d, 'profile').as_uri()}", "--headless", "--norestore",
             *([f"--infilter={infilter}"] if infilter else []), "--convert-to", target, "--outdir", d, str(src)],
            "LibreOffice")
        out = Path(d, f"in.{target}")
        if not out.exists():
            raise ToolError(f"LibreOffice can't convert .{ext} to .{target}")
        return out.read_bytes()


def _public(address: str) -> bool:
    """A public address, also judging an IPv4 address carried inside an IPv6 one (mapped, 6to4, NAT64)."""
    ip = ipaddress.ip_address(address)
    inner = None
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour or (ipaddress.ip_address(int(ip) & 0xFFFFFFFF) if ip in NAT64 else None)
    return ip.is_global and (inner is None or inner.is_global)


def check_public(url: str) -> None:
    """Refuse anything but http(s) to a public address, so pages can't make the server read local files or
    reach its internal network (SSRF).
    shortcut: the name is resolved here and again by the request, so DNS rebinding can slip through; pin the resolved IP if untrusted users get access."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError(f"Only http(s) addresses are allowed: {url[:80]}")
    try:
        addresses = {a[4][0].split("%")[0] for a in socket.getaddrinfo(parts.hostname, None)}
    except OSError as e:
        raise ToolError(f"Can't find {parts.hostname}") from e
    if not all(_public(a) for a in addresses):
        raise ToolError(f"{parts.hostname} is not a public address")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def fetch(url: str, public: bool = True, timeout: int = 10) -> tuple:
    """(final URL, body, headers) of a resource WeasyPrint asks for: inline data: always, the public web only when `public`."""
    if url.startswith("data:"):
        resp = urllib.request.urlopen(url)  # never touches the network
    elif public:
        check_public(url)
        resp = _opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pdfeditor)"}), timeout=timeout)
    else:
        raise ToolError("External resources are blocked")
    body = resp.read(MAX_FETCH + 1)
    if len(body) > MAX_FETCH:
        raise ToolError(f"{url[:80]} is too large")
    return resp.url, body, resp.headers


def web_pdf(public: bool = True, **source) -> PDF:
    """Render HTML (string=, file_obj= or url=) with WeasyPrint. No JavaScript is run."""
    try:
        from weasyprint import HTML
        from weasyprint.urls import URLFetcher, URLFetcherResponse
    except (ImportError, OSError) as e:  # OSError: its native libraries are missing
        raise ToolError("WeasyPrint is not installed on this server") from e

    class Fetcher(URLFetcher):  # WeasyPrint 70 only takes a URLFetcher; replacing fetch() keeps its own file: and http openers unused
        def fetch(self, url, headers=None):
            return URLFetcherResponse(*fetch(url, public))
    return pymupdf.open("pdf", HTML(url_fetcher=Fetcher(), **source).write_pdf())


def _eml_html(data: bytes) -> str:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    body = msg.get_body(("html", "plain"))
    text = body.get_content() if body else ""
    if not body or body.get_content_type() != "text/html":
        text = f"<pre>{html.escape(text)}</pre>"
    head = "".join(f"<div><b>{h}:</b> {html.escape(str(msg[h]))}</div>" for h in ("From", "To", "Cc", "Date", "Subject") if msg[h])
    return f"{PAGE_CSS}{head}<hr>{text}"


def _to_pdf(u: Upload) -> PDF:
    ext = Path(u.name).suffix.lower().lstrip(".")
    if ext == "pdf":
        return open_pdf(u)
    if ext in ("html", "htm"):
        return web_pdf(file_obj=io.BytesIO(u.data))
    if ext in ("md", "markdown"):
        return web_pdf(string=PAGE_CSS + markdown.markdown(u.data.decode(errors="replace"), extensions=["tables", "fenced_code"]))
    if ext == "eml":
        return web_pdf(public=False, string=_eml_html(u.data))  # remote images in mail are trackers
    if ext in MUPDF_TYPES:
        try:
            return pymupdf.open("pdf", pymupdf.open(stream=u.data, filetype=ext).convert_to_pdf())
        except Exception as e:
            raise ToolError(f"{u.name} can't be read as .{ext}") from e
    return pymupdf.open("pdf", office(u.data, ext, "pdf"))


@tool("Convert to PDF", "Convert", accept="")
def to_pdf(files: list[Upload], merge: bool = True):
    """Turn images, Office documents (Word, Excel, PowerPoint, OpenDocument), HTML, Markdown, text, e-mail
    (.eml), EPUB, SVG or comics (.cbz) into PDF. Several files become one PDF in upload order unless merge is off."""
    docs = [(Path(u.name).stem, _to_pdf(u)) for u in files]
    if not merge or len(docs) == 1:
        return [(f"={stem}.pdf", d) for stem, d in docs]
    out = pymupdf.open()
    for _, d in docs:
        out.insert_pdf(d)
    return (".pdf", out)


@tool("Web page to PDF", "Convert")
def url_to_pdf(url: Annotated[str, Form(description="https://…")]):
    """Save a web page as PDF. JavaScript is not run, so script-built pages come out bare. Only public
    internet addresses are allowed."""
    check_public(url)
    return (f"={urlsplit(url).hostname}.pdf", web_pdf(url=url))


@tool("PDF to images", "Convert")
def pdf_to_images(doc: PDF, format: Literal["png", "jpg", "webp", "tiff", "gif", "bmp"] = "png", dpi: Dpi = 150,
                  grayscale: bool = False, pages: Pages = "all"):
    """Save pages as pictures (a zip when there is more than one)."""
    kind = {"jpg": "JPEG"}.get(format, format.upper())
    colorspace = pymupdf.csGRAY if grayscale else pymupdf.csRGB
    return [(f"_{i + 1}.{format}", render(doc[i], dpi, colorspace=colorspace).pil_tobytes(format=kind))
            for i in parse_pages(pages, len(doc))]


@tool("PDF to text, Markdown, HTML or XML", "Convert")
def pdf_to_text(doc: PDF, format: Literal["txt", "md", "html", "xml"] = "txt", pages: Pages = "all"):
    """Extract the text. Scanned pages have none until you run OCR."""
    chosen = parse_pages(pages, len(doc))
    if format == "md":
        return (".md", pymupdf4llm.to_markdown(doc, pages=chosen, show_progress=False))
    if format == "txt":
        return (".txt", "\n\f".join(doc[i].get_text() for i in chosen))
    body = "\n".join(doc[i].get_text("xhtml" if format == "html" else "xml") for i in chosen)
    if format == "html":
        return (".html", f"<!doctype html><html><head><meta charset='utf-8'></head><body>\n{body}\n</body></html>")
    return (".xml", f'<?xml version="1.0" encoding="UTF-8"?>\n<document>\n{body}\n</document>')


@tool("PDF tables to CSV", "Convert")
def pdf_to_csv(doc: PDF, pages: Pages = "all"):
    """Find tables and save each one as a CSV file."""
    out = []
    for i in parse_pages(pages, len(doc)):
        for k, table in enumerate(doc[i].find_tables().tables):
            buf = io.StringIO()
            csv.writer(buf).writerows(table.extract())
            out.append((f"_p{i + 1}_t{k + 1}.csv", buf.getvalue()))
    if not out:
        raise ToolError("No tables found")
    return out


@tool("PDF to Word, PowerPoint or OpenDocument", "Convert")
def pdf_to_office(doc: PDF, format: Literal["docx", "odt", "rtf", "pptx", "odp"] = "docx"):
    """Convert with LibreOffice. The layout is approximated; pages heavy on graphics come out best as pptx."""
    infilter = "impress_pdf_import" if format in ("pptx", "odp") else "writer_pdf_import"
    return (f".{format}", office(doc.tobytes(), "pdf", format, infilter))


def _ocrmypdf(doc: PDF, *options: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d, "in.pdf"), Path(d, "out.pdf")
        src.write_bytes(doc.tobytes())
        run(["ocrmypdf", *options, str(src), str(dst)], "OCRmyPDF")
        return dst.read_bytes()


@tool("OCR (make scans searchable)", "Convert")
def ocr(doc: PDF,
        language: Annotated[str, Form(pattern=r"^[A-Za-z_]+(\+[A-Za-z_]+)*$", description="Tesseract language codes, e.g. eng or eng+deu")] = "eng",
        mode: Literal["skip-text", "redo-ocr", "force-ocr"] = "skip-text", deskew: bool = False, pages: Pages = "all"):
    """Recognise the text on scanned pages and add it as an invisible, searchable layer. skip-text leaves
    pages that already have text alone; force-ocr rasterises and redoes everything."""
    chosen = sorted(set(parse_pages(pages, len(doc))))
    only = ["--pages", ",".join(str(i + 1) for i in chosen)] if len(chosen) < len(doc) else []
    return ("_ocr.pdf", _ocrmypdf(doc, "-l", language, f"--{mode}", "--output-type", "pdf", *only, *(["--deskew"] if deskew else [])))


@tool("PDF to PDF/A", "Convert")
def pdf_to_pdfa(doc: PDF, part: Literal["1", "2", "3"] = "2"):
    """Convert to PDF/A, the self-contained format for long-term archiving."""
    return ("_pdfa.pdf", _ocrmypdf(doc, "--tesseract-timeout", "0", "--skip-text", "--output-type", f"pdfa-{part}"))


@tool("Convert to image PDF", "Convert")
def rasterize(doc: PDF, dpi: Dpi = 150):
    """Replace every page with a picture of itself: nothing can be selected, copied or recovered afterwards."""
    return image_pdf(doc, dpi)
