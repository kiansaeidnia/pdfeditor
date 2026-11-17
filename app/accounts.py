"""First-run setup, accounts (sign-up, sign-in, password reset, admin) and each user's file library.

Mounted by main.py. The guard there decides who reaches what; these endpoints find the signed-in user, if any,
in request.state.user.
"""
import base64
import hashlib
import logging
import mimetypes
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import db as dbm, settings, storage
from .core import MAX_MB, read, safe_name

COOKIE = "pdfeditor_session"
MAIL_MAX_MB = 20  # most mail servers refuse larger messages
EMAIL = r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$"
# reachable without signing in: what the setup, sign-in and sign-up pages themselves need
OPEN = {"/api/health", "/api/config", "/api/setup", "/api/setup/code", "/api/setup/test-email", "/api/setup/test-database",
        "/api/setup/test-storage", "/api/login", "/api/logout", "/api/signup", "/api/forgot", "/api/reset"}
USER = "id, email, hash, role, status, created"
log = logging.getLogger("pdfeditor")
router = APIRouter()

Email = Annotated[str, Form(pattern=EMAIL, max_length=254, description="One e-mail address")]
NewPassword = Annotated[str, Form(min_length=8, max_length=200)]
Code = Annotated[str, Form(max_length=100, description="The setup code from the container's log")]


def now() -> int:
    return int(time.time())


def app_name(request: Request) -> str:
    return request.app.title


# --- who is asking ------------------------------------------------------------------------------

def signed_in(request: Request, s: dict) -> dict | None:
    """The active user behind a session cookie from the sign-in page, or behind HTTP Basic credentials from a script."""
    db = settings.database(s)
    token = request.cookies.get(COOKIE, "")
    if token and (uid := settings.token_user(token)):
        user = db.one(f"SELECT {USER} FROM users WHERE id = ?", uid)
        if user and user["status"] == "active" and settings.check_token(s["secret"], user, "session", token):
            return user
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        email, _, password = base64.b64decode(value).decode().partition(":")
    except ValueError:
        return None
    user = db.one(f"SELECT {USER} FROM users WHERE email = ?", email.strip().lower())
    if settings.check_password(user and user["hash"], password) and user["status"] == "active":
        return user
    time.sleep(1)  # as slow to guess here as on the sign-in page
    return None


