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
        # WP-002: non-global ranges and IPv4 embedded in IPv6
        "http://100.64.0.1/",            # CGNAT
        "http://100.100.100.200/",        # cloud metadata in CGNAT range
        "http://169.254.169.254/",        # link-local metadata
        "http://198.18.0.1/",             # benchmark
        "http://192.0.2.1/",              # documentation
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",     # IPv4-mapped loopback
        "http://[::ffff:169.254.169.254]/",
        "http://[::127.0.0.1]/",          # IPv4-compatible loopback
        "http://[64:ff9b::7f00:1]/",      # NAT64 -> 127.0.0.1
        "http://[64:ff9b::a9fe:a9fe]/",   # NAT64 -> 169.254.169.254
        "http://[64:ff9b:1::a00:1]/",     # local-use NAT64 -> 10.0.0.1
        "http://[2002:7f00:1::1]/",       # 6to4 -> 127.0.0.1
        "http://[2001:0:4136:e378:8000:63bf:80ff:fffe]/",  # Teredo -> 127.0.0.1 client
        "http://[fc00::1]/",              # ULA
        "http://[fe80::1]/",              # link-local
        "http://[2001:db8::1]/",          # documentation
        "http://[64:ff9b::808:808]/",     # whole NAT64 prefix stays refused (pre-existing, fail closed)
    ],
)
def test_private_or_non_http_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        validate_public_http_url(url)


def test_public_https_url_is_allowed():
    assert validate_public_http_url("https://example.com/path") == "https://example.com/path"


@pytest.mark.parametrize("url", [
    "http://8.8.8.8/",
    "http://[2606:4700:4700::1111]/",
    "http://[::ffff:8.8.8.8]/",
])
def test_public_literals_are_allowed(url):
    assert validate_public_http_url(url) == url


def test_refusal_message_does_not_echo_the_address():
    with pytest.raises(UnsafeURL) as err:
        validate_public_http_url("http://[64:ff9b::a9fe:a9fe]/")
    assert "a9fe" not in str(err.value) and "169.254" not in str(err.value)


@pytest.mark.parametrize("url", ["http://[fec0::1]/", "http://[fec0::7f00:1]/", "https://[feff::1]/",
                                 "http://[fed0:1::2]:8080/x"])
def test_ipv6_site_local_is_refused(url):
    """WP-002b P3: fec0::/10 was accepted because Python marks it is_global."""
    with pytest.raises(UnsafeURL):
        validate_public_http_url(url)
