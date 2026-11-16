import pytest

from app.core import ToolError, parse_pages, safe_name
from app.tools.convert import check_public


@pytest.mark.parametrize("spec, expected", [
    ("", list(range(10))), ("all", list(range(10))), ("1,3,5-7", [0, 2, 4, 5, 6]), ("8-", [7, 8, 9]),
    ("last", [9]), ("3-1", [2, 1, 0]), ("odd", [0, 2, 4, 6, 8]), ("even", [1, 3, 5, 7, 9]),
    ("2n", [1, 3, 5, 7, 9]), ("3n+1", [0, 3, 6, 9]), ("3n-1", [1, 4, 7]), ("reverse", list(range(9, -1, -1))),
    ("1,1,2", [0, 0, 1]), (" 2 , 4 ,", [1, 3]), ("8-last", [7, 8, 9]), ("N", list(range(10))),
])
def test_parse_pages(spec, expected):
    assert parse_pages(spec, 10) == expected


@pytest.mark.parametrize("spec", ["0", "11", "5-12", "abc", "1;2", "-3", "0n"])
def test_parse_pages_rejects(spec):
    with pytest.raises(ToolError):
        parse_pages(spec, 10)


def test_safe_name_strips_paths():
    assert "/" not in safe_name("../../etc/passwd") and "\\" not in safe_name(r"..\x.pdf")
    assert safe_name("résumé (1).pdf") == "résumé (1).pdf"
    assert safe_name("...") == "file"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8080/x", "file:///etc/passwd", "ftp://example.com/",
                                 "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/",
                                 "javascript:alert(1)", "http://", "http://2130706433/", "http://0.0.0.0/", "http://user@127.0.0.1/",
                                 "http://[::ffff:10.0.0.1]/", "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/"])
def test_private_and_local_addresses_are_refused(url):
    with pytest.raises(ToolError):
        check_public(url)