def me(request: Request) -> dict:
    """The signed-in user, for endpoints that only make sense with accounts."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(400, "This needs accounts: this server was set up without sign-in.")
    return user


def with_session(body: dict, request: Request, s: dict, user: dict) -> JSONResponse:
    r = JSONResponse(body)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(COOKIE, settings.make_token(s["secret"], user, "session", settings.SESSION_DAYS * 86400),
                 max_age=settings.SESSION_DAYS * 86400, httponly=True, samesite="strict", secure=secure, path="/")
    return r


@router.get("/api/config", tags=["Meta"])
def config(request: Request):
    """What the UI needs to know about this server, and who is signed in."""
    s = settings.current()
    out = {"name": app_name(request), "max_upload_mb": MAX_MB, "setup": s is None, "login": s and s["login"],
           "user": None, "email": mail_enabled(s), "signup": None}
    if s is None:
        p = settings.preset()  # names only: a database URL carries its password
        out["preset"] = {"database": "database" in p, "storage": p.get("storage", {}).get("bucket")}
    elif s["login"] == "accounts":
        out["signup"] = s["signup"]
        try:
            user = signed_in(request, s)
        except dbm.Down as e:
            raise HTTPException(503, f"The accounts database can't be reached: {e}") from e
        if user:
            out["user"] = {"email": user["email"], "role": user["role"]}
            if user["role"] == "admin":
                out["quota_mb"] = s["quota_mb"]
    return out


# --- e-mail -------------------------------------------------------------------------------------

def mail_enabled(s: dict | None) -> bool:
    """E-mail needs accounts as well as a mail server, or anyone who can reach the app could send mail as you."""
    return bool(s and s["login"] == "accounts" and s["smtp"]["host"] and s["smtp"]["sender"])


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


def mail_later(s: dict, to: str, subject: str, text: str) -> None:
    """Send in the background and only log failures: the answer must not depend on the mail server."""
    if not mail_enabled(s):
        return
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(text)

    def go():
        try:
            send_mail(s["smtp"], msg)
        except HTTPException:
            pass  # send_mail has logged it
    in_background(go)


def in_background(job) -> None:
    threading.Thread(target=job, daemon=True).start()


@router.post("/api/email", tags=["Meta"])
def send_email(request: Request, file: UploadFile, to: Email, subject: Annotated[str, Form(max_length=200)] = ""):
    """E-mail a result file as an attachment."""
    s = settings.current()
    if not mail_enabled(s):
        raise HTTPException(503, "E-mail is not set up on this server (it needs a mail server and accounts)")
    user = me(request)
    if s["signup"] == "open" and user["role"] != "admin" and to.strip().lower() != user["email"]:
        raise HTTPException(403, "On this server you can e-mail results only to your own address.")
    upload = read(file)
    if len(upload.data) > MAIL_MAX_MB * 2**20:
        raise HTTPException(413, f"{upload.name} is larger than {MAIL_MAX_MB} MB, too big to e-mail")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject or upload.name
    msg.set_content(f"{upload.name} is attached.\n\nSent by {user['email']} from {app_name(request)}.")
    maintype, _, subtype = (mimetypes.guess_type(upload.name)[0] or "application/octet-stream").partition("/")
    msg.add_attachment(upload.data, maintype=maintype, subtype=subtype, filename=upload.name)
    send_mail(s["smtp"], msg)
    return {"sent": True}


# --- first-run setup ----------------------------------------------------------------------------

def smtp_form(smtp_host: Annotated[str, Form(max_length=253)] = "", smtp_port: Annotated[int, Form(ge=1, le=65535)] = 587,
              smtp_user: Annotated[str, Form(max_length=254)] = "", smtp_password: Annotated[str, Form(max_length=500)] = "",
              smtp_from: Annotated[str, Form(max_length=254)] = "") -> dict:
    sender = smtp_from or (smtp_user if "@" in smtp_user else "")
    if smtp_host and not re.match(EMAIL, sender):
        raise HTTPException(422, "Give the address mail is sent from")
    return {"host": smtp_host.strip(), "port": smtp_port, "user": smtp_user, "password": smtp_password, "sender": sender}


def storage_form(storage: Annotated[Literal["local", "s3"], Form()] = "local", s3_bucket: Annotated[str, Form(max_length=255)] = "",
                 s3_endpoint: Annotated[str, Form(max_length=500)] = "", s3_region: Annotated[str, Form(max_length=50)] = "",
                 s3_access_key: Annotated[str, Form(max_length=500)] = "", s3_secret_key: Annotated[str, Form(max_length=500)] = "") -> dict:
    if "storage" in settings.preset():
        return settings.preset()["storage"]
    if storage == "local":
        return {"kind": "local"}
    if not s3_bucket.strip():
        raise HTTPException(422, "Give the bucket's name")
    if s3_endpoint and not re.match(r"^https?://[^\s/]+/?$", s3_endpoint.strip()):
        raise HTTPException(422, "The S3 endpoint should look like https://s3.example.com")
    return {"kind": "s3", "bucket": s3_bucket.strip(), "endpoint": s3_endpoint.strip().rstrip("/"), "region": s3_region.strip(),
            "access_key": s3_access_key.strip(), "secret_key": s3_secret_key}


def database_form(database: Annotated[str, Form(max_length=1000)] = "") -> str:
    if "database" in settings.preset():
        return settings.preset()["database"]
    database = database.strip()
    if database and not dbm.is_postgres(database):
        raise HTTPException(422, "A Postgres address starts with postgresql://")
    return database


def open_database(url: str) -> dbm.DB:
    try:
        return settings.database({"database": url})
    except Exception as e:  # any driver error: bad address, refused, wrong password, no such database
        raise HTTPException(422, f"Can't use that database: {e}") from e


def open_storage(st: dict):
    try:
        store = settings.store({"storage": st})  # boto3 checks the region here
        storage.check(store)
    except storage.Failure as e:
        raise HTTPException(422, f"Can't use that storage: {e}") from e
    return store


def check_setup_code(code: str) -> None:
    if settings.current() is not None:
        raise HTTPException(409, "This server is already set up.")
    if not secrets.compare_digest(code.strip().upper().encode(), settings.setup_code().encode()):
        time.sleep(1)
        raise HTTPException(403, "That setup code is not right. It is in the container's log: docker compose logs pdfeditor")


@router.post("/api/setup/code", tags=["Setup"])
def setup_code_check(code: Code):
    """During setup: check the code from the log before the rest is filled in."""
    check_setup_code(code)
    return {"ok": True}


@router.post("/api/setup/test-database", tags=["Setup"])
def setup_test_database(code: Code, database: Annotated[str, Depends(database_form)]):
    """During setup: connect to the database being entered and create the tables it needs."""
    check_setup_code(code)
    open_database(database)
    return {"ok": True}


@router.post("/api/setup/test-storage", tags=["Setup"])
def setup_test_storage(code: Code, st: Annotated[dict, Depends(storage_form)]):
    """During setup: write, read and delete a small test file in the storage being entered."""
    check_setup_code(code)
    open_storage(st)
    return {"ok": True}


@router.post("/api/setup/test-email", tags=["Setup"])
def setup_test_email(request: Request, code: Code, to: Email, smtp: Annotated[dict, Depends(smtp_form)]):
    """During setup: send a test message with the mail settings being entered."""
    check_setup_code(code)
    if not smtp["host"]:
        raise HTTPException(422, "Give the mail server first")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, f"{app_name(request)}: test e-mail"
    msg.set_content(f"The mail settings work: {app_name(request)} can send e-mail.")
    send_mail(smtp, msg)
    return {"sent": True}


@router.post("/api/setup", tags=["Setup"])
def setup(request: Request, code: Code, login: Annotated[Literal["none", "accounts"], Form()],
          smtp: Annotated[dict, Depends(smtp_form)], st: Annotated[dict, Depends(storage_form)],
          database: Annotated[str, Depends(database_form)], email: Annotated[str, Form(max_length=254)] = "",
          password: Annotated[str, Form(max_length=200)] = "", signup: Annotated[Literal["open", "approval", "invite"], Form()] = "invite",
          app_url: Annotated[str, Form(max_length=500)] = ""):
    """First run only: who may use the app, the first admin account, the sign-up rule, where accounts and files are
    kept, and optionally a mail server. Needs the code from the log."""
    check_setup_code(code)
    if app_url and not re.match(r"^https?://[^\s/]+$", app_url):
        raise HTTPException(422, "The app's address should look like https://pdf.example.com")
    s = {"login": login, "secret": secrets.token_hex(32), "smtp": smtp, "app_url": app_url}
    if login == "none":
        if smtp["host"]:
            raise HTTPException(422, "E-mail needs accounts, or anyone who can open the app could send mail as you")
    else:
        if not re.match(EMAIL, email):
            raise HTTPException(422, "Give a valid e-mail address for the admin account")
        if len(password) < 8:
            raise HTTPException(422, "The password needs at least 8 characters")
        s.update(signup=signup, quota_mb=settings.DEFAULT_QUOTA_MB, database=database, storage=st)
        db, _ = open_database(database), open_storage(st)
        email = email.strip().lower()
        admin = {"id": settings.new_id(), "email": email, "hash": settings.hash_password(password)}
        # a database kept from an earlier install may know this address already: it becomes the admin again
        if db.run("UPDATE users SET hash = ?, role = 'admin', status = 'active' WHERE email = ?", admin["hash"], email):
            admin["id"] = db.one("SELECT id FROM users WHERE email = ?", email)["id"]
        else:
            db.run("INSERT INTO users (id, email, hash, role, status, created) VALUES (?, ?, ?, 'admin', 'active', ?)",
                   admin["id"], email, admin["hash"], now())
    settings.save(s)
    settings.finish_setup()
    log.info("setup finished: login=%s, e-mail %s", login, "on" if smtp["host"] else "off")
    return with_session({"done": True}, request, s, admin) if login == "accounts" else {"done": True}


# --- signing in and up --------------------------------------------------------------------------

def accounts_on() -> dict:
    s = settings.current()
    if s is None or s["login"] != "accounts":
        raise HTTPException(400, "This server has no accounts.")
    return s


@router.post("/api/login", tags=["Accounts"])
def login(request: Request, email: Annotated[str, Form(max_length=254)], password: Annotated[str, Form(max_length=200)]):
    s = accounts_on()
    user = settings.database(s).one(f"SELECT {USER} FROM users WHERE email = ?", email.strip().lower())
    if not settings.check_password(user and user["hash"], password):
        time.sleep(1)  # shortcut: slows guessing per request only; add per-address lockout if this faces the internet
        raise HTTPException(401, "Wrong e-mail or password.")
    if user["status"] == "pending":
        raise HTTPException(403, "Your account is waiting for an admin to approve it.")
    if user["status"] != "active":
        raise HTTPException(403, "This account is turned off. Ask an admin.")
    return with_session({"email": user["email"]}, request, s, user)


@router.post("/api/logout", tags=["Accounts"])
def logout():
    r = JSONResponse({"done": True})
    r.delete_cookie(COOKIE, path="/")
    return r


def hashed(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()  # invites are stored hashed: a copy of the database can't use them


@router.post("/api/signup", tags=["Accounts"])
def signup(request: Request, email: Email, password: NewPassword, invite: Annotated[str, Form(max_length=200)] = ""):
    """Create an account. Whether it works straight away depends on the sign-up rule the admin chose; an invite
    link always works."""
    s = accounts_on()
    db, email = settings.database(s), email.strip().lower()
    if invite:
        # claimed by deleting it: of two sign-ups at the same moment only one deletes the row
        if not db.run("DELETE FROM invites WHERE token = ? AND expires >= ? AND email IN ('', ?)", hashed(invite), now(), email):
            raise HTTPException(400, "This invite link has expired, was already used, or is for another address.")
        status = "active"
    elif s["signup"] == "invite":
        raise HTTPException(403, "Accounts here are by invitation only. Ask an admin for an invite link.")
    else:
        status = "active" if s["signup"] == "open" else "pending"
    user = {"id": settings.new_id(), "email": email, "hash": settings.hash_password(password)}
    try:
        db.run("INSERT INTO users (id, email, hash, role, status, created) VALUES (?, ?, ?, 'user', ?, ?)",
               user["id"], email, user["hash"], status, now())
    except dbm.Conflict:
        if status == "pending":  # same answer as a new address, so asking doesn't tell who has an account
            return {"pending": True}
        raise HTTPException(409, "There is already an account for this address. Sign in, or reset the password.") from None
    if status == "pending":
        # one message until an admin deals with the queue, so a script can't flood the admins' inboxes
        if db.one("SELECT COUNT(*) AS n FROM users WHERE status = 'pending'")["n"] > 1:
            return {"pending": True}
        for admin in db.all("SELECT email FROM users WHERE role = 'admin' AND status = 'active'"):
            mail_later(s, admin["email"], f"{app_name(request)}: {email} wants an account",
                       f"{email} signed up and is waiting for approval.\n\nApprove or refuse it on the admin page: {s['app_url']}/#/admin")
        return {"pending": True}
    return with_session({"email": email}, request, s, user)


@router.post("/api/forgot", tags=["Accounts"])
def forgot(request: Request, email: Email):
    """E-mail a link that sets a new password. The answer is the same whether or not the address has an account."""
    s = accounts_on()
    if not mail_enabled(s):
        raise HTTPException(400, "Password reset needs e-mail to be set up. Ask an admin.")
    user = settings.database(s).one(f"SELECT {USER} FROM users WHERE email = ?", email.strip().lower())
    if user and user["status"] == "active" and s["app_url"]:
        link = f"{s['app_url']}/#/reset/{settings.make_token(s['secret'], user, 'reset', settings.RESET_MINUTES * 60)}"
        mail_later(s, user["email"], f"{app_name(request)}: set a new password",
                   f"To set a new password for {app_name(request)}, open this link within {settings.RESET_MINUTES} minutes:\n\n"
                   f"{link}\n\nIf you did not ask for this, ignore this message.")
    return {"sent": True}


def set_password(s: dict, user: dict, password: str) -> dict:
    user = {**user, "hash": settings.hash_password(password)}  # a new hash also ends every older session and link
    settings.database(s).run("UPDATE users SET hash = ? WHERE id = ?", user["hash"], user["id"])
    return user


@router.post("/api/reset", tags=["Accounts"])
def reset(request: Request, token: Annotated[str, Form(max_length=500)], password: NewPassword):
    s = accounts_on()
    uid = settings.token_user(token)
    user = uid and settings.database(s).one(f"SELECT {USER} FROM users WHERE id = ?", uid)
    if not (user and user["status"] == "active" and settings.check_token(s["secret"], user, "reset", token)):
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    return with_session({"email": user["email"]}, request, s, set_password(s, user, password))


@router.post("/api/account/password", tags=["Accounts"])
def change_password(request: Request, current: Annotated[str, Form(max_length=200)], password: NewPassword):
    """Change your own password. Every other browser signed in as you is signed out."""
    s, user = accounts_on(), me(request)
    if not settings.check_password(user["hash"], current):
        time.sleep(1)
        raise HTTPException(403, "Your current password is not right.")
    return with_session({"done": True}, request, s, set_password(s, user, password))


# --- admin --------------------------------------------------------------------------------------
# main.py's guard lets only admins reach /api/admin/

@router.get("/api/admin/users", tags=["Admin"])
def admin_users():
    s = accounts_on()
    return settings.database(s).all(
        "SELECT u.id, u.email, u.role, u.status, u.created, COUNT(f.id) AS files, COALESCE(SUM(f.size), 0) AS bytes "
        "FROM users u LEFT JOIN files f ON f.user_id = u.id GROUP BY u.id, u.email, u.role, u.status, u.created ORDER BY u.created")


@router.post("/api/admin/users/{uid}", tags=["Admin"])
def admin_user(request: Request, uid: str,
               action: Annotated[Literal["approve", "disable", "enable", "make_admin", "make_user", "delete"], Form()]):
    """Approve, turn off or on, promote, demote or delete an account. Deleting also deletes the account's files."""
    s, admin = accounts_on(), me(request)
    db = settings.database(s)
    user = db.one(f"SELECT {USER} FROM users WHERE id = ?", uid)
    if not user:
        raise HTTPException(404, "No such account.")
    if uid == admin["id"] and action in ("disable", "make_user", "delete"):
        raise HTTPException(400, "You can't do that to your own account: ask another admin.")  # so one admin always remains
    if action == "delete":
        store = settings.store(s)
        for f in db.all("SELECT id FROM files WHERE user_id = ?", uid):
            try:
                store.delete(f"{uid}/{f['id']}")
            except storage.Failure as e:
                raise HTTPException(502, f"The file store did not delete {user['email']}'s files, so the account is kept: {e}") from e
            db.run("DELETE FROM files WHERE id = ?", f["id"])
        db.run("DELETE FROM users WHERE id = ?", uid)
        return {"done": True}
    if action in ("approve", "enable"):
        db.run("UPDATE users SET status = 'active' WHERE id = ?", uid)
        if user["status"] == "pending":
            mail_later(s, user["email"], f"{app_name(request)}: your account is ready",
                       f"An admin approved your account. Sign in at {s['app_url']}/")
    elif action == "disable":
        db.run("UPDATE users SET status = 'disabled' WHERE id = ?", uid)
    else:
        db.run("UPDATE users SET role = ? WHERE id = ?", "admin" if action == "make_admin" else "user", uid)
    return {"done": True}


