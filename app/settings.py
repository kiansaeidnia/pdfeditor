"""Who may use the app and how it sends mail: chosen once in the first-run setup and kept in DATA_DIR/settings.json.

Without that file the AUTH_* and SMTP_* environment variables apply, as before setup existed; with neither the app
is in setup mode. Sign-in uses a signed cookie (or HTTP Basic for scripts), so every worker process agrees on it.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SESSION_DAYS = 30
RESET_MINUTES = 30
_cache: tuple[Path, float, dict] | None = None  # (file, mtime, settings) last read


def _file() -> Path:
    return DATA_DIR / "settings.json"


def _env() -> dict | None:
    password = os.environ.get("AUTH_PASSWORD", "")
    if not password:
        return None
    user = os.environ.get("SMTP_USER", "")
    return {"login": "password", "user": os.environ.get("AUTH_USER", "admin"), "plain": password, "source": "env",
            # sessions are signed with a key only someone who knows the password could derive
            "secret": hashlib.sha256(b"pdfeditor session " + password.encode()).hexdigest(),
            "smtp": {"host": os.environ.get("SMTP_HOST", ""), "port": int(os.environ.get("SMTP_PORT", "587")),
                     "user": user, "password": os.environ.get("SMTP_PASSWORD", ""),
                     "sender": os.environ.get("SMTP_FROM", "") or user},
            "app_url": ""}


def current() -> dict | None:
    """The settings in force, or None while the app still needs setting up."""
    global _cache
    path = _file()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return _env()
    if _cache is None or _cache[:2] != (path, mtime):  # another worker may have saved new settings
        _cache = (path, mtime, json.loads(path.read_text("utf-8")))
    return _cache[2]


def save(s: dict) -> None:
    """Write atomically and readable by the app only: the file holds the SMTP password."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, _file())


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
    try:
        code = setup_code()
    except OSError as e:
        sys.exit(f"pdfeditor cannot write its settings to {DATA_DIR} ({e}). Give that folder to the app's user "
                 f"(uid 1000: chown 1000 <folder>), or use the named volume from docker-compose.yml.")
    print(f"\n  pdfeditor is not set up yet. Open it in a browser and enter this setup code:\n\n      {code}\n",
          file=sys.stderr, flush=True)


def finish_setup() -> None:
    (DATA_DIR / "setup-code").unlink(missing_ok=True)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_login(s: dict, user: str, password: str) -> bool:
    same_user = hmac.compare_digest(user.strip().lower().encode(), s["user"].lower().encode())
    if "plain" in s:
        ok = hmac.compare_digest(password.encode(), s["plain"].encode())
    else:
        _, salt, digest = s["hash"].split("$")
        ok = hmac.compare_digest(hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1),
                                 base64.b64decode(digest))
    return same_user and ok


def _sign(s: dict, payload: str) -> str:
    # the password hash is part of the key, so changing the password ends every session and reset link
    key = (s["secret"] + s.get("hash", s.get("plain", ""))).encode()
    return base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(s: dict, kind: str, seconds: int) -> str:
    payload = f"{kind}|{s['user']}|{int(time.time()) + seconds}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(s, payload)


def check_token(s: dict, kind: str, token: str) -> bool:
    try:
        body, sig = token.split(".")
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        k, user, expires = payload.split("|")
        expires = int(expires)
    except ValueError:  # also UnicodeDecodeError and binascii.Error, both ValueErrors
        return False
    return hmac.compare_digest(sig.encode(), _sign(s, payload).encode()) and k == kind and user == s["user"] and expires > time.time()
