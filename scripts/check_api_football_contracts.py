#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from bridge.api_football.registry import build_registry,sha256,validate_registry
def flat(r):
 o={}
 for e in r.get("endpoints",[]):
  o["endpoint:"+e["id"]]=e
  for p in e.get("parameters",[]): o["parameter:"+p["id"]]=p
 return o
def diff(old,new):
 a,b=flat(old),flat(new); out=[]
 for k in sorted(set(a)|set(b)):
  if k not in a: out.append({"key":k,"classification":"ADDITIVE","kind":"added"})
  elif k not in b: out.append({"key":k,"classification":"BREAKING","kind":"removed"})
  else:
   x,y=a[k],b[k]
   for f in ("required","type","enum","minimum","maximum","response_schema"):
    if x.get(f)!=y.get(f):
     c="UNKNOWN"
     if f in ("type","minimum","maximum","response_schema") or (f=="required" and y.get(f)): c="BREAKING"
     if f=="enum" and set(y.get(f,[]))<set(x.get(f,[])): c="BREAKING"
     out.append({"key":k,"classification":c,"kind":f+"_changed"})
   if x.get("notes")!=y.get("notes"): out.append({"key":k,"classification":"DOCUMENTATION_ONLY","kind":"notes_changed"})
 return out
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--write",type=Path); ap.add_argument("--snapshot",type=Path); args=ap.parse_args()
 cur=build_registry(); err=validate_registry(cur)
 if err: print("\n".join(err)); return 2
 if args.write:
  args.write.parent.mkdir(parents=True,exist_ok=True); args.write.write_text(json.dumps({"registry":cur,"sha256":sha256(cur)},indent=2,sort_keys=True)+"\n")
 if args.snapshot and args.snapshot.exists():
  ch=diff(json.loads(args.snapshot.read_text())["registry"],cur); print(json.dumps({"changes":ch},indent=2))
  if any(x["classification"] in {"BREAKING","UNKNOWN"} for x in ch): return 2
 else: print(json.dumps({"sha256":sha256(cur),"endpoints":len(cur["endpoints"]),"errors":[]}))
 return 0
if __name__=="__main__": raise SystemExit(main())
