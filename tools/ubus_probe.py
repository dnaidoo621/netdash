#!/usr/bin/env python3
"""Authenticate to the Cudy router's ubus and enumerate what's actually reachable."""
import json, os, sys, urllib.request

ROUTER = os.environ.get("ROUTER_URL", "http://192.168.10.1")
PW = os.environ["ROUTER_PASSWORD"]
NULL = "0" * 32


def ubus(sid, obj, method, args=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "call",
                       "params": [sid, obj, method, args or {}]}).encode()
    req = urllib.request.Request(ROUTER + "/ubus", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


# ---- login: try the usernames stock LuCI-derived firmwares use
sid = None
for user in ("root", "admin"):
    r = ubus(NULL, "session", "login", {"username": user, "password": PW})
    res = r.get("result")
    if isinstance(res, list) and res and res[0] == 0:
        sid = res[1]["ubus_rpc_session"]
        print(f"LOGIN OK as '{user}'  expires={res[1].get('expires')}s")
        acl = res[1].get("acls", {})
        print("  ubus ACL objects granted:")
        for obj, methods in sorted(acl.get("ubus", {}).items()):
            print(f"    {obj:28} {methods}")
        break
    else:
        print(f"login as '{user}': {r.get('result') or r.get('error')}")
if not sid:
    sys.exit("no session")

print()
print("==== live probes ====")
probes = [
    ("system", "board", {}),
    ("system", "info", {}),
    ("network.interface", "dump", {}),
    ("network.device", "status", {}),
    ("iwinfo", "devices", {}),
    ("luci-rpc", "getDHCPLeases", {}),
    ("luci-rpc", "getWirelessDevices", {}),
    ("luci-rpc", "getNetworkDevices", {}),
    ("luci", "getConntrackList", {}),
    ("file", "read", {"path": "/proc/net/dev"}),
]
ok = {}
for obj, method, args in probes:
    r = ubus(sid, obj, method, args)
    res = r.get("result")
    if isinstance(res, list) and res and res[0] == 0:
        payload = res[1] if len(res) > 1 else {}
        ok[(obj, method)] = payload
        print(f"  OK   {obj}.{method}  -> {str(payload)[:110]}")
    else:
        code = (r.get("error") or {}).get("message") if isinstance(r.get("error"), dict) else r.get("error")
        print(f"  --   {obj}.{method}  ({code or res})")

# ---- if iwinfo is open, pull the actual association list per radio
devs = ok.get(("iwinfo", "devices"), {}).get("devices", [])
if devs:
    print()
    print("==== WiFi clients per radio ====")
    for d in devs:
        r = ubus(sid, "iwinfo", "assoclist", {"device": d})
        res = r.get("result")
        if isinstance(res, list) and res and res[0] == 0:
            lst = res[1].get("results", [])
            info = ubus(sid, "iwinfo", "info", {"device": d}).get("result", [0, {}])[1]
            print(f"  {d}: ssid={info.get('ssid')} ch={info.get('channel')} "
                  f"{info.get('hwmodes')} clients={len(lst)}")
            for c in lst[:12]:
                rx = c.get("rx", {}); tx = c.get("tx", {})
                print(f"     {c.get('mac')}  signal={c.get('signal')}dBm  "
                      f"rx={rx.get('rate',0)//1000}Mb tx={tx.get('rate',0)//1000}Mb  "
                      f"inactive={c.get('inactive')}ms")
        else:
            print(f"  {d}: assoclist denied")
