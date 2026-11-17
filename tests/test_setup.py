"""First-run setup, accounts, sign-up rules, admin, password reset, e-mail and each user's files.

The account and file tests run on SQLite with files on disk, and also on Postgres with files in S3 when
TEST_DATABASE_URL and TEST_S3_ENDPOINT point at test servers (CI starts both; REQUIRE_ALL makes them mandatory).
"""
import json
import os
import re
import uuid

import boto3
import psycopg
import pytest

from app import accounts, db as dbm, main, settings
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
    monkeypatch.setattr(accounts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(accounts, "in_background", lambda job: job())  # mail is sent at once, so tests can look at it
    monkeypatch.setattr(accounts.time, "sleep", lambda s: None)
    FakeSMTP.sent.clear()
    return settings.setup_code()


def backends():
    yield pytest.param({}, id="sqlite-disk")
    pg, s3 = os.environ.get("TEST_DATABASE_URL"), os.environ.get("TEST_S3_ENDPOINT")
    if os.environ.get("REQUIRE_ALL") and not (pg and s3):
        raise RuntimeError("REQUIRE_ALL is set but TEST_DATABASE_URL or TEST_S3_ENDPOINT is missing")
    marks = pytest.mark.skipif(not (pg and s3), reason="needs TEST_DATABASE_URL and TEST_S3_ENDPOINT")
    yield pytest.param({"database": pg, "storage": "s3", "s3_endpoint": s3, "s3_access_key": "test", "s3_secret_key": "test"},
                       id="postgres-s3", marks=marks)


@pytest.fixture(params=list(backends()))
def where(request, fresh):
    """Setup fields choosing the database and file store, emptied for this test."""
    fields = dict(request.param)
    if "database" in fields:
        with psycopg.connect(fields["database"]) as c:
            c.execute("DROP TABLE IF EXISTS users, invites, files")
        fields["s3_bucket"] = f"t{uuid.uuid4().hex[:20]}"
        boto3.client("s3", endpoint_url=fields["s3_endpoint"], region_name="us-east-1", aws_access_key_id="test",
                     aws_secret_access_key="test").create_bucket(Bucket=fields["s3_bucket"])
    settings._dbs.clear()
    return fields


def setup(client, code, **fields):
    return client.post("/api/setup", data={"code": code, "app_url": "https://pdf.example.com", **fields})


def setup_accounts(client, code, signup="invite", **fields):
    r = setup(client, code, login="accounts", email="Admin@Example.com", password=PASSWORD, signup=signup, **fields)
    assert r.status_code == 200, r.text
    return r


def sign_in(client, email, password=PASSWORD):
    client.cookies.clear()
    return client.post("/api/login", data={"email": email, "password": password})


def stored_objects(fields):
    """Every object in the file store, as keys."""
    if fields.get("storage") == "s3":
        s3 = boto3.client("s3", endpoint_url=fields["s3_endpoint"], region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        return {o["Key"] for o in s3.list_objects_v2(Bucket=fields["s3_bucket"]).get("Contents", [])}
    root = settings.DATA_DIR / "files"
    return {str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()} if root.exists() else set()


# --- setup --------------------------------------------------------------------------------------

def test_a_new_server_is_closed_until_set_up(client, fresh):
    assert client.get("/api/config").json()["setup"] is True
    assert client.post("/api/tools/rotate", files={"files": ("a.pdf", make_pdf(1))}).status_code == 503
    assert client.get("/api/health").status_code == 200 and client.get("/").status_code == 200
    assert client.post("/api/setup/code", data={"code": "guess"}).status_code == 403
    assert client.post("/api/setup/code", data={"code": fresh.lower()}).status_code == 200  # forgiving about case
    assert setup(client, "guess", login="none").status_code == 403
    assert re.fullmatch(r"[A-Z2-9]{12}", fresh)


def test_setup_without_sign_in(client, fresh):
    assert setup(client, fresh, login="none", **MAIL).status_code == 422  # mail without accounts would be an open relay
    assert setup(client, fresh, login="none").status_code == 200
    assert client.post("/api/tools/rotate", files={"files": ("a.pdf", make_pdf(1))}).status_code == 200
    config = client.get("/api/config").json()
    assert config["login"] == "none" and config["email"] is False
    assert client.get("/api/files").status_code == 400  # no accounts, so no libraries
    assert not (settings.DATA_DIR / "setup-code").exists()
    assert setup(client, fresh, login="none").status_code == 409  # setup runs once


def test_setup_checks_its_inputs(client, fresh):
    assert setup(client, fresh, login="accounts", email="me@example.com", password="short").status_code == 422
    assert setup(client, fresh, login="accounts", email="nobody", password=PASSWORD).status_code == 422
    bad_db = {"code": fresh, "database": "postgresql://nobody:x@127.0.0.1:1/none"}
    assert "Can't use that database" in client.post("/api/setup/test-database", data=bad_db).json()["detail"]
    assert client.post("/api/setup/test-database", data={"code": fresh, "database": "mysql://x"}).status_code == 422
    assert client.post("/api/setup/test-database", data={"code": fresh}).status_code == 200  # SQLite
    bad_s3 = {"code": fresh, "storage": "s3", "s3_bucket": "b", "s3_endpoint": "http://127.0.0.1:1"}
    assert "Can't use that storage" in client.post("/api/setup/test-storage", data=bad_s3).json()["detail"]
    assert client.post("/api/setup/test-storage", data={"code": fresh}).status_code == 200  # this server's disk
    assert client.get("/api/config").json()["setup"] is True  # nothing was saved by the checks


def test_setup_with_accounts(client, where):
    setup_accounts(client, settings.setup_code(), **where)
    assert client.get("/api/config").json()["user"] == {"email": "admin@example.com", "role": "admin"}
    assert client.get("/api/tools").status_code == 200
    stored = (settings.DATA_DIR / "settings.json").read_text()
    assert PASSWORD not in stored

    client.post("/api/logout")
    assert client.get("/api/tools").status_code == 401 and client.get("/api/docs").status_code == 401
    assert client.get("/api/health").status_code == 200  # container health checks need no sign-in
    assert client.post("/api/tools/rotate", headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert sign_in(client, "admin@example.com", "wrong pass").status_code == 401
    r = sign_in(client, "ADMIN@example.com")
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/api/tools").status_code == 200

    client.cookies.clear()
    assert client.get("/api/tools", auth=("admin@example.com", PASSWORD)).status_code == 200  # scripts use HTTP Basic
    assert client.get("/api/tools", auth=("admin@example.com", "wrong pass")).status_code == 401
    client.cookies.set(accounts.COOKIE, "forged.token")
    assert client.get("/api/tools").status_code == 401


# --- sign-up rules and admin --------------------------------------------------------------------

def test_open_sign_up(client, where):
    setup_accounts(client, settings.setup_code(), signup="open", **where)
    client.cookies.clear()
    assert client.post("/api/signup", data={"email": "ann@example.com", "password": PASSWORD}).status_code == 200
    assert client.get("/api/config").json()["user"] == {"email": "ann@example.com", "role": "user"}
    assert client.get("/api/admin/users").status_code == 403  # not an admin
    client.cookies.clear()
    assert client.post("/api/signup", data={"email": "ANN@example.com", "password": PASSWORD}).status_code == 409


def test_sign_up_with_approval(client, where):
    setup_accounts(client, settings.setup_code(), signup="approval", **MAIL, **where)
    admin = client.cookies.get(accounts.COOKIE)
    client.cookies.clear()
    assert client.post("/api/signup", data={"email": "bob@example.com", "password": PASSWORD}).json() == {"pending": True}
    assert FakeSMTP.sent[-1][1]["To"] == "admin@example.com"  # the admin hears about it
    assert sign_in(client, "bob@example.com").status_code == 403
    assert "waiting" in sign_in(client, "bob@example.com").json()["detail"]

    client.cookies.clear()
    client.cookies.set(accounts.COOKIE, admin)
    bob = next(u for u in client.get("/api/admin/users").json() if u["email"] == "bob@example.com")
    assert bob["status"] == "pending"
    assert client.post(f"/api/admin/users/{bob['id']}", data={"action": "approve"}).status_code == 200
    assert FakeSMTP.sent[-1][1]["To"] == "bob@example.com"
    assert sign_in(client, "bob@example.com").status_code == 200


def test_sign_up_by_invitation(client, where):
    setup_accounts(client, settings.setup_code(), **MAIL, **where)
    admin = client.cookies.get(accounts.COOKIE)
    anyone = client.post("/api/admin/invites").json()
    assert anyone["sent"] is False and "/#/signup/" in anyone["link"]
    for_cat = client.post("/api/admin/invites", data={"email": "cat@example.com"}).json()
    assert for_cat["sent"] is True and FakeSMTP.sent[-1][1]["To"] == "cat@example.com"
    client.cookies.clear()

    assert client.post("/api/signup", data={"email": "dan@example.com", "password": PASSWORD}).status_code == 403
    token = for_cat["link"].rsplit("/", 1)[1]
    assert client.post("/api/signup", data={"email": "dan@example.com", "password": PASSWORD, "invite": token}).status_code == 400
    assert client.post("/api/signup", data={"email": "cat@example.com", "password": PASSWORD, "invite": token}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/signup", data={"email": "cat2@example.com", "password": PASSWORD, "invite": token}).status_code == 400  # used
    open_token = anyone["link"].rsplit("/", 1)[1]
    assert client.post("/api/signup", data={"email": "dan@example.com", "password": PASSWORD, "invite": open_token}).status_code == 200
    stored = json.dumps(settings.database(settings.current()).all("SELECT * FROM invites"))
    assert token not in stored and open_token not in stored  # kept hashed

    client.cookies.set(accounts.COOKIE, admin)
    assert client.post("/api/admin/settings", data={"signup": "open", "quota_mb": 5}).status_code == 200
    assert client.get("/api/config").json()["signup"] == "open"


def test_admin_powers(client, where):
    setup_accounts(client, settings.setup_code(), signup="open", **where)
    admin = client.cookies.get(accounts.COOKIE)
    me = client.get("/api/admin/users").json()[0]
    for action in ("disable", "make_user", "delete"):
        assert client.post(f"/api/admin/users/{me['id']}", data={"action": action}).status_code == 400  # one admin always remains

    client.cookies.clear()
    client.post("/api/signup", data={"email": "eve@example.com", "password": PASSWORD})
    eve = client.cookies.get(accounts.COOKIE)
    client.post("/api/files", files=[("files", ("a.pdf", make_pdf(1)))])
    client.cookies.set(accounts.COOKIE, admin)
    eve_id = next(u["id"] for u in client.get("/api/admin/users").json() if u["email"] == "eve@example.com")
    assert next(u for u in client.get("/api/admin/users").json() if u["id"] == eve_id)["files"] == 1

    client.post(f"/api/admin/users/{eve_id}", data={"action": "disable"})
    client.cookies.set(accounts.COOKIE, eve)
    assert client.get("/api/files").status_code == 401  # a turned-off account is signed out at once
    assert "turned off" in sign_in(client, "eve@example.com").json()["detail"]

    client.cookies.set(accounts.COOKIE, admin)
    client.post(f"/api/admin/users/{eve_id}", data={"action": "enable"})
    client.post(f"/api/admin/users/{eve_id}", data={"action": "make_admin"})
    assert sign_in(client, "eve@example.com").status_code == 200
    assert client.get("/api/config").json()["user"]["role"] == "admin"
    client.cookies.set(accounts.COOKIE, admin)
    assert stored_objects(where)
    assert client.post(f"/api/admin/users/{eve_id}", data={"action": "delete"}).status_code == 200
    assert stored_objects(where) == set()  # her files went with her
    assert sign_in(client, "eve@example.com").status_code == 401


# --- files --------------------------------------------------------------------------------------

def test_my_files(client, where):
    setup_accounts(client, settings.setup_code(), signup="open", **where)
    pdf = make_pdf(2)
    r = client.post("/api/files", files=[("files", ("../report.pdf", pdf)), ("files", ("notes.txt", b"hello"))])
    assert r.status_code == 200 and len(r.json()["added"]) == 2
    listed = client.get("/api/files").json()
    assert listed["used"] == len(pdf) + 5 and listed["quota_mb"] == settings.DEFAULT_QUOTA_MB
    report = next(f for f in listed["files"] if f["name"] == "report.pdf")  # no folder in a stored name
    got = client.get(f"/api/files/{report['id']}")
    assert got.content == pdf and got.headers["content-type"] == "application/pdf" and "report.pdf" in got.headers["content-disposition"]

    assert client.post(f"/api/files/{report['id']}/rename", data={"name": "q3/final.pdf"}).status_code == 200
    assert {f["name"] for f in client.get("/api/files").json()["files"]} == {"q3_final.pdf", "notes.txt"}

    # someone else can't see, fetch, rename or delete it
    admin = client.cookies.get(accounts.COOKIE)
    client.cookies.clear()
    client.post("/api/signup", data={"email": "zed@example.com", "password": PASSWORD})
    assert client.get("/api/files").json()["files"] == []
    assert client.get(f"/api/files/{report['id']}").status_code == 404
    assert client.delete(f"/api/files/{report['id']}").status_code == 404
    assert client.post(f"/api/files/{report['id']}/rename", data={"name": "x"}).status_code == 404

    client.cookies.set(accounts.COOKIE, admin)
    assert client.delete(f"/api/files/{report['id']}").status_code == 200
    assert len(stored_objects(where)) == 1
    assert client.get(f"/api/files/{report['id']}").status_code == 404


def test_storage_quota(client, fresh):
    setup_accounts(client, fresh)
    settings.save({**settings.current(), "quota_mb": 1})
    assert client.post("/api/files", files=[("files", ("a.bin", b"x" * 600_000))]).status_code == 200
    r = client.post("/api/files", files=[("files", ("b.bin", b"x" * 600_000))])
    assert r.status_code == 413 and "Not enough room" in r.json()["detail"]
    assert len(client.get("/api/files").json()["files"]) == 1


# --- passwords and e-mail -----------------------------------------------------------------------

def test_passwords_and_reset(client, fresh):
    r = client.post("/api/setup/test-email", data={"code": fresh, "to": "me@example.com", **MAIL})
    assert r.status_code == 200 and FakeSMTP.sent[-1][1]["To"] == "me@example.com"
    setup_accounts(client, fresh, **MAIL)
    config = client.get("/api/config").json()
    assert config["email"] is True

    # e-mailing a result
    assert client.post("/api/email", files={"file": ("out.pdf", make_pdf(1))}, data={"to": "a@example.com", "subject": "Hi"}).json() == {"sent": True}
    smtp, msg = FakeSMTP.sent[-1]
    assert (smtp.host, smtp.port, smtp.tls, smtp.user) == ("mail.example.com", 587, True, "bot@example.com")
    assert (msg["To"], msg["From"], msg["Subject"]) == ("a@example.com", "bot@example.com", "Hi")
    assert next(msg.iter_attachments()).get_filename() == "out.pdf"
    for bad in ("a@example.com, b@example.com", "not-an-address", "a@example.com\nBcc: x@example.com"):
        assert client.post("/api/email", files={"file": ("o.pdf", b"x")}, data={"to": bad}).status_code == 422, bad

    # changing it
    old = client.cookies.get(accounts.COOKIE)
    assert client.post("/api/account/password", data={"current": "wrong pass", "password": "a new password"}).status_code == 403
    assert client.post("/api/account/password", data={"current": PASSWORD, "password": "a new password"}).status_code == 200
    assert client.get("/api/files").status_code == 200  # this browser stays signed in
    client.cookies.set(accounts.COOKIE, old)
    assert client.get("/api/files").status_code == 401  # other sessions end

    # forgetting it
    client.cookies.clear()
    sent = len(FakeSMTP.sent)
    assert client.post("/api/forgot", data={"email": "stranger@example.com"}).json() == {"sent": True}
    assert len(FakeSMTP.sent) == sent  # same answer, but nothing is sent to an unknown address
    assert client.post("/api/forgot", data={"email": "admin@example.com"}).status_code == 200
    token = re.search(r"https://pdf\.example\.com/#/reset/(\S+)", FakeSMTP.sent[-1][1].get_content()).group(1)
    assert client.post("/api/reset", data={"token": token + "x", "password": "third password"}).status_code == 400
    assert client.post("/api/reset", data={"token": token, "password": "third password"}).status_code == 200
    assert client.get("/api/files").status_code == 200  # signed in by the reset
    assert client.post("/api/reset", data={"token": token, "password": "fourth password"}).status_code == 400  # used up
    assert sign_in(client, "admin@example.com", "a new password").status_code == 401
    assert sign_in(client, "admin@example.com", "third password").status_code == 200


def test_mail_needs_accounts(client):
    settings.save({**settings.current(), "smtp": {"host": "mail.example.com", "port": 587, "user": "", "password": "", "sender": "a@example.com"}})
    assert client.get("/api/config").json()["email"] is False
    assert client.post("/api/email", files={"file": ("o.pdf", b"x")}, data={"to": "a@example.com"}).status_code == 503


def test_single_user_settings_are_upgraded(client, fresh):
    """Settings written by the version before accounts: the one sign-in becomes the admin."""
    old = {"login": "password", "user": "old@example.com", "hash": settings.hash_password(PASSWORD), "secret": "s",
           "smtp": {"host": "", "port": 587, "user": "", "password": "", "sender": ""}, "app_url": ""}
    (settings.DATA_DIR / "settings.json").write_text(json.dumps(old))
    assert sign_in(client, "old@example.com").status_code == 200
    assert client.get("/api/config").json()["user"] == {"email": "old@example.com", "role": "admin"}
    assert json.loads((settings.DATA_DIR / "settings.json").read_text())["login"] == "accounts"


def test_environment_presets(client, fresh, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@db/x")
    monkeypatch.setenv("S3_BUCKET", "files")
    assert client.get("/api/config").json()["preset"] == {"database": True, "storage": "files"}  # no password shown
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("S3_BUCKET")


def test_tokens():
    user = {"id": "u1", "hash": "h1"}
    t = settings.make_token("k", user, "session", 60)
    assert settings.token_user(t) == "u1" and settings.check_token("k", user, "session", t)
    assert not settings.check_token("k", user, "reset", t)  # a session is not a reset link
    assert not settings.check_token("k", {**user, "hash": "h2"}, "session", t)  # a new password ends it
    assert not settings.check_token("k", {**user, "id": "u2"}, "session", t)
    assert not settings.check_token("other", user, "session", t)
    assert not settings.check_token("k", user, "session", settings.make_token("k", user, "session", -1))  # expired
    for junk in ("", ".", "x.y", "a.b.c", "!!!.???", t.split(".")[0] + ".é"):
        assert not settings.check_token("k", user, "session", junk)
        settings.token_user(junk)  # never raises


def at_once(n, call):
    """Run call(i) for i in range(n) at the same moment, each with its own client; returns the status codes."""
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    clients = [TestClient(main.app) for _ in range(n)]
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda i: call(clients[i], i).status_code, range(n)))


def test_an_invite_works_once_even_when_used_at_the_same_moment(client, where):
    setup_accounts(client, settings.setup_code(), **where)
    token = client.post("/api/admin/invites").json()["link"].rsplit("/", 1)[1]
    codes = at_once(6, lambda c, i: c.post("/api/signup", data={"email": f"u{i}@example.com", "password": PASSWORD, "invite": token}))
    assert sorted(codes) == [200, 400, 400, 400, 400, 400]


def test_uploads_at_the_same_moment_stay_within_the_quota(client, where):
    setup_accounts(client, settings.setup_code(), **where)
    settings.save({**settings.current(), "quota_mb": 1})
    cookie = client.cookies.get(accounts.COOKIE)

    def upload(c, i):
        c.cookies.set(accounts.COOKIE, cookie)
        return c.post("/api/files", files=[("files", (f"{i}.bin", b"x" * 300_000))])
    codes = at_once(8, upload)
    assert sorted(codes) == [200] * 3 + [413] * 5  # exactly as many as fit, however they interleave
    listed = client.get("/api/files").json()
    assert listed["used"] <= 2**20 and len(listed["files"]) == codes.count(200)
    assert len(stored_objects(where)) == codes.count(200)  # refused uploads left nothing in the store


def test_open_sign_up_can_only_mail_itself(client, fresh):
    setup_accounts(client, fresh, signup="open", **MAIL)
    client.cookies.clear()
    client.post("/api/signup", data={"email": "new@example.com", "password": PASSWORD})
    pdf = {"file": ("o.pdf", make_pdf(1))}
    assert client.post("/api/email", files=pdf, data={"to": "ceo@example.com"}).status_code == 403
    assert client.post("/api/email", files=pdf, data={"to": "new@example.com"}).status_code == 200


def test_approval_sign_up_hides_existing_accounts_and_mails_admins_once(client, fresh):
    setup_accounts(client, fresh, signup="approval", **MAIL)
    client.cookies.clear()
    sent = len(FakeSMTP.sent)
    for email in ("p1@example.com", "p2@example.com", "admin@example.com"):  # the last one exists
        assert client.post("/api/signup", data={"email": email, "password": PASSWORD}).json() == {"pending": True}
    assert len(FakeSMTP.sent) == sent + 1  # one note to the admin for the queue, not one per sign-up


def test_a_database_that_is_down_gives_a_clear_error(client, fresh):
    setup_accounts(client, fresh)
    settings.save({**settings.current(), "database": "postgresql://nobody:x@127.0.0.1:1/none"})
    for r in (client.get("/api/tools"), client.get("/api/config")):
        assert r.status_code == 503 and "database can't be reached" in r.json()["detail"]


def test_storage_failures_keep_the_file_listed(client, fresh, monkeypatch):
    setup_accounts(client, fresh)
    client.post("/api/files", files=[("files", ("a.pdf", make_pdf(1)))])
    fid = client.get("/api/files").json()["files"][0]["id"]

    def broken(self, key):
        raise OSError("disk unplugged")
    monkeypatch.setattr(accounts.storage.Local, "delete", broken)
    r = client.delete(f"/api/files/{fid}")
    assert r.status_code == 502 and "disk unplugged" in r.json()["detail"]
    assert [f["id"] for f in client.get("/api/files").json()["files"]] == [fid]  # still there, so it can be retried
    monkeypatch.setattr(accounts.storage.Local, "put", lambda self, key, data: broken(self, key))
    assert client.post("/api/files", files=[("files", ("b.pdf", make_pdf(1)))]).status_code == 502
    assert len(client.get("/api/files").json()["files"]) == 1


def test_a_bad_s3_region_is_a_clear_setup_error(client, fresh):
    r = client.post("/api/setup/test-storage", data={"code": fresh, "storage": "s3", "s3_bucket": "b", "s3_region": "us east 1"})
    assert r.status_code == 422 and "Can't use that storage" in r.json()["detail"]


def test_sql_placeholders_are_portable():
    assert dbm.DB("postgresql://x", None)._sql("SELECT ? WHERE a = ?") == "SELECT %s WHERE a = %s"
