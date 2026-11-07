#!/usr/bin/env python3
"""Real PostgreSQL 18 TLS adversarial harness for FCB-WP-005/008.

Uses throwaway Docker containers and locally generated test CAs. These CAs are test
material only and are never represented as the authentic Aiven project CA.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _certificates(root: Path, *, expired: bool = False):
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FCB WP005 test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(days=2)).not_valid_after(now + dt.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    end = now - dt.timedelta(days=1) if expired else now + dt.timedelta(days=1)
    start = now - dt.timedelta(days=2)
    server = (x509.CertificateBuilder()
              .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
              .issuer_name(ca.subject).public_key(server_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    (root / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (root / "server.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (root / "server.key").write_bytes(server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wrong test CA")])
    other = (x509.CertificateBuilder().subject_name(other_name).issuer_name(other_name)
             .public_key(other_key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
             .sign(other_key, hashes.SHA256()))
    (root / "wrong-ca.pem").write_bytes(other.public_bytes(serialization.Encoding.PEM))
    for path in root.iterdir():
        path.chmod(0o644)


def _start(cert_dir: Path):
    name = "fcb-wp005-tls-" + uuid.uuid4().hex[:10]
    cmd = [
        "docker", "run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=test-only-password",
        "-p", "127.0.0.1::5432", "-v", f"{cert_dir}:/input:ro", "postgres:18", "bash", "-ceu",
        "install -d -o postgres -g postgres -m 700 /tmp/pgcert; "
        "cp /input/server.crt /input/server.key /tmp/pgcert/; "
        "chown postgres:postgres /tmp/pgcert/*; chmod 600 /tmp/pgcert/server.key; "
        "exec docker-entrypoint.sh postgres -c ssl=on "
        "-c ssl_cert_file=/tmp/pgcert/server.crt -c ssl_key_file=/tmp/pgcert/server.key",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    raw = subprocess.run(["docker", "port", name, "5432/tcp"], check=True,
                         capture_output=True, text=True).stdout.strip()
    port = int(raw.rsplit(":", 1)[1])
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(host="127.0.0.1", port=port, dbname="postgres", user="postgres",
                                 password="test-only-password", sslmode="disable", connect_timeout=2) as conn:
                major = conn.info.server_version // 10000
                if major != 18:
                    raise RuntimeError(f"wrong PostgreSQL major: {major}")
                return name, port
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError("PostgreSQL 18 TLS container did not become ready")


def _connect(port: int, **overrides):
    values = dict(host="localhost", port=port, dbname="postgres", user="postgres",
                  password="test-only-password", sslmode="verify-full")
    values.update(overrides)
    return psycopg.connect(connect_timeout=5, **values)


def _must_fail(label: str, fn, results: dict):
    try:
        with fn():
            pass
    except psycopg.OperationalError:
        results[label] = "PASS_FAIL_CLOSED"
    else:
        raise AssertionError(f"{label} unexpectedly connected")


def main():
    if not shutil.which("docker"):
        raise SystemExit("docker is required; this CI test must not be skipped")
    results = {"test_ca_only": True, "postgres_major": 18}
    containers = []
    with tempfile.TemporaryDirectory(prefix="fcb-wp005-tls-") as td:
        valid = Path(td) / "valid"
        expired = Path(td) / "expired"
        valid.mkdir(); expired.mkdir()
        _certificates(valid)
        _certificates(expired, expired=True)
        try:
            name, port = _start(valid); containers.append(name)
            with _connect(port, sslrootcert=str(valid / "ca.pem")):
                results["verify_full_valid"] = "PASS"
            _must_fail("wrong_hostname", lambda: _connect(
                port, host="127.0.0.1", sslrootcert=str(valid / "ca.pem")), results)
            _must_fail("wrong_ca", lambda: _connect(
                port, sslrootcert=str(valid / "wrong-ca.pem")), results)
            _must_fail("wrong_credentials", lambda: _connect(
                port, password="wrong", sslrootcert=str(valid / "ca.pem")), results)
            with _connect(port, sslmode="require", sslrootcert=None):
                results["unverified_tls_connects_at_libpq_layer"] = "RED_CONTROL"

            name, expired_port = _start(expired); containers.append(name)
            _must_fail("expired_certificate", lambda: _connect(
                expired_port, sslrootcert=str(expired / "ca.pem")), results)
        finally:
            for name in containers:
                subprocess.run(["docker", "rm", "-f", name], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
