#!/usr/bin/env python3
import hashlib, http.cookiejar, os, re, time, urllib.parse, urllib.request

ROUTER = os.environ.get("ROUTER_URL", "http://192.168.10.1")
PW = os.environ["ROUTER_PASSWORD"]
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [("User-Agent", "Mozilla/5.0 netdash"), ("X-Requested-With", "XMLHttpRequest")]


def fetch(path, data=None):
    req = urllib.request.Request(ROUTER + path,
                                 data=urllib.parse.urlencode(data).encode() if data else None)
    try:
        with op.open(req, timeout=15) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as ex:
        return 0, str(ex)


sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
_, page = fetch("/cgi-bin/luci/")
f = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', page))
fetch("/cgi-bin/luci/", {"_csrf": f["_csrf"], "token": f["token"], "salt": f["salt"],
                         "zonename": "Africa/Johannesburg", "timeclock": str(int(time.time())),
                         "luci_username": "admin",
                         "luci_password": sha(sha(PW + f["salt"]) + f["token"])})
print("LOGIN OK" if any("sysauth" in c.name for c in cj) else "LOGIN FAILED")

for p in ["/cgi-bin/luci/admin/network/devices/devlist?detail=1",
          "/cgi-bin/luci/admin/network/devices/status?detail=1",
          "/cgi-bin/luci/admin/network/wireless/status?detail=1&iface=wlan10",
          "/cgi-bin/luci/admin/network/wan/status?detail=1",
          "/cgi-bin/luci/admin/system/status?detail=1"]:
    st, b = fetch(p)
    print(f"\n==== [{st}] {len(b)}B {p}")
    # show raw HTML of first table row-ish structure so I can write a parser
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", b, flags=re.S)
    print(f"  table rows: {len(rows)}")
    for r in rows[:4]:
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S)]
        print("   ", cells)
    if not rows:
        t = re.sub(r"<[^>]+>", " | ", re.sub(r"<script.*?</script>", "", b, flags=re.S))
        print("   ", re.sub(r"(\s*\|\s*)+", " | ", re.sub(r"\s+", " ", t))[:700])
    # any data-* attributes or json on the row (signal, band)?
    attrs = set(re.findall(r'(data-[a-z-]+|class)="([^"]{1,40})"', b))
    print("   attrs:", sorted({a for a, _ in attrs})[:12])
    print("   raw head:", re.sub(r"\s+", " ", b)[:500])
