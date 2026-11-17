"""FastAPI app: one endpoint per registered tool, a pipeline endpoint, optional login and the web UI."""
import base64
import inspect
import io
import logging
import mimetypes
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.params import Form as FormInfo
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, TypeAdapter

from . import settings, tools  # noqa: F401  (importing tools registers them)
from .core import EMPTY, TOOLS, Tool, ToolError, Upload, open_pdf, run_tool, safe_name

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
MAIL_MAX_MB = 20  # most mail servers refuse larger messages
COOKIE = "pdfeditor_session"
# reachable without signing in: what the sign-in and setup pages themselves need
OPEN = {"/api/health", "/api/config", "/api/setup", "/api/setup/code", "/api/setup/test-email", "/api/login", "/api/logout",
        "/api/forgot", "/api/reset"}
EMAIL = r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$"
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


def signed_in(request: Request, s: dict) -> bool:
    """A session cookie from the sign-in page, or HTTP Basic credentials from a script."""
    if settings.check_token(s, "session", request.cookies.get(COOKIE, "")):
        return True
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(value).decode().partition(":")
    except ValueError:
        return False
    if settings.check_login(s, user, password):
        return True
    time.sleep(1)  # as slow to guess here as on the sign-in page
    return False


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # a form on another site, even a sibling subdomain the cookie trusts, can't make the browser post here;
    # scripts send no Sec-Fetch-Site header and are not affected
    if request.method not in ("GET", "HEAD") and request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return JSONResponse({"detail": "Cross-site request refused."}, 403)
    if path.startswith("/api/") and path not in OPEN:
        s = settings.current()
        if s is None:
            return JSONResponse({"detail": "Finish setting up pdfeditor first: open it in a browser."}, 503)
        # the UI and its static files load without signing in, so the sign-in page can show; they hold no data
        if s["login"] == "password" and not await run_in_threadpool(signed_in, request, s):  # scrypt is slow on purpose
            return JSONResponse({"detail": "Sign in first."}, 401)
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


def mail_enabled(s: dict | None) -> bool:
    """E-mail needs a sign-in as well as SMTP settings, or anyone who can reach the app could send mail from
    your domain."""
    return bool(s and s["login"] == "password" and s["smtp"]["host"] and s["smtp"]["sender"])


def send_mail(smtp: dict, msg: EmailMessage) -> None:
    msg["From"] = smtp["sender"]
    try:
        tls = ssl.create_default_context()
        host, port = smtp["host"], int(smtp["port"])
        connect = smtplib.SMTP_SSL(host, port, timeout=30, context=tls) if port == 465 else smtplib.SMTP(host, port, timeout=30)
        with connect as server:
            if port != 465:
                server.starttls(context=tls)  # fails on a server without TLS, so the password never travels in the clear
            if smtp["user"]:
                server.login(smtp["user"], smtp["password"])
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.warning("mail to %s failed: %s", msg["To"], e)
        raise HTTPException(502, f"The mail server did not accept the message: {e}") from e


def with_session(body: dict, request: Request, s: dict) -> JSONResponse:
    r = JSONResponse(body)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    # Strict: the browser never sends it with a request started by another site, which is what stops forged posts
    r.set_cookie(COOKIE, settings.make_token(s, "session", settings.SESSION_DAYS * 86400), max_age=settings.SESSION_DAYS * 86400,
                 httponly=True, samesite="strict", secure=secure, path="/")
    return r


@app.get("/api/config", tags=["Meta"])
def config(request: Request):
    """What the UI needs to know about this server, and who is signed in."""
    s = settings.current()
    login = s and s["login"]
    return {"name": app.title, "max_upload_mb": MAX_MB, "setup": s is None, "login": login,
            "user": s["user"] if login == "password" and signed_in(request, s) else None,
            "user_label": "User name" if s and s.get("source") == "env" else "E-mail",
            "email": mail_enabled(s), "reset": mail_enabled(s) and s.get("source") != "env"}


Email = Annotated[str, Form(pattern=EMAIL, max_length=254, description="One e-mail address")]
NewPassword = Annotated[str, Form(min_length=8, max_length=200)]


@app.post("/api/email", tags=["Meta"])
def send_email(file: UploadFile, to: Email, subject: Annotated[str, Form(max_length=200)] = ""):
    """E-mail a result file as an attachment."""
    s = settings.current()
    if not mail_enabled(s):
        raise HTTPException(503, "E-mail is not set up on this server (it needs SMTP settings and a sign-in)")
    upload = read(file)
    if len(upload.data) > MAIL_MAX_MB * 2**20:
        raise HTTPException(413, f"{upload.name} is larger than {MAIL_MAX_MB} MB, too big to e-mail")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject or upload.name
    msg.set_content(f"{upload.name} is attached.\n\nSent from {app.title}.")
    maintype, _, subtype = (mimetypes.guess_type(upload.name)[0] or "application/octet-stream").partition("/")
    msg.add_attachment(upload.data, maintype=maintype, subtype=subtype, filename=upload.name)
    send_mail(s["smtp"], msg)
    return {"sent": True}


# --- first-run setup and sign-in ----------------------------------------------------------------

