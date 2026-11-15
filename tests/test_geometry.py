"""Drawing helpers must land where the user sees them, whatever the page's rotation and crop."""
import io

import pymupdf
import pytest
from PIL import Image, ImageChops

from app.core import FONT, draw_text, place, put
from app.tools.pages import _crop

CASES = [(r, c) for r in (0, 90, 180, 270) for c in (False, True)]


def blank(rotation: int, cropped: bool, mark: bool = False) -> pymupdf.Document:
    """One 400x600 page, optionally with an off-centre mark, then rotated and cropped."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    if mark:
        page.draw_rect(pymupdf.Rect(40, 60, 160, 110), color=None, fill=(0, 0, 0))
        page.insert_text((60, 300), "MARK", fontsize=40)
    page.set_rotation(rotation)
    doc = pymupdf.open("pdf", doc.tobytes())
    if cropped:
        r = doc[0].rect
        _crop(doc[0], pymupdf.Rect(30, 50, r.width - 20, r.height - 40))
        doc = pymupdf.open("pdf", doc.tobytes())
    return doc


def shot(page, dpi=72) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def ink(page) -> tuple:
    """Bounding box of everything dark on the displayed page, in points."""
    return shot(page).point(lambda v: 255 * (v < 160)).getbbox()


def png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "black").save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("rotation, cropped", CASES)
def test_text_stamp_lands_at_its_anchor(rotation, cropped):
    page = blank(rotation, cropped)[0]
    W, H = page.rect.width, page.rect.height
    put(page, "top-left", text="STAMP", size=20, margin=10)
    x0, y0, x1, y1 = ink(page)
    assert abs(x0 - 10) <= 3 and 10 <= y0 <= 18 and y1 <= 32, (x0, y0, x1, y1)
    assert abs((x1 - x0) - FONT.text_length("STAMP", fontsize=20)) <= 5  # upright and horizontal

    page = blank(rotation, cropped)[0]
    put(page, "bottom-right", text="STAMP", size=20, margin=10)
    x0, y0, x1, y1 = ink(page)
    assert abs(x1 - (W - 10)) <= 3 and H - 32 <= y0 and y1 <= H - 8, (x0, y0, x1, y1)


@pytest.mark.parametrize("rotation, cropped", CASES)
def test_image_stamp_lands_at_its_anchor(rotation, cropped):
    page = blank(rotation, cropped)[0]
    W, H = page.rect.width, page.rect.height
    put(page, "bottom-right", image=png(80, 40), image_width=25, margin=10)
    w = W * 0.25
    expected = (W - 10 - w, H - 10 - w / 2, W - 10, H - 10)
    assert all(abs(a - b) <= 2 for a, b in zip(ink(page), expected)), (ink(page), expected)


def test_text_angle_is_counter_clockwise():
    page = blank(0, False)[0]
    draw_text(page, 200, 300, "UPWARDS", 20, angle=90)
    x0, y0, x1, y1 = ink(page)
    assert y1 - y0 > 3 * (x1 - x0)  # vertical
    direction = page.get_text("dict")["blocks"][0]["lines"][0]["dir"]
    assert direction[1] < -0.9  # runs towards the top of the page


@pytest.mark.parametrize("rotation, cropped", CASES)
@pytest.mark.parametrize("target_rotation", [0, 90, 180, 270])
def test_place_reproduces_the_displayed_page(rotation, cropped, target_rotation):
    src = blank(rotation, cropped, mark=True)
    expected = shot(src[0], 36)
    W, H = src[0].rect.width, src[0].rect.height
    out = pymupdf.open()
    target = out.new_page(width=W if target_rotation in (0, 180) else H, height=H if target_rotation in (0, 180) else W)
    target.set_rotation(target_rotation)
    place(target, target.rect, src, 0)
    actual = shot(pymupdf.open("pdf", out.tobytes())[0], 36)
    assert actual.size == expected.size
    assert sum(ImageChops.difference(actual, expected).histogram()[64:]) < 0.005 * actual.width * actual.height
    assert src[0].rotation == rotation  # the source is left as it was


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_place_clip_takes_the_displayed_region(rotation):
    src = blank(rotation, False, mark=True)
    W, H = src[0].rect.width, src[0].rect.height
    expected = shot(src[0], 36).crop((0, 0, int(W / 2 * 0.5), int(H * 0.5)))
    out = pymupdf.open()
    target = out.new_page(width=W / 2, height=H)
    place(target, target.rect, src, 0, clip=pymupdf.Rect(0, 0, W / 2, H))
    actual = shot(pymupdf.open("pdf", out.tobytes())[0], 36)
    assert abs(actual.width - expected.width) <= 1 and abs(actual.height - expected.height) <= 1
    actual = actual.resize(expected.size)
    assert sum(ImageChops.difference(actual, expected).histogram()[64:]) < 0.01 * actual.width * actual.height
