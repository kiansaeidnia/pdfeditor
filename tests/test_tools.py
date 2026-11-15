"""Each tool, called through the HTTP API the way the UI calls it."""
import datetime
import io
import json
import xml.etree.ElementTree as ET

import pymupdf
import pytest
from PIL import Image

from app.core import EMPTY, TOOLS
from conftest import make_pdf, needs, pdf, rich_pdf, texts, unzip

RICH = rich_pdf()


def png(color="black", size=(80, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_p12(password: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(b"test", key, cert, None, serialization.BestAvailableEncryption(password))


# --- every simple tool runs on a realistic file ---------------------------------------------------

EXTERNAL = {"pdf-to-office", "ocr", "pdf-to-pdfa"}  # need LibreOffice / OCRmyPDF, tested separately
SIMPLE = [t.id for t in TOOLS.values() if t.input == "pdf" and not t.files and t.id not in EXTERNAL | {"protect"}
          and all(default is not EMPTY for _, default in t.params.values())]
NOTHING_TO_FIND = {"split-chapters", "pdf-to-csv", "extract-images", "extract-attachments"}  # in a plain text PDF


@pytest.mark.parametrize("tool", SIMPLE)
def test_tool_runs_with_default_settings(run, tool):
    r = run(tool, RICH)
    assert r.content
    if r.headers["content-type"] == "application/pdf":
        assert len(pdf(r)) >= 1


@pytest.mark.parametrize("tool", SIMPLE)
def test_tool_runs_on_rotated_password_protected_input(run, tool):
    locked = run("protect", make_pdf(2, rotation=90), user_password="pw").content
    run(tool, locked, password="pw", expect=400 if tool in NOTHING_TO_FIND else 200)


# --- pages ----------------------------------------------------------------------------------------

def test_merge_appends_in_upload_order(run):
    out = pdf(run("merge", make_pdf(2, "A{n}"), make_pdf(3, "B{n}")))
    assert texts(out) == ["A1", "A2", "B1", "B2", "B3"]


def test_merge_interleaves_fronts_and_reversed_backs(run):
    out = pdf(run("merge", make_pdf(3, "front{n}"), make_pdf(3, "back{n}"), mode="interleave", reverse_last=True))
    assert texts(out) == ["front1", "back3", "front2", "back2", "front3", "back1"]


def test_split_after_pages_and_into_single_pages(run):
    parts = unzip(run("split", make_pdf(5), after="2"))
    assert sorted(parts) == ["doc1_1-2.pdf", "doc1_3-5.pdf"]
    assert texts(pymupdf.open("pdf", parts["doc1_3-5.pdf"])) == ["Page 3", "Page 4", "Page 5"]
    assert len(unzip(run("split", make_pdf(3)))) == 3


@pytest.mark.parametrize("mode, value, sizes", [("pages-per-file", 4, [4, 4, 1]), ("number-of-files", 4, [2, 2, 2, 3]),
                                                ("number-of-files", 50, [1] * 9)])
def test_split_by_count(run, mode, value, sizes):
    parts = unzip(run("split-by", make_pdf(9), mode=mode, value=value))
    assert sorted(len(pymupdf.open("pdf", p)) for p in parts.values()) == sorted(sizes)


def test_split_by_size_keeps_every_page_and_respects_the_limit(run):
    doc = pymupdf.open()
    for n in range(6):  # ~60 kB of incompressible picture per page
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 150, 150))
        pix.set_rect(pix.irect, (n * 40, 10, 10))
        Image.effect_noise((150, 150), 80).save(buf := io.BytesIO(), "PNG")
        doc.new_page().insert_image(pymupdf.Rect(0, 0, 300, 300), stream=buf.getvalue())
    parts = unzip(run("split-by", doc.tobytes(), mode="max-megabytes", value=0.05))
    assert sum(len(pymupdf.open("pdf", p)) for p in parts.values()) == 6 and len(parts) > 1
    assert all(len(p) <= 0.05 * 2**20 or len(pymupdf.open("pdf", p)) == 1 for p in parts.values())


def test_split_chapters_names_files_after_bookmarks(run):
    parts = unzip(run("split-chapters", RICH))
    assert sorted(parts) == ["doc1_01_One.pdf", "doc1_02_Two.pdf"]
    assert len(pymupdf.open("pdf", parts["doc1_02_Two.pdf"])) == 2


def test_split_sections_halves_pages(run):
    out = pdf(run("split-sections", make_pdf(2), columns=2, rows=1, pages="1"))
    assert [round(p.rect.width) for p in out] == [298, 298, 595]
    assert "Page 1" in out[0].get_text() and not out[1].get_text().strip()


def scan_with_blank_separator() -> bytes:
    doc = pymupdf.open("pdf", make_pdf(3))
    doc[1].add_redact_annot(doc[1].rect)
    doc[1].apply_redactions()
    return doc.tobytes()


def test_blank_pages_are_removed_or_used_as_separators(run):
    assert texts(pdf(run("remove-blank", scan_with_blank_separator()))) == ["Page 1", "Page 3"]
    assert sorted(unzip(run("split-blank", scan_with_blank_separator()))) == ["doc1_1.pdf", "doc1_3.pdf"]
    run("remove-blank", scan_with_blank_separator(), tolerance=0)
    assert len(pdf(run("remove-blank", make_pdf(3)))) == 3


def test_organize_remove_rotate_insert(run):
    assert texts(pdf(run("organize", make_pdf(3), pages="3,1,1"))) == ["Page 3", "Page 1", "Page 1"]
    assert texts(pdf(run("remove-pages", make_pdf(3), pages="2"))) == ["Page 1", "Page 3"]
    run("remove-pages", make_pdf(3), pages="all", expect=400)
    assert [p.rotation for p in pdf(run("rotate", make_pdf(3), angle=90, pages="2,2"))] == [0, 90, 0]
    assert texts(pdf(run("insert-blank", make_pdf(2), after="1,last", count=2))) == ["Page 1", "", "", "Page 2", "", ""]


@pytest.mark.parametrize("rotation", [0, 90])
def test_crop_trims_the_displayed_edges(run, rotation):
    before = pymupdf.open("pdf", make_pdf(1, rotation=rotation))[0].rect
    out = pdf(run("crop", make_pdf(1, rotation=rotation), left=50, top=20, right=10, bottom=5))
    assert (round(out[0].rect.width), round(out[0].rect.height)) == (round(before.width - 60), round(before.height - 25))
    assert "Page 1" in out[0].get_text()
    run("crop", make_pdf(1), left=400, right=400, expect=400)


@pytest.mark.parametrize("rotation", [0, 270])
def test_auto_crop_hugs_the_content(run, rotation):
    out = pdf(run("auto-crop", make_pdf(1, rotation=rotation), margin=5))
    assert out[0].rect.width < 130 and out[0].rect.height < 130 and "Page 1" in out[0].get_text()


def test_resize_keeps_orientation_and_bookmarks(run):
    out = pdf(run("resize", RICH, size="Letter"))
    assert (out[0].rect.width, out[0].rect.height) == (612, 792) and len(out.get_toc()) == 2
    wide = pdf(run("resize", make_pdf(1, width=842, height=595), size="A5"))
    assert wide[0].rect.width > wide[0].rect.height and "Page 1" in wide[0].get_text()


def test_n_up_and_booklet_and_single_page(run):
    sheets = pdf(run("n-up", make_pdf(5), per_sheet=4, border=True))
    assert len(sheets) == 2 and all(f"Page {n}" in sheets[0].get_text() for n in (1, 2, 3, 4))
    two_up = pdf(run("n-up", make_pdf(2), per_sheet=2))
    assert two_up[0].rect.width > two_up[0].rect.height
    book = pdf(run("booklet", make_pdf(8)))
    order = [[w[4] for w in sorted(p.get_text("words"), key=lambda w: w[0]) if w[4].isdigit()] for p in book]
    assert order == [["8", "1"], ["2", "7"], ["6", "3"], ["4", "5"]]
    tall = pdf(run("single-page", make_pdf(3)))
    assert len(tall) == 1 and round(tall[0].rect.height) == 3 * 842


def test_overlay_draws_second_pdf_on_every_page(run):
    out = pdf(run("overlay", make_pdf(3), extra={"overlay_pdf": ("over.pdf", make_pdf(1, "LETTERHEAD"))}))
    assert all("LETTERHEAD" in t for t in texts(out))
    once = pdf(run("overlay", make_pdf(3), extra={"overlay_pdf": ("over.pdf", make_pdf(1, "LETTERHEAD"))}, repeat=False))
    assert ["LETTERHEAD" in t for t in texts(once)] == [True, False, False]


# --- convert --------------------------------------------------------------------------------------

def test_images_and_text_become_pdf(run):
    merged = pdf(run("to-pdf", ("a.png", png()), ("b.jpg", png("red")), ("c.txt", b"plain words")))
    assert len(merged) == 3 and "plain words" in merged[2].get_text()
    assert sorted(unzip(run("to-pdf", ("a.png", png()), ("b.png", png()), merge=False))) == ["a.pdf", "b.pdf"]
    run("to-pdf", ("broken.png", b"not an image"), expect=400)


def test_pdf_to_images(run):
    images = unzip(run("pdf-to-images", make_pdf(3), dpi=50))
    assert sorted(images) == ["doc1_1.png", "doc1_2.png", "doc1_3.png"]
    assert all(abs(Image.open(io.BytesIO(d)).width - 595 * 50 / 72) <= 1 for d in images.values())
    one = run("pdf-to-images", make_pdf(3), format="jpg", pages="2", grayscale=True)
    assert one.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(one.content)).mode == "L"
    run("pdf-to-images", make_pdf(1, width=14000, height=14000), dpi=600, expect=400)


