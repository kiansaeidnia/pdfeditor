"""FastAPI app: one endpoint per registered tool, a pipeline endpoint, the guard in front of everything, the web UI.
Setup, accounts and file libraries are in accounts.py."""
import hashlib
import inspect
import io
import logging
import mimetypes
import os
import threading
import zipfile
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.params import Form as FormInfo
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, TypeAdapter

from . import accounts, db, settings, tools  # noqa: F401  (importing tools registers them)
from .core import EMPTY, MAX_MB, TOOLS, Tool, ToolError, Upload, open_pdf, read, run_tool

# Applied to the UI only: the interactive API docs load their script from a CDN.
CSP = ("default-src 'self'; img-src 'self' data: blob:; frame-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
log = logging.getLogger("pdfeditor")
# shortcut: PyMuPDF is not thread-safe, so one tool runs at a time per process; raise WORKERS for more throughput
lock = threading.Lock()

app = FastAPI(title=os.environ.get("APP_NAME", "pdfeditor"), version="1.0.0", docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)


if settings.current() is None:
    settings.announce_setup()
elif why := settings.missing(settings.current()):
    log.error(why)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # a form on another site, even a sibling subdomain the cookie trusts, can't make the browser post here;
    # scripts send no Sec-Fetch-Site header and are not affected
    if request.method not in ("GET", "HEAD") and request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return JSONResponse({"detail": "Cross-site request refused."}, 403)
    if path.startswith("/api/") and path not in accounts.OPEN:
        s = settings.current()
        if s is None:
            return JSONResponse({"detail": "Finish setting up pdfeditor first: open it in a browser."}, 503)
        if why := settings.missing(s):
            return JSONResponse({"detail": why}, 503)
        # the UI and its static files load without signing in, so the sign-in page can show; they hold no data
        if s["login"] == "accounts":
            try:
                user = await run_in_threadpool(accounts.signed_in, request, s)  # scrypt and the database: off the event loop
            except db.Down as e:
                return JSONResponse({"detail": f"The accounts database can't be reached: {e}"}, 503)
            if user is None:
                return JSONResponse({"detail": "Sign in first."}, 401)
            if path.startswith("/api/admin/") and user["role"] != "admin":
                return JSONResponse({"detail": "Only admins can do this."}, 403)
            request.state.user = user
    size = request.headers.get("content-length", "")
    if size.isdigit() and int(size) > MAX_MB * 2**20:
        return JSONResponse({"detail": f"Upload is larger than {MAX_MB} MB"}, 413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not request.url.path.startswith("/api/"):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so an update never pairs an old page with a new script
    return response


def execute(name: str, job) -> Response:
    """Run `job` (-> list of output files) under the PyMuPDF lock and turn the result into a download."""
    try:
        with lock:
            outputs = job()
    except HTTPException:
        raise
    except (ToolError, ValueError) as e:  # ValueError: bad JSON, failed validation, PyMuPDF argument checks
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        log.exception("%s failed", name)
        raise HTTPException(500, f"{name} failed: {e}") from e
    if not outputs:
        raise HTTPException(400, "Nothing to output")
    if len(outputs) == 1:
        out = outputs[0]
    else:
        buf, seen = io.BytesIO(), set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, u in enumerate(outputs):
                z.writestr(f"{i}_{u.name}" if u.name in seen else u.name, u.data)
                seen.add(u.name)
        out = Upload(f"{name}.zip", buf.getvalue())
    return Response(out.data, media_type=mimetypes.guess_type(out.name)[0] or "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(out.name)}"})


def endpoint_for(t: Tool):
    """Build the POST handler for a tool, with a signature FastAPI can validate and document."""
    def endpoint(**kw):
        def job():
            password = kw.pop("password", "")
            uploads = [read(f) for f in kw.pop("files", [])]
            for name, (kind, _, _) in t.files.items():
                f = kw[name]
                if kind == "files":
                    kw[name] = [read(x) for x in f]
                elif f is None or not f.filename:
                    kw[name] = None
                else:
                    kw[name] = open_pdf(read(f), password) if kind == "pdf" else read(f)
            return run_tool(t, uploads, kw, password)
        return execute(t.id, job)

    P, K = inspect.Parameter, inspect.Parameter.KEYWORD_ONLY
    sig = [P("files", K, annotation=Annotated[list[UploadFile], File(description=f"File(s) to process ({t.accept or 'any type'})")])] if t.input else []
    for name, (ann, default) in t.params.items():
        has_form = any(isinstance(m, FormInfo) for m in getattr(ann, "__metadata__", ()))
        sig.append(P(name, K, annotation=ann if has_form else Annotated[ann, Form()], default=default))
    for name, (kind, required, _) in t.files.items():
        ann = list[UploadFile] if kind == "files" else UploadFile if required else UploadFile | None
        sig.append(P(name, K, annotation=Annotated[ann, File()], default=EMPTY if required else None))
    if "pdf" in t.input or any(kind == "pdf" for kind, _, _ in t.files.values()):
        sig.append(P("password", K, annotation=Annotated[str, Form(description="Password of the uploaded PDF, if it has one")], default=""))
    endpoint.__signature__ = inspect.Signature(sig)
    endpoint.__name__ = t.id.replace("-", "_")
    return endpoint


for _t in TOOLS.values():
    app.post(f"/api/tools/{_t.id}", summary=_t.title, tags=[_t.category], description=inspect.getdoc(_t.fn),
             response_class=Response)(endpoint_for(_t))
DESCRIPTIONS = [t.describe() for t in TOOLS.values()]


class Step(BaseModel):
    tool: str
    params: dict = {}


@app.post("/api/pipeline", tags=["Pipeline"], response_class=Response)
def pipeline(files: list[UploadFile],
             steps: Annotated[str, Form(description='JSON list, e.g. [{"tool": "rotate", "params": {"angle": "90"}}, {"tool": "compress"}]')],
             password: Annotated[str, Form(description="Password of the uploaded PDFs, if any")] = ""):
    """Run tools in sequence: every file a step produces is fed to the next step."""
    def job():
        current = [read(f) for f in files]
        plan = TypeAdapter(Annotated[list[Step], Field(min_length=1, max_length=30)]).validate_json(steps)
        for i, step in enumerate(plan):
            t = TOOLS.get(step.tool)
            if t is None or t.files:
                raise ToolError(f"Step {i + 1}: '{step.tool}' " + ("needs an extra file, so it can't run in a pipeline" if t else "is not a tool"))
            if unknown := step.params.keys() - t.params.keys():
                raise ToolError(f"Step {i + 1} ({t.id}): unknown setting {', '.join(sorted(unknown))}")
            if missing := [n for n, (_, d) in t.params.items() if d is EMPTY and n not in step.params]:
                raise ToolError(f"Step {i + 1} ({t.id}): missing {', '.join(missing)}")
            kw = {n: TypeAdapter(t.params[n][0]).validate_python(v) for n, v in step.params.items()}
            current = run_tool(t, current, kw, password if i == 0 else "")
        return current
    return execute("pipeline", job)


@app.get("/api/tools", tags=["Meta"])
def list_tools():
    """Every tool with its settings, as the UI renders them."""
    return DESCRIPTIONS


@app.get("/api/health", tags=["Meta"])
def health():
    return {"status": "ok", "tools": len(TOOLS)}


@app.exception_handler(db.Down[0])
@app.exception_handler(db.Down[1])
async def database_down(request: Request, e: Exception):
    log.error("accounts database: %s", e)
    return JSONResponse({"detail": f"The accounts database can't be reached: {e}"}, 503)


app.include_router(accounts.router)
STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index():
    """The page, with its script and stylesheet addresses tagged by their content: Cloudflare tells browsers to keep
    .js and .css for hours whatever we say, so a new version must have a new address to be fetched at once."""
    html = (STATIC / "index.html").read_text("utf-8")
    for name in ("app.js", "style.css"):
        tag = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"{name}"', f'"{name}?v={tag}"')
    return HTMLResponse(html)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