@router.post("/api/admin/invites", tags=["Admin"])
def admin_invite(request: Request, email: Annotated[str, Form(max_length=254)] = ""):
    """Make a one-time sign-up link, valid for a week. With an address it works only for that address, and is
    e-mailed to it when e-mail is set up."""
    s = accounts_on()
    email = email.strip().lower()
    if email and not re.match(EMAIL, email):
        raise HTTPException(422, "That is not a valid e-mail address.")
    token = secrets.token_urlsafe(24)
    settings.database(s).run("INSERT INTO invites (token, email, expires) VALUES (?, ?, ?)",
                             hashed(token), email, now() + settings.INVITE_DAYS * 86400)
    link = f"{s['app_url'] or str(request.base_url).rstrip('/')}/#/signup/{token}"
    sent = bool(email and mail_enabled(s))
    if sent:
        mail_later(s, email, f"You're invited to {app_name(request)}",
                   f"{me(request)['email']} invited you to {app_name(request)}. Create your account within "
                   f"{settings.INVITE_DAYS} days:\n\n{link}")
    return {"link": link, "sent": sent}


@router.post("/api/admin/settings", tags=["Admin"])
def admin_settings(signup: Annotated[Literal["open", "approval", "invite"], Form()],
                   quota_mb: Annotated[int, Form(ge=0, le=10_000_000, description="0 = no limit")]):
    s = accounts_on()
    settings.save({**s, "signup": signup, "quota_mb": quota_mb})
    return {"done": True}


