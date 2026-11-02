from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURL(ValueError):
    pass


def validate_public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURL("only http/https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeURL("URL hostname is required")
    if parsed.username or parsed.password:
        raise UnsafeURL("credentials in URLs are not allowed")

    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURL(f"hostname cannot be resolved: {host}") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue

    if not addresses:
        raise UnsafeURL("hostname resolved to no usable address")

    for addr in addresses:
        for candidate in [addr, *_embedded_ipv4(addr)]:
            if _non_public(candidate):
                raise UnsafeURL("non-public destination refused")

    return url


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_IPV4_COMPAT = ipaddress.ip_network("::/96")


def _non_public(addr) -> bool:
    """Anything that is not globally routable unicast is refused (CGNAT, benchmark,
    documentation, IETF special ranges included), on top of the explicit checks."""
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )


def _embedded_ipv4(addr) -> list:
    """IPv4 addresses carried inside an IPv6 address that a gateway may route to:
    IPv4-mapped, IPv4-compatible, NAT64 (RFC 6052 /96 and the local-use /48), 6to4,
    Teredo (client address). Each one must also be public."""
    if addr.version != 6:
        return []
    found = []
    if addr.ipv4_mapped is not None:
        found.append(addr.ipv4_mapped)
    if addr.sixtofour is not None:
        found.append(addr.sixtofour)
    if addr.teredo is not None:
        found.append(addr.teredo[1])
    if any(addr in net for net in _NAT64) or (addr in _IPV4_COMPAT and int(addr) > 1):
        found.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return found
