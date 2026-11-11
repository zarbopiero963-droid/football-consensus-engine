#!/bin/bash
# #189 uid-split LAB (CI or a disposable machine; never Render). Reproduces the layout of
# deploy/cloak-uid-split/Dockerfile on the host and runs the uid-split end-to-end tests as the
# bridge uid with the same ambient capabilities as the image entrypoint.
#
#   sudo [FCB_LAB_PYTHON=/path/to/python] scripts/cloak_uid_split_lab.sh <chrome binary> [pytest args...]
#
# Creates users fcbapp (bridge) and cloak (Chromium), copies the binary to /opt/cloak (root-owned),
# installs the launcher to /usr/local/bin/cloak-launch. Idempotent. Needs root, util-linux setpriv.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (lab only)" >&2; exit 2; }
src_bin=$1; shift
repo=$(cd "$(dirname "$0")/.." && pwd)
dir_name=$(basename "$(dirname "$(readlink -f "$src_bin")")")

id fcbapp >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin fcbapp
id cloak >/dev/null 2>&1 || useradd -M -d /var/lib/cloak -s /usr/sbin/nologin cloak
install -d -o cloak -g cloak -m 700 /var/lib/cloak
install -d -o root -g root -m 755 /opt/cloak
rm -rf "/opt/cloak/$dir_name"
cp -a "$(dirname "$(readlink -f "$src_bin")")" "/opt/cloak/$dir_name"
chown -R root:root /opt/cloak && chmod -R a+rX,go-w /opt/cloak
install -o root -g root -m 755 "$repo/deploy/cloak-uid-split/cloak-launch" /usr/local/bin/cloak-launch
# A root-owned, read-only copy of the code, like /app in the image (the checkout may sit in a home
# directory the bridge uid cannot read).
lab=/opt/fcb-uid-split-lab
rm -rf "$lab" && install -d -m 755 "$lab/repo"
cp -a "$repo/bridge" "$repo/app" "$repo/tests" "$repo/deploy" "$repo/conftest.py" "$lab/repo/" 2>/dev/null \
  || cp -a "$repo/bridge" "$repo/app" "$repo/tests" "$repo/deploy" "$lab/repo/"
chown -R root:root "$lab" && chmod -R a+rX,go-w "$lab"
repo="$lab/repo"
python=${FCB_LAB_PYTHON:-$(command -v python3)}

work=$(mktemp -d /tmp/fcb-uid-split-XXXXXX)
chown fcbapp:fcbapp "$work"
exec setpriv --reuid=fcbapp --regid=fcbapp --init-groups \
  --inh-caps=+setuid,+setgid,+kill --ambient-caps=+setuid,+setgid,+kill \
  env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/fcbapp LANG=C.UTF-8 TMPDIR="$work" \
  PYTHONPATH="$repo" SXBET_SIGNER_PROCESS=1 PYTHONDONTWRITEBYTECODE=1 \
  FCB_CLOAK_UID_SPLIT=1 FCB_CLOAK_CHROMIUM="/opt/cloak/$dir_name/chrome" \
  CLOAKBROWSER_LAUNCHER=/usr/local/bin/cloak-launch \
  PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-}" \
  bash -c 'cd "$1"; py=$2; shift 2; exec "$py" -m pytest -p no:cacheprovider -q "$@"' _ "$repo" "$python" \
  tests/test_cloak_uid_split_e2e.py "$@"