def test_pdf_to_text_formats(run):
    assert "Page 2" in run("pdf-to-text", make_pdf(3)).text
    assert "Chapter 1" in run("pdf-to-text", RICH, format="md").text
    assert "<p" in run("pdf-to-text", RICH, format="html", pages="1").text
    assert len(ET.fromstring(run("pdf-to-text", RICH, format="xml").content).findall("page")) == 4


def test_tables_become_csv(run):
    r = run("pdf-to-csv", RICH)
    assert "r0c0" in r.text and "r2c2" in r.text
    run("pdf-to-csv", make_pdf(1), expect=400)


def test_rasterize_leaves_no_text(run):
    out = pdf(run("rasterize", make_pdf(2), dpi=72))
    assert len(out) == 2 and not out[0].get_text().strip() and out[0].get_images()


@needs("soffice")
def test_office_round_trip(run):
    docx = run("pdf-to-office", make_pdf(1, "Hello office"), format="docx")
    assert docx.content[:2] == b"PK"
    back = pdf(run("to-pdf", ("letter.docx", docx.content)))
    assert "Hello office" in back[0].get_text()


@needs("ocrmypdf", "tesseract")
def test_ocr_makes_a_scan_searchable(run):
    scan = run("rasterize", make_pdf(1, "Searchable words"), dpi=200).content
    assert not pdf(type("R", (), {"content": scan})).__getitem__(0).get_text().strip()
    assert "Searchable" in pdf(run("ocr", scan))[0].get_text()


