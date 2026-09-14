#!/usr/bin/env python3
"""Real Cudy/LuCI login (sha256(sha256(pw+salt)+token)) then probe the status endpoints."""
import hashlib, http.cookiejar, json, os, re, time, urllib.parse, urllib.request

ROUTER = os.environ.get("ROUTER_URL", "http://192.168.10.1")
PW = os.environ["ROUTER_PASSWORD"]

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [("User-Agent", "Mozilla/5.0 netdash")]


def get(path):
    try:
        with op.open(ROUTER + path, timeout=10) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(ROUTER + path, data=body)
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def sha(s): return hashlib.sha256(s.encode()).hexdigest()


# 1. fetch login page in this session to get csrf/salt/token
st, page = get("/cgi-bin/luci/")
f = {n: v for n, v in re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', page)}
print("login page fields:", {k: (v[:8] + "…" if v else "") for k, v in f.items()})
hashed = sha(sha(PW + f["salt"]) + f["token"])
st, body = post("/cgi-bin/luci/", {
    "_csrf": f["_csrf"], "token": f["token"], "salt": f["salt"],
    "zonename": "Africa/Johannesburg", "timeclock": str(int(time.time())),
    "luci_username": "admin", "luci_password": hashed,
})
cookies = [c.name for c in cj]
print(f"login POST: HTTP {st}, cookies={cookies}")
if not any("sysauth" in c.lower() for c in cookies):
    print("no sysauth cookie — login failed; first 300 bytes:", body[:300])
    raise SystemExit(1)
print("LOGIN OK\n")

# 2. probe the classic LuCI JSON status endpoints
tests = [
    "/cgi-bin/luci/admin/status/realtime/bandwidth_status/wan",
    "/cgi-bin/luci/admin/status/realtime/bandwidth_status/eth0",
    "/cgi-bin/luci/admin/status/realtime/bandwidth_status/br-lan",
    "/cgi-bin/luci/admin/status/realtime/load_status",
    "/cgi-bin/luci/admin/network/dhcplease_status",
    "/cgi-bin/luci/admin/network/wireless_assoclist",
    "/cgi-bin/luci/admin/status/realtime/wireless_status/wlan0",
    "/cgi-bin/luci/admin/status/realtime/wireless_status/ra0",
    "/cgi-bin/luci/admin/network/iface_status/wan",
    "/cgi-bin/luci/admin/network/iface_status/lan",
    "/cgi-bin/luci/admin/status/overview",
]
for p in tests:
    st, body = get(p)
    b = body.strip()
    kind = "json" if b[:1] in "[{" else ("html" if "<" in b[:20] else "text")
    print(f"  {st:>3} {kind:4} {p}")
    if st == 200 and kind == "json":
        try:
            j = json.loads(b)
            print("        ->", json.dumps(j)[:220])
        except Exception:
            print("        ->", b[:160])

# 3. the overview page itself often embeds the WAN/wireless device names — mine it
st, body = get("/cgi-bin/luci/admin/status/overview")
if st == 200:
    print("\noverview page: device names mentioned:",
          sorted(set(re.findall(r'\b(eth\d(?:\.\d)?|wan\d?|wlan\d|ra\d|rai\d|apcli\d|phy\d|radio\d|br-lan)\b', body)))[:20])
    print("overview: data urls:", sorted(set(re.findall(r'(/cgi-bin/luci/[^"\'\s]{6,90})', body)))[:20])
