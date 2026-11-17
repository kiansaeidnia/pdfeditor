"""First-run setup, sign-in, password reset and e-mail: who may do what, before and after setup."""
import re

import pytest

from app import main, settings
from conftest import make_pdf

PASSWORD = "correct horse"
MAIL = {"smtp_host": "mail.example.com", "smtp_user": "bot@example.com", "smtp_password": "x"}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.tls, self.user = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        assert self.tls, "password sent before TLS"
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append((self, msg))


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A server that has never been set up, with mail going to FakeSMTP and no one-second waits."""
    (tmp_path / "settings.json").unlink()
    monkeypatch.setattr(main.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    FakeSMTP.sent.clear()
    return settings.setup_code()


def setup(client, code, **fields):
    return client.post("/api/setup", data={"code": code, "app_url": "https://pdf.example.com", **fields})


def test_a_new_server_is_closed_until_set_up(client, fresh):
    assert client.get("/api/config").json()["setup"] is True
    assert client.post("/api/tools/rotate", files={"files": ("a.pdf", make_pdf(1))}).status_code == 503
    assert client.get("/api/health").status_code == 200 and client.get("/").status_code == 200
    assert client.post("/api/setup/code", data={"code": "guess"}).status_code == 403
    assert client.post("/api/setup/code", data={"code": fresh}).status_code == 200
    assert setup(client, "guess", login="none").status_code == 403


def test_setup_without_sign_in(client, fresh):
    assert setup(client, fresh, login="none", **MAIL).status_code == 422  # mail without a sign-in would be an open relay
    assert setup(client, fresh, login="none").status_code == 200
    assert client.post("/api/tools/rotate", files={"files": ("a.pdf", make_pdf(1))}).status_code == 200
    assert client.get("/api/config").json()["email"] is False
    assert not (settings.DATA_DIR / "setup-code").exists()
    assert setup(client, fresh, login="none").status_code == 409  # setup runs once


def test_setup_with_sign_in(client, fresh):
    assert setup(client, fresh, login="password", email="me@example.com", password="short").status_code == 422
    assert setup(client, fresh, login="password", email="nobody", password=PASSWORD).status_code == 422
    assert setup(client, fresh, login="password", email="Me@Example.com", password=PASSWORD).status_code == 200
    stored = (settings.DATA_DIR / "settings.json").read_text()
    assert PASSWORD not in stored and '"user": "me@example.com"' in stored
    assert client.get("/api/config").json()["user"] == "me@example.com"  # signed in straight after setup
    assert client.get("/api/tools").status_code == 200

    client.post("/api/logout")
    assert client.get("/api/tools").status_code == 401 and client.get("/api/docs").status_code == 401
    assert client.get("/api/health").status_code == 200  # container health checks need no sign-in
    assert client.post("/api/tools/rotate", headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert client.get("/").status_code == 200  # the sign-in page itself loads
    assert client.post("/api/login", data={"email": "me@example.com", "password": "wrong pass"}).status_code == 401
    r = client.post("/api/login", data={"email": "ME@example.com", "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/tools").status_code == 200

    client.cookies.clear()
    assert client.get("/api/tools", auth=("me@example.com", PASSWORD)).status_code == 200  # scripts use HTTP Basic
    assert client.get("/api/tools", auth=("me@example.com", "wrong pass")).status_code == 401
    client.cookies.set(main.COOKIE, "forged.token")
    assert client.get("/api/tools").status_code == 401


def test_setup_with_mail_and_password_reset(client, fresh):
    r = client.post("/api/setup/test-email", data={"code": fresh, "to": "me@example.com", **MAIL})
    assert r.status_code == 200 and FakeSMTP.sent[-1][1]["To"] == "me@example.com"
    assert setup(client, fresh, login="password", email="me@example.com", password=PASSWORD, **MAIL).status_code == 200
    assert client.get("/api/config").json()["email"] is True and client.get("/api/config").json()["reset"] is True

    # e-mailing a result
    assert client.post("/api/email", files={"file": ("out.pdf", make_pdf(1))}, data={"to": "a@example.com", "subject": "Hi"}).json() == {"sent": True}
    smtp, msg = FakeSMTP.sent[-1]
    assert (smtp.host, smtp.port, smtp.tls, smtp.user) == ("mail.example.com", 587, True, "bot@example.com")
    assert (msg["To"], msg["From"], msg["Subject"]) == ("a@example.com", "bot@example.com", "Hi")
    assert next(msg.iter_attachments()).get_filename() == "out.pdf"
    for bad in ("a@example.com, b@example.com", "not-an-address", "a@example.com\nBcc: x@example.com"):
        assert client.post("/api/email", files={"file": ("o.pdf", b"x")}, data={"to": bad}).status_code == 422, bad

    # a forgotten password
    client.post("/api/logout")
    sent = len(FakeSMTP.sent)
    assert client.post("/api/forgot", data={"email": "stranger@example.com"}).json() == {"sent": True}
    assert len(FakeSMTP.sent) == sent  # same answer, but nothing is sent to an unknown address
    assert client.post("/api/forgot", data={"email": "me@example.com"}).status_code == 200
    body = FakeSMTP.sent[-1][1].get_content()
    token = re.search(r"https://pdf\.example\.com/#/reset/(\S+)", body).group(1)
    assert client.post("/api/reset", data={"token": token + "x", "password": "a new password"}).status_code == 400
    assert client.post("/api/reset", data={"token": token, "password": "a new password"}).status_code == 200
    assert client.get("/api/tools").status_code == 200  # signed in by the reset
    assert client.post("/api/reset", data={"token": token, "password": "another one!"}).status_code == 400  # used up
    client.cookies.clear()
    assert client.post("/api/login", data={"email": "me@example.com", "password": PASSWORD}).status_code == 401
    assert client.post("/api/login", data={"email": "me@example.com", "password": "a new password"}).status_code == 200


def test_a_password_change_ends_old_sessions(client, fresh):
    setup(client, fresh, login="password", email="me@example.com", password=PASSWORD)
    old = client.cookies.get(main.COOKIE)
    settings.save({**settings.current(), "hash": settings.hash_password("changed!!")})
    client.cookies.set(main.COOKIE, old)
    assert client.get("/api/tools").status_code == 401


def test_environment_variables_still_configure_a_server(client, fresh, monkeypatch):
    monkeypatch.setenv("AUTH_PASSWORD", "s3cret-env")
    assert client.get("/api/config").json()["setup"] is False
    assert client.get("/api/config").json()["user_label"] == "User name"
    assert client.get("/api/tools").status_code == 401
    assert client.get("/api/tools", auth=("admin", "s3cret-env")).status_code == 200
    assert client.post("/api/login", data={"email": "admin", "password": "s3cret-env"}).status_code == 200
    assert client.get("/api/tools").status_code == 200
    assert setup(client, fresh, login="none").status_code == 409


def test_mail_needs_a_sign_in(client, monkeypatch):
    settings.save({**settings.current(), "smtp": {"host": "mail.example.com", "port": 587, "user": "", "password": "", "sender": "a@example.com"}})
    assert client.get("/api/config").json()["email"] is False
    assert client.post("/api/email", files={"file": ("o.pdf", b"x")}, data={"to": "a@example.com"}).status_code == 503


def test_tokens():
    s = {"user": "me@example.com", "secret": "k", "hash": "h1"}
    t = settings.make_token(s, "session", 60)
    assert settings.check_token(s, "session", t)
    assert not settings.check_token(s, "reset", t)  # a session is not a reset link
    assert not settings.check_token({**s, "hash": "h2"}, "session", t)
    assert not settings.check_token(s, "session", settings.make_token(s, "session", -1))  # expired
    for junk in ("", ".", "x.y", "a.b.c", "!!!.???", t.split(".")[0] + ".é"):
        assert not settings.check_token(s, "session", junk)