@needs("ocrmypdf", "gs")
def test_pdfa_conversion(run):
    out = pdf(run("pdf-to-pdfa", make_pdf(1)))
    assert "pdfaid" in (out.get_xml_metadata() or "") and "Page 1" in out[0].get_text()


def weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


@pytest.mark.skipif(not weasyprint_available(), reason="needs WeasyPrint's native libraries")
def test_html_markdown_and_email_become_pdf(run):
    out = pdf(run("to-pdf", ("a.html", b"<h1>Heading</h1><img src='file:///etc/passwd'><img src='http://127.0.0.1/x.png'>"),
                  ("b.md", b"# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |"),
                  ("c.eml", b"From: a@example.com\nSubject: Hi there\n\nMail body")))
    assert ["Heading" in out[0].get_text(), "Title" in out[1].get_text(), "Mail body" in out[2].get_text()] == [True] * 3


def test_web_page_tool_refuses_internal_addresses(run):
    run("url-to-pdf", url="http://127.0.0.1:8080/", expect=400)
    run("url-to-pdf", url="file:///etc/passwd", expect=400)


def test_weasyprint_is_present_when_required():
    import os
    if os.environ.get("REQUIRE_ALL"):
        assert weasyprint_available()


# --- security -------------------------------------------------------------------------------------

