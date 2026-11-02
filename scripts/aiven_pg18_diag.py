"""Non-sensitive diagnostics for the manual FCB Aiven PG18 test workflow.

Stdlib only; the workflow runs it with ``python -I -S`` while the secret is in the
environment. Nothing printed by this module may contain the host, IP address, port,
user, password or database name of the secret URI: only fixed categories and a libpq
message with every such value replaced by a placeholder.

Subcommands (the URI is always read from $FCB_PG18_TEST_URL, never from argv):
  check-raw          fail if the raw secret has whitespace or control characters
                     (Python's urlsplit silently drops them, libpq does not)
  probe              DNS address-family counts + TCP reachability per family
  sanitize FILE      print one redacted line of a libpq stderr capture
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
from urllib.parse import unquote, urlsplit

ENV = "FCB_PG18_TEST_URL"
MAX_LINE = 300

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6 literals: at least two colons among hex groups (covers ::1, fe80::x, full form).
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
# Any dotted DNS-like name (one or more dots, underscores allowed), except libpq's own
# "pg_hba.conf" so that message stays readable.
_FQDN = re.compile(r"(?<![\w.-])(?!pg_hba\.conf\b)[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?(?:\.[A-Za-z0-9_-]+)+")
_PORT = re.compile(r"\bport\s+\d+\b", re.IGNORECASE)


def raw_problem(raw: str) -> str | None:
    """Return a non-sensitive category if the raw secret has whitespace/control chars."""
    bad = [i for i, c in enumerate(raw) if ord(c) <= 0x20 or ord(c) == 0x7F]
    if not bad:
        return None
    n, k = len(raw), len(bad)
    if bad == list(range(n - k, n)):
        return "trailing_whitespace_or_newline"
    if bad == list(range(k)):
        return "leading_whitespace"
    return "embedded_whitespace_or_control_char"


def _secret_values(raw: str) -> list[str]:
    vals: list[str] = []
    try:
        u = urlsplit(raw)
        cand = [u.hostname, u.username, u.password, u.port and str(u.port), u.path.lstrip("/")]
    except ValueError:
        cand = []
    for v in cand:
        if v:
            vals.append(v)
            dv = unquote(v)
            if dv != v:
                vals.append(dv)
    vals.append(raw.strip())
    # Longest first so a value containing another is replaced whole.
    return sorted(set(vals), key=len, reverse=True)


def sanitize(text: str, raw: str) -> str:
    out = text
    for v in _secret_values(raw):
        if len(v) >= 2:
            # Case-insensitive: urlsplit lowercases the host, libpq echoes it as typed.
            out = re.sub(re.escape(v), "<redacted>", out, flags=re.IGNORECASE)
        else:
            # A 1-character value is redacted only where libpq quotes or delimits it.
            out = re.sub(r'(["\']|:)' + re.escape(v) + r'(?=["\'\s,)]|$)', r"\1<redacted>", out,
                         flags=re.IGNORECASE)
    out = _FQDN.sub("<host>", out)
    out = _IPV4.sub("<ip>", out)
    out = _IPV6.sub("<ip6>", out)
    out = _PORT.sub("port <port>", out)
    out = " ".join(out.split())  # one line, no control characters
    out = "".join(c for c in out if c.isprintable())
    return out[:MAX_LINE]


def _match(value: str, env_key: str) -> str:
    """yes/no/unset: SHA-256 of the value against a pinned hash (the value is never printed)."""
    want = os.environ.get(env_key, "").strip().lower()
    if not want:
        return "unset"
    return "yes" if hashlib.sha256(value.encode()).hexdigest() == want else "no"


PUBLIC_PORT = "14274"  # public port of the Aiven service piero-1 (not a secret)


def port_layout(raw: str, public_port: str = PUBLIC_PORT) -> str:
    """Where the port is in the URI, without printing any value.

    port_form: explicit (digits after host:), empty ("host:"), absent (no ':' after host).
    public_port_in: which URI parts contain the public port string (netloc_port,
    userinfo, path, query, fragment) or "none".
    """
    u = urlsplit(raw.strip())
    hostinfo = u.netloc.rpartition("@")[2]
    userinfo = u.netloc.rpartition("@")[0] if "@" in u.netloc else ""
    if hostinfo.startswith("["):
        _, _, after = hostinfo.partition("]")
    else:
        _, _, after = hostinfo.partition(":")
        after = ":" + after if ":" in hostinfo else ""
    if not after:
        form = "absent"
    elif after == ":":
        form = "empty"
    else:
        form = "explicit"
    where = [name for name, part in (("netloc_port", after), ("userinfo", userinfo),
                                     ("path", u.path), ("query", u.query),
                                     ("fragment", u.fragment)) if public_port in part]
    return f"port_form={form} public_port_in={','.join(where) or 'none'}"


def probe(raw: str, timeout: float = 5.0) -> list[str]:
    u = urlsplit(raw.strip())
    host, port = u.hostname, u.port or 5432
    lines: list[str] = [
        f"host_matches_service={_match(host or '', 'EXPECTED_HOST_SHA256')} "
        f"port_matches_service={_match(str(port), 'EXPECTED_PORT_SHA256')}",
        port_layout(raw),
    ]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return lines + [f"dns=failed errno={exc.errno}"]
    fams: dict[str, list] = {"ipv4": [], "ipv6": []}
    for fam, _t, _p, _c, addr in infos:
        key = "ipv4" if fam == socket.AF_INET else "ipv6" if fam == socket.AF_INET6 else None
        if key and addr not in fams[key]:
            fams[key].append(addr)
    lines.append(f"dns=ok ipv4_addresses={len(fams['ipv4'])} ipv6_addresses={len(fams['ipv6'])}")
    for key, addrs in fams.items():
        for idx, addr in enumerate(addrs):
            fam = socket.AF_INET if key == "ipv4" else socket.AF_INET6
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                s.connect(addr)
                res = "open"
            except socket.timeout:
                res = "timeout"
            except ConnectionRefusedError:
                res = "refused"
            except OSError as exc:
                res = f"oserror_errno_{exc.errno}"
            finally:
                s.close()
            lines.append(f"tcp {key}#{idx}={res}")
    return lines


def main(argv: list[str]) -> int:
    raw = os.environ.get(ENV, "")
    if not raw or len(argv) < 2:
        print("::error::diag: missing secret or subcommand", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "check-raw":
        why = raw_problem(raw)
        if why:
            print(f"::error::secret contains whitespace or control characters ({why}); "
                  "re-save it without spaces or a final newline", file=sys.stderr)
            return 1
        return 0
    if cmd == "probe":
        for line in probe(raw):
            print(f"probe: {line}")
        return 0
    if cmd == "sanitize" and len(argv) == 3:
        try:
            with open(argv[2], encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        print(sanitize(text, raw) or "<empty libpq message>")
        return 0
    print("::error::diag: unknown subcommand", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
