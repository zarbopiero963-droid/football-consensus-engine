# CloakBrowser uid split (#189). PREPARED, NOT APPLIED. Using it switches the existing Render web
# service from the native Python runtime to Docker (same service, same plan, no extra service).
# That is a production change: the owner applies it only through the canary and rollback in
# docs/certification/areas/cloak_hardening.md. Build from the repository root:
#   docker build -f deploy/cloak-uid-split/Dockerfile .
#
# Why: on the native runtime the bridge and Chromium share one uid, so a compromised browser
# process could read or rewrite what the bridge owns. Here the entrypoint starts as root only to
# split identities, then never runs anything as root again:
#   app   (uid 10001) runs the MCP bridge and holds the secrets (env). It keeps CAP_SETUID/SETGID
#                     (to start Chromium as `cloak`) and CAP_KILL (to stop it) as ambient caps;
#   cloak (uid 10002) runs Chromium with a minimal env, no capabilities, its own profile dir.
# The source tree is root-owned and read-only for both. Chromium's sandbox stays ON; if Render
# refuses unprivileged user namespaces, Cloak fails closed with CLOAK_SANDBOX_UNAVAILABLE (the
# bridge checks every renderer in /proc). Never --no-sandbox.
FROM python:3.13-slim-bookworm

RUN groupadd -g 10001 app && useradd -u 10001 -g app -m -d /home/app -s /usr/sbin/nologin app \
 && groupadd -g 10002 cloak && useradd -u 10002 -g cloak -M -d /var/lib/cloak -s /usr/sbin/nologin cloak \
 && install -d -o cloak -g cloak -m 700 /var/lib/cloak

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install-deps chromium \
 && apt-get update && apt-get install -y --no-install-recommends util-linux \
 && rm -rf /var/lib/apt/lists/*

# The pinned keyless Cloak binary (cloakbrowser==0.5.11 -> Chromium 146.0.7680.177.5) is fetched at
# BUILD time (cloakbrowser checks its Ed25519-signed SHA256SUMS), then checked against the same
# SHA-256 pin the bridge enforces at startup. No download and no update at runtime.
ENV CLOAKBROWSER_CACHE_DIR=/opt/cloak CLOAKBROWSER_AUTO_UPDATE=false
RUN env -u CLOAKBROWSER_LICENSE_KEY python -c "from cloakbrowser import ensure_binary; print(ensure_binary(browser_version='146.0.7680.177.5'))" \
 && echo "715722e8605ae3ce81523c1218aba1ec89425786ab33ceaf99f8a6cb5e70e6e8  /opt/cloak/chromium-146.0.7680.177.5/chrome" | sha256sum -c - \
 && chown -R root:root /opt/cloak && chmod -R a+rX,go-w /opt/cloak

COPY . /app
RUN chown -R root:root /app && chmod -R a+rX,go-w /app \
 && install -o root -g root -m 755 deploy/cloak-uid-split/cloak-launch /usr/local/bin/cloak-launch
ENV CLOAKBROWSER_LAUNCHER=/usr/local/bin/cloak-launch HOME=/home/app

# Entry as root only to drop to `app`: the bridge never runs as root.
CMD ["sh", "-c", "exec setpriv --reuid=app --regid=app --init-groups --inh-caps=+setuid,+setgid,+kill --ambient-caps=+setuid,+setgid,+kill uvicorn bridge.server:app --host 0.0.0.0 --port \"${PORT:-10000}\""]