def test_password_round_trip_and_permissions(run):
    locked = run("protect", make_pdf(1), user_password="open", owner_password="own", allow_print=False).content
    doc = pymupdf.open("pdf", locked)
    assert doc.needs_pass and doc.authenticate("open")
    assert not doc.permissions & pymupdf.PDF_PERM_PRINT and doc.permissions & pymupdf.PDF_PERM_COPY
    assert "needs a password" in run("rotate", locked, expect=400).json()["detail"]
    assert "wrong password" in run("rotate", locked, password="nope", expect=400).json()["detail"]
    assert not pdf(run("unlock", locked, password="open")).needs_pass
    run("protect", make_pdf(1), expect=400)


def test_sanitize_removes_scripts_attachments_and_metadata(run):
    assert json.loads(run("javascript", RICH).content)["scripts"]
    out = pdf(run("sanitize", RICH))
    assert not json.loads(run("javascript", out.tobytes()).content)["scripts"]
    assert not out.embfile_names() and not out.metadata["title"]
    assert "Chapter 1" in out[0].get_text()


@pytest.mark.parametrize("rotation", [0, 90])
def test_redact_removes_the_text_itself(run, rotation):
    source = make_pdf(2, "Invoice {n} for Alice Smith", rotation=rotation)
    out = pdf(run("redact", source, find="alice smith\nInvoice"))
    assert texts(out) == ["1 for", "2 for"]
    assert b"Alice" not in out.tobytes(expand=255)
    run("redact", source, find="Bob", expect=400)


def test_redact_with_regex(run):
    out = pdf(run("redact", RICH, find=r"ABC-\d+", regex=True))
    assert "ABC-" not in " ".join(texts(out)) and "Secret code" in out[0].get_text()
    run("redact", RICH, find="(unclosed", regex=True, expect=400)


def test_watermark_text_and_image(run):
    out = pdf(run("watermark", make_pdf(2), text="DRAFT", tile=True))
    assert all(t.count("DRAFT") > 1 for t in texts(out))
    assert "Привет" in pdf(run("watermark", make_pdf(1), text="Привет"))[0].get_text()
    stamped = pdf(run("watermark", make_pdf(2), extra={"image": ("logo.png", png())}))
    assert stamped[0].get_text().strip() == "Page 1"
    run("watermark", make_pdf(1), text=" ", expect=400)
    run("watermark", make_pdf(1), extra={"image": ("logo.png", b"junk")}, expect=400)


def test_sign_validate_and_remove_signature(run):
    cert = {"certificate": ("me.p12", make_p12(b"pw"))}
    signed = run("sign", make_pdf(1), extra=cert, certificate_password="pw", reason="Approved", visible=True).content
    report = json.loads(run("validate-signatures", signed).content)
    assert report["signed"] and report["signatures"][0]["unchanged_since_signing"]
    assert "Test Signer" in report["signatures"][0]["signer"]
    twice = run("sign", signed, extra=cert, certificate_password="pw").content
    assert [s["unchanged_since_signing"] for s in json.loads(run("validate-signatures", twice).content)["signatures"]] == [True, True]
    assert json.loads(run("info", signed).content)["signed"]
    run("sign", make_pdf(1), extra=cert, certificate_password="wrong", expect=400)
    cleared = run("remove-signatures", signed).content
    assert not json.loads(run("validate-signatures", cleared).content)["signed"]
    assert not json.loads(run("validate-signatures", make_pdf(1)).content)["signed"]


# --- edit -----------------------------------------------------------------------------------------

