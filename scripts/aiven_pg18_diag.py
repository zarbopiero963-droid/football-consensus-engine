"""Non-sensitive diagnostics for the manual FCB Aiven PG18 test workflow.

Stdlib only; the workflow runs it with ``python -I -S`` while the secret is in the
environment. Nothing printed by this module may contain the host, IP address, port,
user, password or database name of the secret URI: only fixed categories and a libpq
message with every such value replaced by a placeholder.

Subcommands (the URI is always read from $FCB_PG18_TEST_URL, or from $FCB_DEFAULTDB_URL when
FCB_DB_URL_VAR names it, never from argv):
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
from urllib.parse import parse_qs, unquote, urlsplit

ENV = "FCB_PG18_TEST_URL"
# FCB-WP-005: the defaultdb restore workflow reuses this module with its own secret name.
# Only these variable names may carry the URI; anything else is a configuration error.
URL_VARS = ("FCB_PG18_TEST_URL", "FCB_DEFAULTDB_URL")


def url_var(env=None) -> str | None:
    env = os.environ if env is None else env
    name = env.get("FCB_DB_URL_VAR", ENV)
    return name if name in URL_VARS else None
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


# libpq variables that could redirect the connection away from the URI's host/database.
_REDIRECT_ENV = ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGDATABASE", "PGOPTIONS",
                 "PGTARGETSESSIONATTRS")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_ALLOWED_SSLMODES = ("require", "verify-ca", "verify-full")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def target_check(raw: str, env: dict | None = None) -> tuple[bool, str]:
    """Fail-closed target policy. Returns (ok, non-sensitive message).

    Python's urlsplit and libpq parse URIs differently (e.g. libpq does not treat '#' as a
    fragment and keeps the LAST duplicate parameter), so the raw string is held to a strict
    grammar that both parsers read the same way:
      postgres[ql]://<user>:<password>@<host>[:[14274]]/fcb_pg18_test[?sslmode=<allowed>]
    - no '#' anywhere; the query is empty or exactly one 'sslmode' with an allowed value;
    - host: lowercase ASCII DNS name whose SHA-256 equals EXPECTED_HOST_SHA256 (pinned);
    - database: path exactly '/' + EXPECTED_DB;
    - port: EXPECTED_PORT (job config, pinned by EXPECTED_PORT_SHA256, exported as PGPORT) is
      used by libpq when the URI port is empty or absent; an explicit port must be exactly
      that string, anything else is rejected (never rewritten);
    - no libpq environment variable that could redirect the connection may be set.
    """
    env = os.environ if env is None else env
    exp_port = env.get("EXPECTED_PORT", "")
    if not exp_port or _sha(exp_port) != env.get("EXPECTED_PORT_SHA256", "").strip().lower():
        return False, "config_error: EXPECTED_PORT missing or not matching EXPECTED_PORT_SHA256"
    if env.get("PGPORT") != exp_port:
        return False, "config_error: PGPORT must equal EXPECTED_PORT"
    for k in _REDIRECT_ENV:
        if env.get(k):
            return False, f"config_error: {k} must not be set"
    # No strip(): psql receives the raw value, so the raw value is what must be validated.
    if raw_problem(raw):
        return False, "whitespace_or_control_char"
    if "#" in raw:
        return False, "fragment_not_allowed"
    scheme, sep, rest = raw.partition("://")
    if not sep or scheme not in ("postgres", "postgresql"):
        return False, "scheme_not_postgres"
    authority, slash, tail = rest.partition("/")
    if not slash:
        return False, "database_not_expected"
    if authority.count("@") != 1 or "," in authority:
        return False, "netloc_not_single_user_at_host"
    userinfo, _, hostport = authority.partition("@")
    if not userinfo or "?" in userinfo:
        return False, "netloc_not_single_user_at_host"
    host, colon, port = hostport.partition(":")
    if not _HOST_RE.fullmatch(host):
        return False, "host_not_expected_service"
    if _sha(host) != env.get("EXPECTED_HOST_SHA256", "").strip().lower():
        return False, "host_not_expected_service"
    if port and port != exp_port:
        if not port.isdigit():
            return False, "port_invalid"
        return False, "port_mismatch_explicit (rejected, not rewritten)"
    path, qmark, query = tail.partition("?")
    if path != env.get("EXPECTED_DB", "\0"):
        return False, "database_not_expected"
    if qmark and query:
        key, eq, val = query.partition("=")
        if key != "sslmode" or not eq or "&" in val or "=" in val:
            return False, "query_not_allowed (only a single sslmode parameter)"
        if val not in _ALLOWED_SSLMODES:
            return False, "sslmode_too_weak"
    elif qmark:
        return False, "query_not_allowed (only a single sslmode parameter)"
    source = "uri" if port else ("config(empty)" if colon else "config(absent)")
    return True, f"host=expected_service database={env['EXPECTED_DB']} port={exp_port} port_source={source}"


def probe(raw: str, timeout: float = 5.0) -> list[str]:
    u = urlsplit(raw.strip())
    # Effective port as libpq resolves it: explicit URI port, else PGPORT, else 5432.
    host, port = u.hostname, u.port or int(os.environ.get("PGPORT") or 5432)
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
    var = url_var()
    if var is None:
        print("::error::diag: FCB_DB_URL_VAR is not an allowed variable name", file=sys.stderr)
        return 2
    raw = os.environ.get(var, "")
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
    if cmd == "target":
        ok, msg = target_check(raw)
        if not ok:
            print(f"::error::target check failed: {msg}", file=sys.stderr)
            return 1
        print(f"target: {msg}")
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
