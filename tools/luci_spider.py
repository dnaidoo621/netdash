#!/usr/bin/env python3
"""Log in, then spider Cudy's custom LuCI tree to find the JSON data endpoints."""
import hashlib, http.cookiejar, json, os, re, time, urllib.parse, urllib.request
from collections import deque

ROUTER = os.environ.get("ROUTER_URL", "http://192.168.10.1")
PW = os.environ["ROUTER_PASSWORD"]
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [("User-Agent", "Mozilla/5.0 netdash")]


def fetch(path, data=None):
    req = urllib.request.Request(ROUTER + path,
                                 data=urllib.parse.urlencode(data).encode() if data else None)
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode(errors="replace")
    except Exception:
        return 0, "", ""


sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
_, _, page = fetch("/cgi-bin/luci/")
f = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', page))
fetch("/cgi-bin/luci/", {"_csrf": f["_csrf"], "token": f["token"], "salt": f["salt"],
                         "zonename": "Africa/Johannesburg", "timeclock": str(int(time.time())),
                         "luci_username": "admin",
                         "luci_password": sha(sha(PW + f["salt"]) + f["token"])})
assert any("sysauth" in c.name for c in cj), "login failed"
print("LOGIN OK")

LINK = re.compile(r'''(?:href|src|url|action|data-url|url:)\s*[=:]\s*["']?(/cgi-bin/luci/[^"'\s)<>]{2,120})''')
seen, queue, jsonish = set(), deque(["/cgi-bin/luci/"]), {}
while queue and len(seen) < 120:
    p = queue.popleft().split("?")[0].rstrip("/") or "/cgi-bin/luci"
    if p in seen or "logout" in p or "revert" in p or "reboot" in p or "reset" in p:
        continue
    seen.add(p)
    st, ct, body = fetch(p)
    if st != 200:
        continue
    b = body.lstrip()
    if b[:1] in "[{" or "json" in ct:
        jsonish[p] = b[:400]
    for m in LINK.findall(body):
        m = m.split("?")[0]
        if m not in seen:
            queue.append(m)

print(f"\ncrawled {len(seen)} pages\n")
print("=== ALL DISCOVERED PATHS ===")
for p in sorted(seen):
    print("  ", p)
print("\n=== JSON-LOOKING RESPONSES ===")
for p, b in jsonish.items():
    print(f"\n  {p}\n    {b[:380]}")
if not jsonish:
    print("  (none — data may be embedded in HTML or fetched via POST)")

# Mine the JS bundles Cudy ships for ajax paths
print("\n=== paths inside luci-static JS ===")
_, _, land = fetch("/cgi-bin/luci/")
hits = set()
for js in set(re.findall(r'src="(/luci-static/[^"]+\.js)[^"]*"', land)):
    _, _, code = fetch(js)
    for m in re.findall(r'["\'](/cgi-bin/luci/[^"\'\s]{4,100})["\']', code):
        hits.add(m)
    for m in re.findall(r'(admin/[a-z_]+/[a-z_/]{3,60})', code):
        hits.add("/cgi-bin/luci/" + m)
for h in sorted(hits)[:40]:
    print("  ", h)