def test_stamp_and_page_numbers(run):
    out = pdf(run("stamp", make_pdf(2), text="Copy {n} of {total}", position="top-right"))
    assert ["Copy 1 of 2" in texts(out)[0], "Copy 2 of 2" in texts(out)[1]] == [True, True]
    numbered = pdf(run("page-numbers", make_pdf(3, "x"), text="ABC{n}", start=7, digits=6, pages="2-3"))
    assert texts(numbered) == ["x", "x ABC000007", "x ABC000008"]
    word = next(w for w in numbered[1].get_text("words") if w[4].startswith("ABC"))
    assert word[1] > 780 and 250 < word[0] < 300  # bottom centre


@pytest.mark.parametrize("rotation", [0, 90])
def test_find_and_replace(run, rotation):
    out = pdf(run("replace-text", make_pdf(2, rotation=rotation), find="Page", replace="Sheet"))
    words = sorted(out[0].get_text("words"))  # left to right, in page coordinates
    assert [w[4] for w in words] == ["Sheet", "1"]
    assert abs(words[0][0] - 72) < 2 and abs(words[0][3] - words[1][3]) < 2  # same start, same baseline
    assert words[0][2] < words[1][0]  # shrunk to fit: does not run into the next word
    run("replace-text", make_pdf(1), find="absent", replace="x", expect=400)


def test_highlight_adds_annotations(run):
    out = pdf(run("highlight", make_pdf(2), find="page"))
    assert [len(list(p.annots())) for p in out] == [1, 1]
    assert not any(list(p.annots()) for p in pdf(run("remove-annotations", out.tobytes())))


@pytest.mark.parametrize("rotation", [0, 90])
def test_visual_editor_operations(run, rotation):
    data_url = "data:image/png;base64," + __import__("base64").b64encode(png()).decode()
    ops = [{"type": "redact", "page": 0, "rect": [0, 0, 2000, 2000]},
           {"type": "text", "page": 0, "points": [[50, 50]], "text": "Typed text", "size": 16},
           {"type": "ink", "page": 0, "points": [[10, 200], [100, 220], [150, 260]], "color": "#ff0000"},
           {"type": "image", "page": 1, "rect": [100, 100, 200, 150], "image": data_url},
           {"type": "whiteout", "page": 1, "rect": [200, 300, 100, 250]},
           {"type": "highlight", "page": 1, "rect": [60, 60, 200, 90]}]
    out = pdf(run("edit", make_pdf(2, rotation=rotation), ops=json.dumps(ops)))
    assert texts(out) == ["Typed text", "Page 2"]
    shown = out[0].get_text("words")[0]
    upright = pymupdf.Rect(shown[:4]) * out[0].rotation_matrix
    assert abs(upright.normalize().x0 - 50) < 3 and 44 < upright.y0 < 56  # where the user clicked, as displayed
    run("edit", make_pdf(1), ops=json.dumps([{"type": "text", "page": 5, "points": [[1, 1]], "text": "x"}]), expect=400)
    run("edit", make_pdf(1), ops="[{\"type\": \"explode\", \"page\": 0}]", expect=400)


def test_metadata_and_bookmarks(run):
    out = pdf(run("metadata", RICH, title="New title"))
    assert (out.metadata["title"], out.metadata["author"]) == ("New title", "Tester")
    assert not pdf(run("metadata", RICH, title="Only", clear_others=True)).metadata["author"]
    toc = [[1, "Start", 1], [2, "Deeper", 2]]
    assert [t[:3] for t in pdf(run("bookmarks", make_pdf(2), outline=json.dumps(toc))).get_toc()] == toc
    assert pdf(run("bookmarks", RICH, outline="[]")).get_toc() == []
    run("bookmarks", make_pdf(2), outline="not json", expect=400)
    run("bookmarks", make_pdf(2), outline='[[2, "No parent", 1]]', expect=400)