# --- each user's files --------------------------------------------------------------------------

def file_row(s: dict, user: dict, fid: str) -> dict:
    row = settings.database(s).one("SELECT id, name, size, type, created FROM files WHERE id = ? AND user_id = ?", fid, user["id"])
    if not row:
        raise HTTPException(404, "No such file.")
    return row


@router.get("/api/files", tags=["My files"])
def list_files(request: Request):
    s, user = accounts_on(), me(request)
    rows = settings.database(s).all("SELECT id, name, size, type, created FROM files WHERE user_id = ? ORDER BY created DESC, name",
                                    user["id"])
    return {"files": rows, "used": sum(r["size"] for r in rows), "quota_mb": s["quota_mb"]}


@router.post("/api/files", tags=["My files"])
def add_files(request: Request, files: list[UploadFile]):
    """Keep files in your library."""
    s, user = accounts_on(), me(request)
    uploads = [read(f) for f in files]
    db, store = settings.database(s), settings.store(s)
    rows = [(settings.new_id(), u.name, len(u.data), mimetypes.guess_type(u.name)[0] or "application/octet-stream", now())
            for u in uploads]
    # the space is reserved first, under a lock on the account, then the bytes are stored
    if not db.reserve(user["id"], rows, s["quota_mb"] * 2**20):
        raise HTTPException(413, f"Not enough room: your files may use {s['quota_mb']} MB. Delete some to make space.")
    stored = []
    try:
        for (fid, *_), u in zip(rows, uploads):
            store.put(f"{user['id']}/{fid}", u.data)
            stored.append(fid)
    except Exception as e:  # nothing half-kept: the reserved rows and any stored bytes go
        for fid, *_ in rows:
            db.run("DELETE FROM files WHERE id = ?", fid)
        for fid in stored:
            try:
                store.delete(f"{user['id']}/{fid}")
            except storage.Failure:
                log.warning("could not remove %s/%s from the file store", user["id"], fid)
        if isinstance(e, storage.Failure):
            log.exception("storing a file failed")
            raise HTTPException(502, f"The file store did not take {u.name}: {e}") from e
        raise
    return {"added": [fid for fid, *_ in rows]}


