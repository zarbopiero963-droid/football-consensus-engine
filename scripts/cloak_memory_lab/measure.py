"""#189 memory lab: the whole bridge (all components registered) + one Cloak session, inside a
512 MB memory cgroup, Chromium under the uid split. Prints JSON with samples.
argv: <cgroup dir> <cloak binary> <page kind: typical|heavy>"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CG, BINARY, KIND = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ.update({"BROWSER_TOOLS_ENABLED": "true", "CLOAKBROWSER_ENABLED": "true",
                   "APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_API_KEY": "lab-not-a-key",
                   "SXBET_ENABLED": "true", "CLOAKBROWSER_BINARY_PATH": BINARY,
                   "CLOAKBROWSER_LAUNCHER": "/usr/local/bin/cloak-launch", "CLOAKBROWSER_AUTO_UPDATE": "false",
                   "MCP_OAUTH_PASSWORD": "lab", "SXBET_SIGNER_PROCESS": "1"})


def cg():
    def rd(name):
        try:
            return open(os.path.join(CG, name)).read()
        except OSError:
            return ""
    stat = dict(line.split() for line in rd("memory.stat").splitlines())
    return {"usage_mb": int(rd("memory.usage_in_bytes") or 0) / 2**20,
            "max_usage_mb": int(rd("memory.max_usage_in_bytes") or 0) / 2**20,
            "rss_mb": int(stat.get("total_rss", 0)) / 2**20,
            "cache_mb": int(stat.get("total_cache", 0)) / 2**20,
            "shmem_mb": int(stat.get("total_shmem", 0)) / 2**20,
            "failcnt": int(rd("memory.failcnt") or 0)}


def pss_tree():
    from bridge import browser_use
    pids = [os.getpid()] + browser_use._descendants(os.getpid())
    total, by = 0, {}
    for p in pids:
        try:
            for line in open(f"/proc/{p}/smaps_rollup"):
                if line.startswith("Pss:"):
                    kb = int(line.split()[1])
                    total += kb
                    comm = open(f"/proc/{p}/comm").read().strip()
                    by[comm] = by.get(comm, 0) + kb
        except OSError:
            pass  # other uid: not readable without ptrace rights
    return {"pss_readable_mb": total / 1024, "by_comm_mb": {k: round(v / 1024, 1) for k, v in by.items()}}


ROWS = {"light": 20, "typical": 3000}.get(KIND, 20000)
PAGE = ("<html><head><title>lab</title><style>td{padding:2px;border:1px solid #ccc}</style></head><body>"
        "<h1>Odds</h1><input id=q><button id=b onclick=\"document.title='clicked'\">go</button><table>"
        + "".join(f"<tr><td>Team {i} vs Team {i+1}</td><td>{1.5 + i % 7 / 10:.2f}</td><td>{3.1:.2f}</td>"
                  f"<td>{2.4:.2f}</td></tr>" for i in range(ROWS))
        + "</table><script>const keep=[];for(let i=0;i<" + {"light": "10", "typical": "2000"}.get(KIND, "40000")
        + ";i++){const d=document.createElement('div');d.textContent='live '+i;keep.push(d);"
        "if(i%10==0)document.body.appendChild(d)}"
        + ("" if KIND in ("light", "typical") else "window.big=new Array(4e6).fill(0).map((_,i)=>i);")
        + "</script></body></html>").encode()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


async def main():
    samples = {"start": cg()}
    from bridge import browser_use, server, session_leases
    tools = await server.mcp.list_tools()
    samples["bridge_loaded"] = {**cg(), "tools": len(tools)}
    port = srv.server_address[1]

    def validate(url):
        p = urlsplit(url)
        if p.hostname == "127.0.0.1" and p.port == port:
            return url
        raise browser_use.UnsafeURL("lab", "URL_NON_PUBLIC_DESTINATION")
    browser_use.validate_public_http_url = validate
    for n in ("register", "renew", "renew_stop", "finish"):
        setattr(session_leases, n, lambda *a, **k: None)
    session_leases.claim_stop = lambda *a, **k: True
    browser_use.CLOAK_PROFILES["pinned-146"] = browser_use.CloakProfile(
        "pinned-146", "146.0.7680.177.5", browser_use._sha256_file(BINARY), False)
    bridge = server.bridge
    await bridge.prepare_cloak_runtime()
    assert bridge._cloak_ready, bridge._cloak_failure
    samples["prepared"] = cg()
    peak_pss = 0
    for rnd in range(3):
        sid = (await bridge.create_cloak())["session_id"]
        samples[f"r{rnd}_created"] = cg()
        try:
            await bridge.open_url(sid, base + "/")
            await bridge.dom_text(sid, "h1")
            await bridge.type_text(sid, "#q", "abc")
            await bridge.click(sid, "#b")
            try:
                await bridge.screenshot(sid, os.environ.get("FULL_PAGE", "1") == "1")
                samples.setdefault("screenshot", []).append("ok")
            except browser_use.BrowserRefusal as exc:
                samples.setdefault("screenshot", []).append(exc.code)
                await bridge.screenshot(sid, False)
            await bridge.html(sid)
            await bridge.new_page(sid, base + "/?second")
            await bridge.elements(sid)
        except Exception as exc:
            samples.setdefault("errors", []).append(browser_use.browser_error_code(exc))
        await asyncio.sleep(1)
        s = {**cg(), **pss_tree(), "audit": bridge.cloak_audit}
        peak_pss = max(peak_pss, s["pss_readable_mb"])
        samples[f"r{rnd}_session"] = s
        await bridge.stop(sid)
        await asyncio.sleep(1)
        samples[f"r{rnd}_stopped"] = {**cg(), "chromium_left": len([
            p for p in browser_use._descendants(os.getpid())
            if (browser_use._proc_read(p, "comm") or b"").strip() == b"chrome"])}
    samples["end"] = cg()
    print("RESULT=" + json.dumps(samples))

asyncio.run(main())
