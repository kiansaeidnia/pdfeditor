"""The HTTP layer: listing, validation, batches, pipelines, limits and login."""
import json

import pymupdf

from app import main
from app.core import TOOLS
from conftest import make_pdf, pdf, texts, unzip


def test_tool_list_and_docs(client):
    listed = client.get("/api/tools").json()
    assert len(listed) == len(TOOLS) > 55 and all(t["title"] and t["description"] for t in listed)
    rotate = next(t for t in listed if t["id"] == "rotate")
    assert rotate["params"][0]["enum"] == ["90", "180", "270"] and rotate["input"] == "pdf"
    assert "/api/tools/rotate" in client.get("/api/openapi.json").json()["paths"]
    assert client.get("/api/health").json()["status"] == "ok"
    assert "<title>" in client.get("/").text


def test_bad_settings_and_bad_files_are_rejected(run):
    assert run("rotate", make_pdf(1), angle=45, expect=422).json()["detail"][0]["loc"][-1] == "angle"
    run("pdf-to-images", make_pdf(1), dpi=5000, expect=422)
    run("watermark", make_pdf(1), color="red", expect=422)
    run("rotate", expect=422)
    assert "not a readable PDF" in run("rotate", ("x.pdf", b"hello"), expect=400).json()["detail"]
    assert "outside this document" in run("organize", make_pdf(2), pages="5", expect=400).json()["detail"]


def test_several_files_are_processed_as_a_batch(run):
    parts = unzip(run("rotate", ("a.pdf", make_pdf(1)), ("b.pdf", make_pdf(2)), ("a.pdf", make_pdf(3))))
    assert sorted(parts) == ["2_a_rotate.pdf", "a_rotate.pdf", "b_rotate.pdf"]
    assert pymupdf.open("pdf", parts["b_rotate.pdf"])[0].rotation == 90


def test_download_names_cannot_escape_a_folder(run):
    name = run("rotate", ("../../evil dir/../x.pdf", make_pdf(1))).headers["content-disposition"]
    assert "/" not in name.split("''")[1] and "%2F" not in name and "%5C" not in name


def test_pipeline_chains_tools(client):
    steps = [{"tool": "rotate", "params": {"angle": "180", "pages": "1"}},
             {"tool": "organize", "params": {"pages": "reverse"}},
             {"tool": "page-numbers"},
             {"tool": "split", "params": {"after": "1"}}]
    r = client.post("/api/pipeline", files=[("files", ("in.pdf", make_pdf(3)))], data={"steps": json.dumps(steps)})
    assert r.status_code == 200, r.text
    parts = unzip(r)
    assert len(parts) == 2
    first = pymupdf.open("pdf", next(v for k, v in parts.items() if k.endswith("_1.pdf")))
    assert texts(first) == ["Page 3 1"]


def test_pipeline_rejects_bad_plans(client):
    def post(steps):
        r = client.post("/api/pipeline", files=[("files", ("in.pdf", make_pdf(2)))], data={"steps": steps})
        return r.status_code, r.json()["detail"]
    assert post('[{"tool": "nope"}]') == (400, "Step 1: 'nope' is not a tool")
    assert post('[{"tool": "overlay"}]')[0] == 400
    assert "unknown setting" in post('[{"tool": "rotate", "params": {"speed": 3}}]')[1]
    assert "missing pages" in post('[{"tool": "organize"}]')[1]
    assert post('[{"tool": "rotate", "params": {"angle": "45"}}]')[0] == 400
    assert post("[]")[0] == 400 and post("{broken")[0] == 400


def test_upload_limit(run, monkeypatch):
    monkeypatch.setattr(main, "MAX_MB", 0)
    run("rotate", make_pdf(1), expect=413)


def test_login_is_enforced_when_a_password_is_set(client, monkeypatch):
    monkeypatch.setattr(main, "AUTH_PASSWORD", "s3cret")
    assert client.get("/api/tools").status_code == 401
    assert client.get("/").headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/tools", auth=("admin", "wrong")).status_code == 401
    assert client.get("/api/tools", auth=("admin", "s3cret")).status_code == 200
    assert client.get("/api/health").status_code == 200  # container health checks need no login


def test_ui_gets_security_headers_and_config(client):
    page = client.get("/")
    assert "script-src 'self'" in page.headers["content-security-policy"] and page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-cache"
    assert "content-security-policy" not in client.get("/api/docs").headers  # the docs page loads a CDN script
    assert client.get("/api/config").json() == {"name": "pdfeditor", "email": False, "max_upload_mb": 200}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
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


def test_email_sends_the_result_as_an_attachment(client, monkeypatch):
    def send(**data):
        return client.post("/api/email", files={"file": ("out.pdf", make_pdf(1))}, data=data, auth=("admin", "pw"))

    monkeypatch.setattr(main, "AUTH_PASSWORD", "pw")
    assert send(to="a@example.com").status_code == 503  # no SMTP settings yet
    for name, value in {"SMTP_HOST": "mail.example.com", "SMTP_USER": "bot", "SMTP_PASSWORD": "x", "SMTP_FROM": "bot@example.com"}.items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(main.smtplib, "SMTP", FakeSMTP)
    assert client.get("/api/config", auth=("admin", "pw")).json()["email"] is True
    assert send(to="a@example.com", subject="Your file").json() == {"sent": True}
    smtp, msg = FakeSMTP.sent[-1]
    assert (smtp.host, smtp.port, smtp.tls, smtp.user) == ("mail.example.com", 587, True, "bot")
    assert (msg["To"], msg["From"], msg["Subject"]) == ("a@example.com", "bot@example.com", "Your file")
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "out.pdf" and attachment.get_content_type() == "application/pdf"
    for bad in ("a@example.com, b@example.com", "not-an-address", "a@example.com\nBcc: x@example.com"):
        assert send(to=bad).status_code == 422, bad

    monkeypatch.setattr(main, "AUTH_PASSWORD", "")  # without a login the app must not relay mail for anyone who can reach it
    assert client.post("/api/email", files={"file": ("out.pdf", b"x")}, data={"to": "a@example.com"}).status_code == 503
    assert len(FakeSMTP.sent) == 1