@router.get("/api/files/{fid}", tags=["My files"])
def get_file(request: Request, fid: str):
    s, user = accounts_on(), me(request)
    row = file_row(s, user, fid)
    try:
        data = settings.store(s).get(f"{user['id']}/{fid}")
    except storage.Failure as e:
        raise HTTPException(502, f"The file store did not return {row['name']}: {e}") from e
    return Response(data, media_type=row["type"],
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['name'])}"})


@router.post("/api/files/{fid}/rename", tags=["My files"])
def rename_file(request: Request, fid: str, name: Annotated[str, Form(min_length=1, max_length=200)]):
    s, user = accounts_on(), me(request)
    file_row(s, user, fid)
    settings.database(s).run("UPDATE files SET name = ? WHERE id = ?", safe_name(name), fid)
    return {"done": True}


@router.delete("/api/files/{fid}", tags=["My files"])
def delete_file(request: Request, fid: str):
    s, user = accounts_on(), me(request)
    row = file_row(s, user, fid)
    try:
        settings.store(s).delete(f"{user['id']}/{fid}")  # first: if it fails, the file is still listed and can be retried
    except storage.Failure as e:
        raise HTTPException(502, f"The file store did not delete {row['name']}: {e}") from e
    settings.database(s).run("DELETE FROM files WHERE id = ?", fid)
    return {"done": True}
