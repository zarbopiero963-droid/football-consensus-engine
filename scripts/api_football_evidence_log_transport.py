#!/usr/bin/env python3
"""Artifact-quota-independent evidence transport: safe ZIP as chunked Actions log lines.

Run logs are downloadable from GitHub Actions. Never include API keys or raw
provider HTTP bodies. The included inventory files are sanitized projections.
"""
import argparse
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import zipfile

PREFIX = "AF_EVIDENCE_V1"
MAX_TOTAL = 2_000_000
FILES = ("route_inventory.json", "route_inventory.csv", "capabilities.json",
         "catalogs.csv", "field_registry.csv", "pagination_coverage.json",
         "odds_observations.csv")

def encode(paths):
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            if path.is_file() and path.name in FILES:
                raw = path.read_bytes()
                # Evidence is public in repository workflow logs; prevent key disclosure.
                secret = os.environ.get("APIFOOTBALL_API_KEY", "").encode()
                if secret and secret in raw:
                    raise ValueError("SECRET_PRESENT_IN_EVIDENCE")
                archive.writestr(path.name, raw)
                count += 1
    if not count:
        raise ValueError("NO_EVIDENCE_FILES")
    data = buffer.getvalue()
    if len(data) > MAX_TOTAL:
        raise ValueError("EVIDENCE_TOO_LARGE_FOR_LOG_TRANSPORT")
    encoded = base64.b64encode(data).decode("ascii")
    sha = hashlib.sha256(data).hexdigest()
    chunks = [encoded[i:i + 120] for i in range(0, len(encoded), 120)]
    print(f"{PREFIX}_BEGIN count={len(chunks)} sha256={sha} files={count}")
    for i, chunk in enumerate(chunks, 1):
        print(f"{PREFIX}_CHUNK {i}/{len(chunks)} {chunk}")
    print(f"{PREFIX}_END sha256={sha}")

def decode(log, output):
    match = re.search(rf"{PREFIX}_BEGIN count=(\d+) sha256=([0-9a-f]{{64}})", log)
    if not match:
        raise ValueError("MISSING_ARCHIVE_START")
    expected, sha = int(match.group(1)), match.group(2)
    pattern = re.compile(rf"{PREFIX}_CHUNK (\d+)/(\d+) ([A-Za-z0-9+/=]+)")
    chunks = {}
    for index, total, content in pattern.findall(log):
        if int(total) == expected:
            chunks[int(index)] = content
    if set(chunks) != set(range(1, expected + 1)):
        raise ValueError("INCOMPLETE_LOG_CHUNKS")
    data = base64.b64decode("".join(chunks[i] for i in range(1, expected + 1)), validate=True)
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("ARCHIVE_HASH_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if any(p.startswith("/") or ".." in Path(p).parts for p in archive.namelist()):
            raise ValueError("UNSAFE_ARCHIVE_PATH")
    output.write_bytes(data)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--encode", nargs="*", type=Path)
    parser.add_argument("--decode-log", type=Path)
    parser.add_argument("--output-zip", type=Path)
    args = parser.parse_args()
    if args.decode_log:
        if not args.output_zip:
            parser.error("--output-zip required")
        decode(args.decode_log.read_text(encoding="utf-8", errors="replace"), args.output_zip)
    else:
        encode(args.encode or [])

if __name__ == "__main__":
    main()
