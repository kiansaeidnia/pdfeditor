"""How this server is set up: chosen once in the first-run setup and kept in DATA_DIR/settings.json.

The file holds who may use the app, the sign-up rule, the mail server, and where accounts and files are kept. While
it is missing the app is in setup mode. DATABASE_URL and S3_* environment variables, when set, win over the file.
Sign-in is a signed cookie (or HTTP Basic for scripts), so every worker process agrees on it without shared memory.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

from . import storage
from .db import DB, Conflict, Down

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SESSION_DAYS = 30
RESET_MINUTES = 30
INVITE_DAYS = 7
DEFAULT_QUOTA_MB = 1024
_cache: tuple[Path, float, dict] | None = None  # (file, mtime, settings) last read
_dbs: dict = {}  # one DB object (and one schema check) per database per process
FROM_ENV = "from the environment"  # kept in the file in place of a value the environment supplies
NAMES = {"database": "DATABASE_URL", "storage": "S3_BUCKET and the other S3_ settings"}


def _file() -> Path:
    return DATA_DIR / "settings.json"


def preset() -> dict:
    """What the server's environment fixes, so the setup doesn't ask for it."""
    out = {}
    if url := os.environ.get("DATABASE_URL", ""):
        out["database"] = url
    if bucket := os.environ.get("S3_BUCKET", ""):
        out["storage"] = {"kind": "s3", "bucket": bucket, "endpoint": os.environ.get("S3_ENDPOINT", ""),
                          "region": os.environ.get("S3_REGION", ""), "access_key": os.environ.get("S3_ACCESS_KEY", ""),
                          "secret_key": os.environ.get("S3_SECRET_KEY", "")}
    return out


def current() -> dict | None:
    """The settings in force, or None while the app still needs setting up."""
    global _cache
    path = _file()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    if _cache is None or _cache[:2] != (path, mtime):  # another worker may have saved new settings
        s = json.loads(path.read_text("utf-8"))
        if s.get("login") == "password":  # written by the single-user version: its one account becomes the admin
            try:
                s = _upgrade(s)
            except (*Conflict, *Down):  # another worker is upgrading the same file at this moment: use its result
                time.sleep(1)
                return current()
        _cache = (path, path.stat().st_mtime_ns, s)
    return {**_cache[2], **preset()}


def _upgrade(old: dict) -> dict:
    s = {k: v for k, v in old.items() if k not in ("user", "hash")}
    s.update(login="accounts", signup="invite", quota_mb=DEFAULT_QUOTA_MB, database="", storage={"kind": "local"})
    db = database({**s, **preset()})
    if not db.one("SELECT id FROM users WHERE email = ?", old["user"]):
        db.run("INSERT INTO users (id, email, hash, role, status, created) VALUES (?, ?, ?, 'admin', 'active', ?)",
               new_id(), old["user"], old["hash"], int(time.time()))
    save(s)
    return s


def save(s: dict) -> None:
    """Write atomically and readable by the app only: the file holds the mail and storage passwords."""
    # never copy secrets from the environment; note instead that they come from there, so missing() can tell
    p = preset()
    s = {k: FROM_ENV if k in p and v == p[k] else v for k, v in s.items()}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, _file())


def missing(s: dict) -> str:
    """Why this server can't run, if it was set up with a database or storage from the environment that is gone now.
    Without this it would quietly start over on an empty database."""
    gone = [NAMES[k] for k in NAMES if s.get(k) == FROM_ENV]
    if not gone:
        return ""
    return (f"This server was set up with {' and '.join(gone)} from its environment (.env), and that is not set now. "
            "Put it back and restart the app; your accounts and files are still where they were.")


def database(s: dict) -> DB:
    key = (s.get("database", ""), str(DATA_DIR))
    if key not in _dbs:
        db = DB(key[0], DATA_DIR / "pdfeditor.db")
        db.init()
        _dbs[key] = db
    return _dbs[key]


def store(s: dict):
    st = s.get("storage") or {"kind": "local"}
    if st["kind"] == "s3":
        return storage.S3(st["bucket"], st.get("endpoint", ""), st.get("region", ""), st.get("access_key", ""), st.get("secret_key", ""))
    return storage.Local(DATA_DIR / "files")


def new_id() -> str:
    return uuid.uuid4().hex


# --- first-run setup code -----------------------------------------------------------------------

def setup_code() -> str:
    """The one-time code that proves the person setting up can read the container's log. Shared by all workers."""
    path = DATA_DIR / "setup-code"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # written in full under a private name, then linked into place: a worker starting at the same moment either
        # wins this or reads the other's complete code, never a half-written file
        tmp = DATA_DIR / f"setup-code.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            # letters and digits that can't be mistaken for each other: 60 bits, easy to copy by hand
            f.write("".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12)))
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        tmp.unlink()
    return path.read_text().strip()


def announce_setup() -> None:
    if os.environ.get("AUTH_PASSWORD") or os.environ.get("SMTP_HOST"):
        print("\n  AUTH_PASSWORD and SMTP_* are no longer read: choose the sign-in and mail server in the setup instead.",
              file=sys.stderr, flush=True)
    try:
        code = setup_code()
    except OSError as e:
        sys.exit(f"pdfeditor cannot write its settings to {DATA_DIR} ({e}). Give that folder to the app's user "
                 f"(uid 1000: chown 1000 <folder>), or use the named volume from docker-compose.yml.")
    print(f"\n  pdfeditor is not set up yet. Open it in a browser and enter this setup code:\n\n      {code}\n",
          file=sys.stderr, flush=True)


def finish_setup() -> None:
    (DATA_DIR / "setup-code").unlink(missing_ok=True)


# --- passwords and signed tokens ----------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


_DUMMY = hash_password(secrets.token_hex(8))


def check_password(stored: str | None, password: str) -> bool:
    """stored None (no such user) still costs one scrypt, so the answer's timing doesn't tell who has an account."""
    _, salt, digest = (stored or _DUMMY).split("$")
    ok = hmac.compare_digest(hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1),
                             base64.b64decode(digest))
    return ok and stored is not None


def _sign(secret: str, user: dict, payload: str) -> str:
    # the user's password hash is part of the key, so a new password ends their sessions and reset links
    key = (secret + user["hash"]).encode()
    return base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(secret: str, user: dict, kind: str, seconds: int) -> str:
    payload = f"{kind}|{user['id']}|{int(time.time()) + seconds}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(secret, user, payload)


def token_user(token: str) -> str | None:
    """The user id a token claims to be for; check_token then decides whether to believe it."""
    try:
        body = token.split(".")[0]
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split("|")[1]
    except (ValueError, IndexError):
        return None


def check_token(secret: str, user: dict, kind: str, token: str) -> bool:
    try:
        body, sig = token.split(".")
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        k, uid, expires = payload.split("|")
        expires = int(expires)
    except ValueError:  # also UnicodeDecodeError and binascii.Error, both ValueErrors
        return False
    return (hmac.compare_digest(sig.encode(), _sign(secret, user, payload).encode()) and k == kind
            and uid == user["id"] and expires > time.time())