def test_forms_fill_unlock_flatten(run):
    def fields(doc):
        page = doc[0]  # a widget is only usable while its page object is alive
        return {w.field_name: (w.field_value, w.field_flags) for w in page.widgets()}

    assert fields(pdf(run("fill-form", RICH, values='{"name": "Ada"}')))["name"][0] == "Ada"
    flat = pdf(run("fill-form", RICH, values='{"name": "Ada"}', flatten=True))
    assert not fields(flat) and "Ada" in flat[0].get_text()
    run("fill-form", RICH, values='{"nope": 1}', expect=400)

    locked = pymupdf.open("pdf", RICH)
    page = locked[0]
    locked.xref_set_key(next(page.widgets()).xref, "Ff", str(pymupdf.PDF_FIELD_IS_READ_ONLY))
    assert fields(pymupdf.open("pdf", locked.tobytes()))["name"][1] & pymupdf.PDF_FIELD_IS_READ_ONLY
    assert not fields(pdf(run("unlock-forms", locked.tobytes())))["name"][1] & pymupdf.PDF_FIELD_IS_READ_ONLY
    assert not list(pdf(run("flatten", RICH))[2].annots())


def test_attachments_round_trip(run):
    out = run("attach", make_pdf(1), extra={"attachments": [("a.txt", b"first"), ("b.csv", b"1,2")]})
    assert pdf(out).embfile_names() == ["a.txt", "b.csv"]
    assert unzip(run("extract-attachments", out.content)) == {"doc1_a.txt": b"first", "doc1_b.csv": b"1,2"}
    run("extract-attachments", make_pdf(1), expect=400)


def test_image_removal_and_colour_effects(run):
    def picture_pixel(data):  # middle of the red picture on page 1
        return pymupdf.open("pdf", data)[0].get_pixmap(dpi=72).pixel(130, 240)

    assert picture_pixel(RICH)[0] > 150 and picture_pixel(RICH)[1] < 80
    assert picture_pixel(run("remove-images", RICH).content) == (255, 255, 255)
    for tool in ("adjust-colors", "invert-colors", "scan-effect"):
        assert len(pdf(run(tool, make_pdf(2), dpi=50))) == 2
    dark = pdf(run("invert-colors", make_pdf(1), dpi=36))[0].get_pixmap(dpi=10)
    assert dark.pixel(0, 0)[0] < 30
    gray = pdf(run("grayscale", RICH))
    assert gray[0].get_pixmap(dpi=20).is_monochrome or len(set(gray[0].get_pixmap(dpi=20).pixel(30, 60))) == 1


# --- inspect --------------------------------------------------------------------------------------

def test_info_reports_the_document(run):
    info = json.loads(run("info", RICH).content)
    assert info["pages"] == 4 and info["metadata"]["title"] == "Sample" and info["javascript_actions"] == 1
    assert info["attachments"] == ["note.txt"] and info["bookmarks"][1] == [1, "Two", 3]
    assert "name" in info["form_fields"] and info["has_text"] and not info["signed"]
    assert info["page_details"][0]["images"] == 1 and info["page_details"][2]["annotations"] == 1


def test_compare_reports_differences(run):
    r = run("compare", make_pdf(2, "Same {n}"), make_pdf(2, "Same {n} but changed"))
    assert r.headers["content-type"].startswith("text/html") and "diff_chg" in r.text or "diff_add" in r.text
    run("compare", make_pdf(1), expect=400)


def test_compress_shrinks_a_picture_heavy_file(run):
    doc = pymupdf.open()
    Image.effect_noise((1500, 1500), 60).convert("RGB").save(buf := io.BytesIO(), "PNG")
    doc.new_page().insert_image(pymupdf.Rect(0, 0, 300, 300), stream=buf.getvalue())
    original = doc.tobytes()
    small = run("compress", original, level="high").content
    assert len(small) < len(original) / 3 and len(pymupdf.open("pdf", small)) == 1


def test_repair_extract_images_rename_preview(run):
    assert len(pdf(run("repair", make_pdf(2) + b"trailing junk"))) == 2
    picture = run("extract-images", RICH)
    assert picture.headers["content-type"].startswith("image/")
    run("extract-images", make_pdf(1), expect=400)
    assert "Chapter%201.pdf" in run("auto-rename", RICH).headers["content-disposition"]
    shown = json.loads(run("preview", make_pdf(3), dpi=30, pages="2").content)
    assert shown["count"] == 3 and shown["pages"][0]["index"] == 1 and shown["pages"][0]["image"].startswith("data:image/jpeg")
