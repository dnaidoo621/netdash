"""
Cudy M1800 stock-firmware client.

Cudy's firmware is LuCI underneath but with the ubus ACL stripped and the
standard controllers replaced. The web UI polls a handful of HTML fragments;
we log in the same way it does (sha256(sha256(pw+salt)+token)) and parse those.
Unofficial, so every parser degrades to "no data" rather than raising.
"""
import hashlib
import html
import re
import time

import httpx

BAND_RE = re.compile(r"(2\.4G WiFi|5G WiFi|Wired|LAN)")
MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
RATE_RE = re.compile(r"([\d.]+)\s*([KMG]?)bps", re.I)
SIG_RE = re.compile(r"(\d+)\s*dB")
DUR_RE = re.compile(r"((?:\d+ Day )?\d\d:\d\d:\d\d)")
SYSLOG_TS = re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2}) +(\d+) (\d\d):(\d\d):(\d\d) (\d{4})")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _text(fragment: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def _undouble(s: str) -> str:
    """Status cells render twice (desktop + mobile copy): 'DHCP client DHCP client' -> 'DHCP client'."""
    n = len(s)
    if n % 2 == 1 and s[n // 2] == " " and s[: n // 2] == s[n // 2 + 1:]:
        return s[: n // 2]
    return s


def _kbps(value: str, unit: str) -> float:
    v = float(value)
    return {"": v / 1000, "K": v, "M": v * 1000, "G": v * 1_000_000}.get(unit.upper(), v)


def _syslog_ts(line: str) -> int | None:
    m = SYSLOG_TS.match(line)
    if not m:
        return None
    mon, day, hh, mm, ss, yyyy = m.groups()
    return int(time.mktime((int(yyyy), MONTHS[mon], int(day), int(hh), int(mm), int(ss), 0, 0, -1)))


class Router:
    def __init__(self, url: str, password: str, username: str = "admin"):
        self.url = url.rstrip("/")
        self.password = password
        self.username = username
        self.client = httpx.AsyncClient(base_url=self.url, timeout=20, follow_redirects=True,
                                        headers={"X-Requested-With": "XMLHttpRequest",
                                                 "User-Agent": "netdash"})
        self.logged_in = False
        self.last_login = 0.0
        self.last_error: str | None = None

    # ------------------------------------------------------------ auth --
    async def login(self) -> bool:
        try:
            # A still-valid sysauth cookie makes the router serve the dashboard instead of
            # the login form (no salt/token), so a re-login must start from a clean jar.
            self.client.cookies.clear()
            r = await self.client.get("/cgi-bin/luci/")
            fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', r.text))
            salt, token, csrf = fields.get("salt", ""), fields.get("token", ""), fields.get("_csrf", "")
            if not (salt and token):
                self.last_error = "login page missing salt/token"
                return False
            sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
            r = await self.client.post("/cgi-bin/luci/", data={
                "_csrf": csrf, "token": token, "salt": salt,
                "zonename": "Africa/Johannesburg", "timeclock": str(int(time.time())),
                "luci_username": self.username,
                "luci_password": sha(sha(self.password + salt) + token),
            })
            self.logged_in = any("sysauth" in k.lower() for k in self.client.cookies.keys())
            self.last_login = time.time()
            self.last_error = None if self.logged_in else "no sysauth cookie after login"
            return self.logged_in
        except Exception as e:
            self.last_error = f"login: {e}"[:200]
            self.logged_in = False
            return False

    async def get(self, path: str) -> str | None:
        if not self.logged_in or time.time() - self.last_login > 1500:
            if not await self.login():
                return None
        try:
            r = await self.client.get(path)
            if r.status_code == 403 or "luci_password" in r.text[:3000]:
                if not await self.login():
                    return None
                r = await self.client.get(path)
            if r.status_code != 200:
                self.last_error = f"{path} -> {r.status_code}"
                return None
            return r.text
        except Exception as e:
            self.last_error = f"{path}: {e}"[:200]
            return None

    # ------------------------------------------------------ bandwidth --
    async def wan_counters(self, iface: str = "eth1.2") -> tuple[float, int, int] | None:
        """Latest (ts_seconds, rx_bytes, tx_bytes) sample from the router's own counter poll."""
        body = await self.get(f"/cgi-bin/luci/admin/status/bandwidth?iface={iface}")
        if not body:
            return None
        rows = re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\s*,\s*(\d+)\s*,\s*\d+\s*\]", body)
        if not rows:
            return None
        ts, rx, tx = rows[-1]
        return int(ts) / 1_000_000, int(rx), int(tx)

    # -------------------------------------------------------- devices --
    async def devices(self) -> list[dict]:
        body = await self.get("/cgi-bin/luci/admin/network/devices/devlist?detail=1")
        if not body:
            return []
        out = []
        for row in re.findall(r"<tr[^>]*data-sid=[^>]*>(.*?)</tr>", body, flags=re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
            if len(cells) < 6:
                continue
            mac = MAC_RE.search(row)
            if not mac:
                continue
            ip = IP_RE.search(_text(cells[4]) if len(cells) > 4 else row)
            # cell renders "<host><band> <host><band>" (desktop + mobile copies);
            # everything before the first band token is the hostname.
            name_cell = _text(cells[1])
            band = BAND_RE.search(name_cell)
            name = name_cell[: band.start()].strip() if band else name_cell
            # Rate cell: "<arrow-up> 28 Kbps <arrow-down> 20 Kbps" — anchor on the icon,
            # not on position, so the order can't bite again.
            rate_cell = cells[5] if len(cells) > 5 else row
            up_m = re.search(r"arrow-up.*?([\d.]+)\s*([KMG]?)bps", rate_cell, re.I | re.S)
            down_m = re.search(r"arrow-down.*?([\d.]+)\s*([KMG]?)bps", rate_cell, re.I | re.S)
            up = _kbps(*up_m.groups()) if up_m else 0.0
            down = _kbps(*down_m.groups()) if down_m else 0.0
            sig = SIG_RE.search(row)
            dur = DUR_RE.search(_text(cells[7]) if len(cells) > 7 else row)
            out.append({
                "mac": mac.group(1).upper(),
                "ip": ip.group(1) if ip else "",
                "name": "" if name.lower() == "unknown" else name,
                "band": band.group(1) if band else "",
                "down_kbps": round(down, 1),
                "up_kbps": round(up, 1),
                "signal_db": int(sig.group(1)) if sig else None,
                "duration": dur.group(1) if dur else "",
            })
        return out

    # --------------------------------------------------------- status --
    async def _kv(self, path: str) -> dict:
        body = await self.get(path)
        if not body:
            return {}
        kv = {}
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
            cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                kv[_undouble(cells[0])] = _undouble(cells[1])
        return kv

    async def wan_status(self) -> dict:
        return await self._kv("/cgi-bin/luci/admin/network/wan/status?detail=1")

    async def system(self) -> dict:
        return await self._kv("/cgi-bin/luci/admin/system/status?detail=1")

    async def mesh(self) -> dict:
        return await self._kv("/cgi-bin/luci/admin/network/mesh/status")

    # --------------------------------------------------------- syslog --
    async def syslog_events(self, since_ts: int) -> list[dict]:
        """WAN online/offline transitions and WiFi disconnects (with RSSI) newer than since_ts."""
        body = await self.get("/cgi-bin/luci/admin/system/status/syslog")
        if not body:
            return []
        log = _text(body)
        events = []
        for line in re.split(r"(?=(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} +\d+ \d\d:\d\d:\d\d \d{4})", log):
            ts = _syslog_ts(line)
            if ts is None or ts <= since_ts:
                continue
            m = re.search(r"Interface '?wan'? changed to (ONLINE|OFFLINE)", line)
            if m:
                events.append({"ts": ts, "kind": "wan_" + m.group(1).lower(),
                               "detail": f"Router pingcheck: WAN {m.group(1)}"})
                continue
            m = re.search(r"(ra\d+|rai\d+) disassoc: ([0-9A-F:]{17}), rssi: (\d+)", line)
            if m:
                events.append({"ts": ts, "kind": "wifi_drop",
                               "detail": f"{m.group(2)} dropped from {m.group(1)} at {m.group(3)} dB",
                               "mac": m.group(2), "rssi": int(m.group(3)),
                               "radio": "2.4G" if m.group(1).startswith("ra0") else "5G"})
        return events
