import pytest

from bridge.security import UnsafeURL, validate_public_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://192.168.1.10/",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ],
)
def test_private_or_non_http_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        validate_public_http_url(url)


def test_public_https_url_is_allowed():
    assert validate_public_http_url("https://example.com/path") == "https://example.com/path"
