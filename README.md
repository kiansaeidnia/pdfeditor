# pdfeditor

A self-hosted PDF toolkit: 59 tools behind one web page and one HTTP API. Nothing is kept on the server: each
request is processed on its own and its files are gone when the response is sent.

## Quick start

```bash
docker compose up -d --build
```

Open <http://localhost:8080>. The first build downloads LibreOffice and takes a few minutes; the image is about 1.2 GB.
Out of the box it only answers on the machine it runs on. To reach it from other devices, set `AUTH_PASSWORD` and
`BIND=0.0.0.0` in `.env`.

## Configuration

Set these in a `.env` file next to `docker-compose.yml` (copy `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `BIND` | `127.0.0.1` | Address the app is published on. The default is this machine only; `0.0.0.0` opens it to the network. |
| `PORT` | `8080` | Port it is published on. |
| `AUTH_USER` | `admin` | Login name. |
| `AUTH_PASSWORD` | empty | Login password. **Empty means no login at all.** |
| `MAX_UPLOAD_MB` | `200` | Largest accepted request. |
| `CONVERT_TIMEOUT` | `300` | Seconds LibreOffice or OCR may spend on one file. |
| `WORKERS` | `2` | Server processes. Each handles one PDF job at a time. |
| `APP_NAME` | `pdfeditor` | Title shown in the UI and the API docs. |
| `OCR_LANGS` | `eng` | Tesseract languages built into the image, e.g. `eng deu fra` (build-time). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | empty, `587` | Lets users e-mail a result. Port 465 uses TLS from the start, any other port STARTTLS; a server without TLS is refused. Only active when `AUTH_PASSWORD` is set too. |

## What it does

- **Pages (18):** merge (append or interleave), split (at pages, by size or count, by chapters, into sections, at blank
  pages), remove blank pages, extract or reorder, remove, rotate, insert blank pages, crop, auto-crop margins, resize,
  N-up, booklet, single long page, overlay.
- **Convert (9):** anything to PDF (images, Office documents, HTML, Markdown, text, e-mail, EPUB, SVG, comics), web page
  to PDF, PDF to images, to text / Markdown / HTML / XML, tables to CSV, to Word / PowerPoint / OpenDocument, OCR,
  PDF/A, image-only PDF.
- **Edit (17):** stamp, page numbers (including Bates numbering), find and replace text, highlight text, metadata,
  bookmarks, fill form, unlock form fields, flatten, add and extract attachments, remove annotations, remove images,
  adjust colours, invert colours, grayscale, scanner effect.
- **Security (8):** add password and permissions, remove password, sanitize, redact text, watermark, sign with a
  certificate, validate signatures, remove signatures.
- **Inspect (7):** info, show JavaScript, compare two PDFs, compress, repair, extract images, auto-rename.

Every tool accepts a password-protected PDF (give the password with the request) and several files at once (the
result is then a zip). Results are always saved **without** a password; add one back with "Add password".

The web page also has three visual views: a **page organizer** (drag pages, rotate, duplicate, delete), an
**editor** (type, draw, highlight, white out, redact, place images and a hand-drawn signature) and a **pipeline
builder** that chains tools.

### Page selections

Wherever a tool asks for pages: `1,3,5-9` · `7-` (7 to the end) · `last` · `odd` · `even` · `2n+1` (every second page
from 1) · `reverse` · `all`. Order and repeats are kept, so `3,1,1` means page 3, then page 1 twice.

## HTTP API

Interactive documentation for every tool is at `/api/docs`. `GET /api/tools` lists the tools and their settings as JSON.

```bash
# one tool: rotate pages 1-3 by 90 degrees
curl -F files=@in.pdf -F angle=90 -F pages=1-3 -o out.pdf http://localhost:8080/api/tools/rotate

# a pipeline: each step's output feeds the next
curl -F files=@scan.pdf -o out.pdf http://localhost:8080/api/pipeline \
  -F 'steps=[{"tool": "remove-blank"}, {"tool": "ocr"}, {"tool": "compress", "params": {"level": "high"}}]'
```

With a login set, add `-u admin:yourpassword`. Errors come back as JSON: `{"detail": "what went wrong"}`.

## Security notes

- There is **no login until you set `AUTH_PASSWORD`**. Set it, and put the app behind HTTPS, before exposing it
  beyond a network you trust. The login is HTTP Basic, which is only safe over HTTPS.
- "Web page to PDF" and HTML conversion only fetch from public internet addresses and never run JavaScript. The
  address check and the download resolve the name separately, so a hostile DNS server could still redirect one to an
  internal address; do not offer this tool to untrusted users on a network with sensitive internal services.
- Uploaded Office documents are opened by LibreOffice, and PDFs by MuPDF. Keep the image up to date.
- Redaction removes the text and the image pixels under it, not just covers them. Regular expressions in the redact
  and highlight tools run without a time limit.

## Limits

- One shared login; no user accounts, roles or single sign-on.
- English interface only.
- Certificate signing takes PKCS#12 (`.p12`/`.pfx`) files only.
- Scanned stacks are split at blank separator sheets, not QR-code sheets.
- Each worker runs one PDF job at a time; raise `WORKERS` for more.

## Licence of the parts

The PDF engine (PyMuPDF) and Ghostscript are AGPL-licensed. Using this privately is unrestricted; if you let other
people use your instance over a network, the AGPL requires you to offer them this source code.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest -q
```

Tests that need LibreOffice, Tesseract, Ghostscript or WeasyPrint are skipped when those are missing. CI runs the
whole suite inside the Docker image with `REQUIRE_ALL=1`, where a missing program is a failure instead of a skip:

```bash
docker build -t pdfeditor:dev .
docker run --rm -e REQUIRE_ALL=1 -v "$PWD/tests:/srv/tests:ro" -v "$PWD/pyproject.toml:/srv/pyproject.toml:ro" \
  -v "$PWD/requirements-dev.txt:/srv/requirements-dev.txt:ro" pdfeditor:dev \
  sh -c "pip install -q --user -r requirements-dev.txt && python -m pytest -q"
```

A tool is one decorated function in `app/tools/`; its signature is its schema, so the API endpoint, the API docs and
the web form all come from it.
