#!/bin/bash
# Lab only (#189): run as root on a disposable host after scripts/cloak_uid_split_lab.sh has created
# the users, /opt/cloak and the launcher. Never on Render.
# usage: run_measure.sh <repo> <binary under /opt/cloak> <typical|heavy> <limit MB>
set -eu
repo=$1 bin=$2 kind=$3 limit=$4
CG=/sys/fs/cgroup/memory/fcblab-$kind-$limit
rmdir "$CG" 2>/dev/null || true
mkdir -p "$CG"
echo $((limit*1024*1024)) > "$CG/memory.limit_in_bytes"
echo $((limit*1024*1024)) > "$CG/memory.memsw.limit_in_bytes" 2>/dev/null || true
echo 0 > "$CG/memory.max_usage_in_bytes" || true
echo $$ > "$CG/cgroup.procs"
work=$(mktemp -d /tmp/fcb-mem-XXXXXX); chown fcbapp:fcbapp "$work"
exec setpriv --reuid=fcbapp --regid=fcbapp --init-groups \
  --inh-caps=+setuid,+setgid,+kill --ambient-caps=+setuid,+setgid,+kill \
  env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/fcbapp LANG=C.UTF-8 TMPDIR="$work" \
  FULL_PAGE="${FULL_PAGE:-1}" PYTHONPATH="$repo" python3 "$repo/scripts/cloak_memory_lab/measure.py" "$CG" "$bin" "$kind"
