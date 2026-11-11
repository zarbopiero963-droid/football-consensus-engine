import json
import sys

text = open(sys.argv[1]).read()
line = [l for l in text.splitlines() if l.startswith("RESULT=")]
if not line:
    print("NO RESULT")
    print("\n".join(l for l in text.splitlines() if "Error" in l)[:2000])
    sys.exit(0)
d = json.loads(line[0][7:])
keys = ("usage_mb", "max_usage_mb", "rss_mb", "cache_mb", "shmem_mb", "failcnt", "pss_readable_mb", "chromium_left", "tools")
for k, v in d.items():
    if not isinstance(v, dict):
        print(k, v)
        continue
    print(k, {kk: (round(vv, 1) if isinstance(vv, float) else vv) for kk, vv in v.items() if kk in keys})
    if "by_comm_mb" in v:
        print("   by_comm", v["by_comm_mb"], "audit", v.get("audit"))