def smtp_form(smtp_host: Annotated[str, Form(max_length=253)] = "", smtp_port: Annotated[int, Form(ge=1, le=65535)] = 587,
              smtp_user: Annotated[str, Form(max_length=254)] = "", smtp_password: Annotated[str, Form(max_length=500)] = "",
              smtp_from: Annotated[str, Form(max_length=254)] = "") -> dict:
    sender = smtp_from or (smtp_user if "@" in smtp_user else "")
    if smtp_host and not re.match(EMAIL, sender):
        raise HTTPException(422, "Give the address mail is sent from")
    return {"host": smtp_host.strip(), "port": smtp_port, "user": smtp_user, "password": smtp_password, "sender": sender}


def check_setup_code(code: str) -> None:
    if settings.current() is not None:
        raise HTTPException(409, "This server is already set up.")
    if not secrets.compare_digest(code.strip().encode(), settings.setup_code().encode()):
        time.sleep(1)
        raise HTTPException(403, "That setup code is not right. It is in the container's log: docker compose logs pdfeditor")


@app.post("/api/setup/code", tags=["Setup"])
def setup_code_check(code: Annotated[str, Form(max_length=100)]):
    """During setup: check the code from the log before the rest is filled in."""
    check_setup_code(code)
    return {"ok": True}


@app.post("/api/setup", tags=["Setup"])
def setup(request: Request, code: Annotated[str, Form(max_length=100)], login: Annotated[Literal["none", "password"], Form()],
          smtp: Annotated[dict, Depends(smtp_form)], email: Annotated[str, Form(max_length=254)] = "",
          password: Annotated[str, Form(max_length=200)] = "", app_url: Annotated[str, Form(max_length=500)] = ""):
    """First run only: choose who may use the app, and optionally a mail server. Needs the code from the log."""
    check_setup_code(code)
    if login == "password":
        if not re.match(EMAIL, email):
            raise HTTPException(422, "Give a valid e-mail address to sign in with")
        if len(password) < 8:
            raise HTTPException(422, "The password needs at least 8 characters")
    elif smtp["host"]:
        raise HTTPException(422, "E-mail needs a sign-in, or anyone who can open the app could send mail as you")
    if app_url and not re.match(r"^https?://[^\s/]+$", app_url):
        raise HTTPException(422, "The app's address should look like https://pdf.example.com")
    s = {"login": login, "user": email.strip().lower() if login == "password" else "",
         "hash": settings.hash_password(password) if login == "password" else "",
         "secret": secrets.token_hex(32), "smtp": smtp, "app_url": app_url}
    settings.save(s)
    settings.finish_setup()
    log.info("setup finished: login=%s, e-mail %s", login, "on" if smtp["host"] else "off")
    return with_session({"done": True}, request, s) if login == "password" else {"done": True}


@app.post("/api/setup/test-email", tags=["Setup"])
def setup_test_email(code: Annotated[str, Form(max_length=100)], to: Email, smtp: Annotated[dict, Depends(smtp_form)]):
    """During setup: send a test message with the mail settings being entered."""
    check_setup_code(code)
    if not smtp["host"]:
        raise HTTPException(422, "Give the mail server first")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, f"{app.title}: test e-mail"
    msg.set_content(f"The mail settings work: {app.title} can send e-mail.")
    send_mail(smtp, msg)
    return {"sent": True}


@app.post("/api/login", tags=["Setup"])
def login(request: Request, email: Annotated[str, Form(max_length=254)], password: Annotated[str, Form(max_length=200)]):
    s = settings.current()
    if s is None or s["login"] != "password":
        raise HTTPException(400, "This server has no sign-in.")
    if not settings.check_login(s, email, password):
        time.sleep(1)  # shortcut: slows guessing per request only; add per-address lockout if this faces the internet
        raise HTTPException(401, "Wrong e-mail or password.")
    return with_session({"user": s["user"]}, request, s)


@app.post("/api/logout", tags=["Setup"])
def logout():
    r = JSONResponse({"done": True})
    r.delete_cookie(COOKIE, path="/")
    return r


@app.post("/api/forgot", tags=["Setup"])
def forgot(email: Email):
    """E-mail a link that sets a new password. The answer is the same whether or not the address is known."""
    s = settings.current()
    if not (s and mail_enabled(s) and s.get("source") != "env"):
        raise HTTPException(400, "Password reset needs e-mail to be set up.")
    if email.strip().lower() == s["user"] and s["app_url"]:
        msg = EmailMessage()
        msg["To"], msg["Subject"] = s["user"], f"{app.title}: set a new password"
        link = f"{s['app_url']}/#/reset/{settings.make_token(s, 'reset', settings.RESET_MINUTES * 60)}"
        msg.set_content(f"To set a new password for {app.title}, open this link within {settings.RESET_MINUTES} minutes:\n\n"
                        f"{link}\n\nIf you did not ask for this, ignore this message.")
        # sent in the background, errors only logged: the answer must not show whether the address is known
        threading.Thread(target=send_quietly, args=(s["smtp"], msg), daemon=True).start()
    return {"sent": True}


def send_quietly(smtp: dict, msg: EmailMessage) -> None:
    try:
        send_mail(smtp, msg)
    except HTTPException:
        pass  # send_mail has logged it


@app.post("/api/reset", tags=["Setup"])
def reset(request: Request, token: Annotated[str, Form(max_length=500)], password: NewPassword):
    s = settings.current()
    if not (s and s["login"] == "password" and s.get("source") != "env" and settings.check_token(s, "reset", token)):
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    s = {**s, "hash": settings.hash_password(password)}  # a new hash also ends every older session and link
    settings.save(s)
    return with_session({"user": s["user"]}, request, s)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
