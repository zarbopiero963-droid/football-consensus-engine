"use strict";
// Shared helpers. No external dependency, no inline script (CSP script-src 'self').
const fcb = {
  b64urlToBytes(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const bin = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  },
  bytesToB64url(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
  async postJSON(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body), credentials: "omit", cache: "no-store" });
    return { status: r.status, body: await r.json() };
  },
  text(el, value) { el.textContent = value; },  // textContent only (never HTML injection): labels come from data
};
