"""FastAPI app: one endpoint per registered tool, a pipeline endpoint, optional login and the web UI."""
import base64
import inspect
import io
import logging
import mimetypes
import os
import secrets
import smtplib
import ssl
import threading
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.params import Form as FormInfo
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter

from . import tools  # noqa: F401  (importing registers the tools)
from .core import EMPTY, TOOLS, Tool, ToolError, Upload, open_pdf, run_tool, safe_name

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
AUTH_USER, AUTH_PASSWORD = os.environ.get("AUTH_USER", "admin"), os.environ.get("AUTH_PASSWORD", "")
SMTP_HOST, SMTP_PORT = os.environ.get("SMTP_HOST", ""), int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER, SMTP_PASSWORD = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
MAIL_MAX_MB = 20  # most mail servers refuse larger messages
# Applied to the UI only: the interactive API docs load their script from a CDN.
CSP = ("default-src 'self'; img-src 'self' data: blob:; frame-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
log = logging.getLogger("pdfeditor")
# shortcut: PyMuPDF is not thread-safe, so one tool runs at a time per process; raise WORKERS for more throughput
lock = threading.Lock()

app = FastAPI(title=os.environ.get("APP_NAME", "pdfeditor"), version="1.0.0", docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    if AUTH_PASSWORD and request.url.path != "/api/health":
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASSWORD}".encode()).decode()
        if not secrets.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="pdfeditor"'})
    size = request.headers.get("content-length", "")
    if size.isdigit() and int(size) > MAX_MB * 2**20:
        return JSONResponse({"detail": f"Upload is larger than {MAX_MB} MB"}, 413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not request.url.path.startswith("/api/"):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so an update never pairs an old page with a new script
    return response


def read(f: UploadFile) -> Upload:
    data = f.file.read(MAX_MB * 2**20 + 1)  # also caps chunked uploads, which carry no Content-Length
    if len(data) > MAX_MB * 2**20:
        raise HTTPException(413, f"{f.filename} is larger than {MAX_MB} MB")
    return Upload(safe_name(Path(f.filename or "file").name), data)


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


def mail_enabled() -> bool:
    """E-mail needs a login password as well as SMTP settings, or anyone who can reach the app could
    send mail from your domain."""
    return bool(SMTP_HOST and SMTP_FROM and AUTH_PASSWORD)


@app.get("/api/config", tags=["Meta"])
def config():
    """What the UI needs to know about this server."""
    return {"name": app.title, "email": mail_enabled(), "max_upload_mb": MAX_MB}


@app.post("/api/email", tags=["Meta"])
def send_email(file: UploadFile,
               to: Annotated[str, Form(pattern=r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$", max_length=254, description="One e-mail address")],
               subject: Annotated[str, Form(max_length=200)] = ""):
    """E-mail a result file as an attachment."""
    if not mail_enabled():
        raise HTTPException(503, "E-mail is not set up on this server (it needs SMTP settings and a login password)")
    upload = read(file)
    if len(upload.data) > MAIL_MAX_MB * 2**20:
        raise HTTPException(413, f"{upload.name} is larger than {MAIL_MAX_MB} MB, too big to e-mail")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject or upload.name
    msg.set_content(f"{upload.name} is attached.\n\nSent from {app.title}.")
    maintype, _, subtype = (mimetypes.guess_type(upload.name)[0] or "application/octet-stream").partition("/")
    msg.add_attachment(upload.data, maintype=maintype, subtype=subtype, filename=upload.name)
    try:
        tls = ssl.create_default_context()
        connect = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=tls) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        with connect as smtp:
            if SMTP_PORT != 465:
                smtp.starttls(context=tls)  # fails on a server without TLS, so the password never travels in the clear
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.warning("mail to %s failed: %s", to, e)
        raise HTTPException(502, f"The mail server did not accept the message: {e}") from e
    return {"sent": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
