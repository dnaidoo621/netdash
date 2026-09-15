"""
netdash — single-glance home network dashboard for the HTPC.

Collects WAN health (latency / jitter / loss / throughput) and machine health
into SQLite on a timer, proxies Pi-hole's API live, and serves one static page.
Everything is local so the page still works when the internet is down — which
is precisely when you'll be looking at it.
"""
import asyncio
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from router import Router

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "netdash.db"
STATIC = BASE / "static"

PIHOLE_URL = os.environ.get("PIHOLE_URL", "https://127.0.0.1")
PIHOLE_PASSWORD = os.environ.get("PIHOLE_PASSWORD", "")
GATEWAY = os.environ.get("GATEWAY", "192.168.10.1")
ISP_BOX = os.environ.get("ISP_BOX", "192.168.1.1")
INTERNET = os.environ.get("INTERNET_TARGET", "1.1.1.1")
IFACE = os.environ.get("IFACE", "enp2s0")
PROBE_INTERVAL = int(os.environ.get("PROBE_INTERVAL", "60"))
THROUGHPUT_INTERVAL = int(os.environ.get("THROUGHPUT_INTERVAL", "900"))
THROUGHPUT_URL = os.environ.get(
    "THROUGHPUT_URL", "https://speed.cloudflare.com/__down?bytes=5000000"
)
# Raw samples are kept RAW_DAYS; hourly rollups are kept RETENTION_DAYS. Anything the
# UI asks for beyond RAW_DAYS is served from the rollups.
RAW_DAYS = int(os.environ.get("RAW_DAYS", "14"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
MAX_HOURS = RETENTION_DAYS * 24
# What you pay for. Set it and the throughput tile and monthly report say "x% of plan".
PLAN_DOWN_MBPS = float(os.environ.get("PLAN_DOWN_MBPS", "0") or 0)
ROUTER_URL = os.environ.get("ROUTER_URL", f"http://{GATEWAY}")
ROUTER_PASSWORD = os.environ.get("ROUTER_PASSWORD", "")
ROUTER_WAN_IFACE = os.environ.get("ROUTER_WAN_IFACE", "eth1.2")
SYSLOG_INTERVAL = int(os.environ.get("SYSLOG_INTERVAL", "300"))

SERVICE_INTERVAL = int(os.environ.get("SERVICE_INTERVAL", "120"))

TARGETS = {"gateway": GATEWAY, "isp": ISP_BOX, "internet": INTERNET}

# Service catalog: (name, check URL, Pi-hole domain patterns). Which of these get
# checked is decided automatically from what the house actually resolves — see
# Collector.detect_services(). A services.json next to app.py pins extra entries
# (or overrides a URL) and is always checked regardless of traffic.
SERVICE_CATALOG = [
    ("Netflix",     "https://www.netflix.com/",            ["netflix", "nflx"]),
    ("YouTube",     "https://www.youtube.com/",            ["youtube", "googlevideo", "ytimg"]),
    ("Google",      "https://www.google.com/generate_204", ["google.com", "gstatic", "googleapis"]),
    ("GitHub",      "https://github.com/",                 ["github"]),
    ("iCloud",      "https://www.icloud.com/",             ["icloud"]),
    ("Apple",       "https://www.apple.com/",              ["apple.com", "apple-dns", "mzstatic"]),
    ("Teams",       "https://teams.microsoft.com/",        ["teams.microsoft"]),
    ("Microsoft",   "https://www.microsoft.com/",          ["microsoft", "msftconnecttest", "office.com", "live.com", "hotmail", "outlook"]),
    ("WhatsApp",    "https://web.whatsapp.com/",           ["whatsapp"]),
    ("Stremio",     "https://app.strem.io/",               ["strem.io", "strem.fun"]),
    ("Claude",      "https://claude.ai/",                  ["claude.ai", "anthropic"]),
    ("OpenAI",      "https://chatgpt.com/",                ["openai", "chatgpt"]),
    ("Spotify",     "https://open.spotify.com/",           ["spotify", "scdn.co"]),
    ("Prime Video", "https://www.primevideo.com/",         ["primevideo", "aiv-cdn", "amazonvideo"]),
    ("Amazon",      "https://www.amazon.com/",             ["amazon.com", "amazon.co"]),
    ("Disney+",     "https://www.disneyplus.com/",         ["disneyplus", "disney-plus", "bamgrid"]),
    ("Showmax",     "https://www.showmax.com/",            ["showmax"]),
    ("DStv",        "https://www.dstv.com/",               ["dstv", "multichoice"]),
    ("Facebook",    "https://www.facebook.com/",           ["facebook", "fbcdn"]),
    ("Instagram",   "https://www.instagram.com/",          ["instagram", "cdninstagram"]),
    ("TikTok",      "https://www.tiktok.com/",             ["tiktok", "byteoversea"]),
    ("X",           "https://x.com/",                      ["twitter", "twimg"]),
    ("Reddit",      "https://www.reddit.com/",             ["reddit", "redd.it"]),
    ("Twitch",      "https://www.twitch.tv/",              ["twitch", "ttvnw"]),
    ("Slack",       "https://slack.com/",                  ["slack.com", "slack-edge"]),
    ("Zoom",        "https://zoom.us/",                    ["zoom.us"]),
    ("Discord",     "https://discord.com/",                ["discord"]),
    ("Telegram",    "https://web.telegram.org/",           ["telegram"]),
    ("Steam",       "https://store.steampowered.com/",     ["steampowered", "steamcontent", "steamstatic"]),
    ("PlayStation", "https://www.playstation.com/",        ["playstation"]),
    ("Xbox",        "https://www.xbox.com/",               ["xbox"]),
    ("Plex",        "https://app.plex.tv/",                ["plex.tv"]),
    ("Dropbox",     "https://www.dropbox.com/",            ["dropbox"]),
    ("Tailscale",   "https://login.tailscale.com/",        ["tailscale"]),
    ("Cloudflare",  "https://www.cloudflare.com/",         ["cloudflare"]),
]
CATALOG_BY_NAME = {n: (u, p) for n, u, p in SERVICE_CATALOG}
# Fallback when Pi-hole isn't available to tell us what's in use.
DEFAULT_SERVICES = [(n, u) for n, u, _ in SERVICE_CATALOG[:11]]
AUTO_DETECT_DAYS = int(os.environ.get("AUTO_DETECT_DAYS", "14"))
AUTO_DETECT_MIN_QUERIES = int(os.environ.get("AUTO_DETECT_MIN_QUERIES", "25"))


def load_pinned() -> list[tuple[str, str]]:
    p = BASE / "services.json"
    if not p.exists():
        return []
    try:
        import json
        return [(s["name"], s.get("url") or CATALOG_BY_NAME.get(s["name"], ("",))[0])
                for s in json.loads(p.read_text())]
    except Exception:
        return []


PINNED_SERVICES = load_pinned()

# curl exit codes worth naming — everything else is reported by number.
CURL_REASON = {6: "DNS failed", 7: "connection refused", 28: "timeout",
               35: "TLS handshake failed", 56: "connection reset", 60: "bad certificate"}

# ---------------------------------------------------------------- storage --

SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    ts INTEGER NOT NULL, target TEXT NOT NULL,
    sent INTEGER, recv INTEGER, loss REAL,
    rtt_avg REAL, rtt_max REAL, jitter REAL
);
CREATE INDEX IF NOT EXISTS ix_probes ON probes(target, ts);
CREATE TABLE IF NOT EXISTS throughput (ts INTEGER NOT NULL, mbps REAL);
CREATE TABLE IF NOT EXISTS machine (
    ts INTEGER NOT NULL, load1 REAL, pkg_temp REAL, fan INTEGER,
    mem_avail_mb INTEGER, swap_free_mb INTEGER, rx_mbps REAL, tx_mbps REAL
);
CREATE TABLE IF NOT EXISTS events (ts INTEGER NOT NULL, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_events ON events(ts);
CREATE TABLE IF NOT EXISTS router_wan (ts INTEGER NOT NULL, rx_mbps REAL, tx_mbps REAL);
CREATE TABLE IF NOT EXISTS router_devices (
    ts INTEGER NOT NULL, mac TEXT NOT NULL, ip TEXT, name TEXT, band TEXT,
    signal_db INTEGER, down_kbps REAL, up_kbps REAL
);
CREATE INDEX IF NOT EXISTS ix_rdev ON router_devices(mac, ts);
CREATE TABLE IF NOT EXISTS wifi_drops (ts INTEGER NOT NULL, mac TEXT, radio TEXT, rssi INTEGER);
CREATE INDEX IF NOT EXISTS ix_wdrops ON wifi_drops(ts);
CREATE TABLE IF NOT EXISTS services (
    ts INTEGER NOT NULL, name TEXT NOT NULL, ok INTEGER, status INTEGER,
    dns_ms REAL, connect_ms REAL, ttfb_ms REAL, reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_services ON services(name, ts);
CREATE TABLE IF NOT EXISTS service_seen (
    name TEXT PRIMARY KEY, last_seen INTEGER NOT NULL, queries INTEGER
);
-- Friendly device names: manual overrides and mDNS discoveries.
CREATE TABLE IF NOT EXISTS device_names (
    mac TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, updated INTEGER NOT NULL
);
-- Hourly rollups. Column names match the raw tables where the UI reads them.
CREATE TABLE IF NOT EXISTS probes_h (
    ts INTEGER NOT NULL, target TEXT NOT NULL, n INTEGER,
    loss REAL, loss_max REAL, rtt_avg REAL, rtt_max REAL, jitter REAL, outages INTEGER,
    PRIMARY KEY (ts, target)
);
CREATE TABLE IF NOT EXISTS router_wan_h (
    ts INTEGER PRIMARY KEY, rx_mbps REAL, rx_max REAL, tx_mbps REAL, tx_max REAL
);
CREATE TABLE IF NOT EXISTS machine_h (
    ts INTEGER PRIMARY KEY, load1 REAL, pkg_temp REAL, temp_max REAL, fan INTEGER,
    mem_avail_mb INTEGER, swap_free_mb INTEGER, rx_mbps REAL, tx_mbps REAL
);
CREATE TABLE IF NOT EXISTS services_h (
    ts INTEGER NOT NULL, name TEXT NOT NULL, checks INTEGER, ok INTEGER, ttfb_ms REAL,
    PRIMARY KEY (ts, name)
);
CREATE TABLE IF NOT EXISTS router_devices_h (
    ts INTEGER NOT NULL, mac TEXT NOT NULL, sig_min INTEGER, sig_avg REAL, sig_max INTEGER,
    down_avg REAL, down_max REAL, up_avg REAL, up_max REAL,
    PRIMARY KEY (ts, mac)
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)


def add_event(kind: str, detail: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO events(ts, kind, detail) VALUES (?,?,?)",
            (int(time.time()), kind, detail),
        )


def rollup(backfill: bool = False):
    """Aggregate raw samples into hourly rows. Re-does the last 3 complete hours each run,
    so late samples are folded in; INSERT OR REPLACE keeps it idempotent. backfill=True
    (startup) covers every complete hour present in the raw tables."""
    now = int(time.time())
    end = now - now % 3600            # current, incomplete hour is excluded
    start = end - 3 * 3600
    if backfill:
        with db() as conn:
            r = conn.execute("SELECT MIN(ts) m FROM probes").fetchone()
        start = (r["m"] // 3600) * 3600 if r and r["m"] else start
    with db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO probes_h
            SELECT (ts/3600)*3600, target, COUNT(*), AVG(loss), MAX(loss),
                   AVG(rtt_avg), MAX(rtt_max), AVG(jitter), SUM(loss >= 100)
            FROM probes WHERE ts >= ? AND ts < ? GROUP BY 1, 2""", (start, end))
        conn.execute("""
            INSERT OR REPLACE INTO router_wan_h
            SELECT (ts/3600)*3600, AVG(rx_mbps), MAX(rx_mbps), AVG(tx_mbps), MAX(tx_mbps)
            FROM router_wan WHERE ts >= ? AND ts < ? GROUP BY 1""", (start, end))
        conn.execute("""
            INSERT OR REPLACE INTO machine_h
            SELECT (ts/3600)*3600, AVG(load1), AVG(pkg_temp), MAX(pkg_temp), AVG(fan),
                   AVG(mem_avail_mb), AVG(swap_free_mb), AVG(rx_mbps), AVG(tx_mbps)
            FROM machine WHERE ts >= ? AND ts < ? GROUP BY 1""", (start, end))
        conn.execute("""
            INSERT OR REPLACE INTO services_h
            SELECT (ts/3600)*3600, name, COUNT(*), SUM(ok), AVG(ttfb_ms)
            FROM services WHERE ts >= ? AND ts < ? GROUP BY 1, 2""", (start, end))
        conn.execute("""
            INSERT OR REPLACE INTO router_devices_h
            SELECT (ts/3600)*3600, mac, MIN(signal_db), AVG(signal_db), MAX(signal_db),
                   AVG(down_kbps), MAX(down_kbps), AVG(up_kbps), MAX(up_kbps)
            FROM router_devices WHERE ts >= ? AND ts < ? GROUP BY 1, 2""", (start, end))


def prune():
    now = int(time.time())
    raw_cut = now - RAW_DAYS * 86400
    long_cut = now - RETENTION_DAYS * 86400
    with db() as conn:
        for t in ("probes", "machine", "router_wan", "router_devices", "services"):
            conn.execute(f"DELETE FROM {t} WHERE ts < ?", (raw_cut,))
        for t in ("throughput", "events", "wifi_drops", "probes_h", "router_wan_h",
                  "machine_h", "services_h", "router_devices_h"):
            conn.execute(f"DELETE FROM {t} WHERE ts < ?", (long_cut,))


def raw_ok(hours: float) -> bool:
    """Serve raw samples for short windows, hourly rollups for long ones."""
    return hours <= RAW_DAYS * 24


# ------------------------------------------------------------- collectors --

PING_SUMMARY = re.compile(
    r"(\d+) packets transmitted, (\d+) received.*?([\d.]+)% packet loss"
)
PING_RTT = re.compile(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)")


async def run(cmd: list[str], timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace")


async def ping(target: str, count: int = 10) -> dict:
    out = await run(
        ["ping", "-n", "-c", str(count), "-i", "0.2", "-W", "1", target],
        timeout=count * 0.2 + 5,
    )
    m = PING_SUMMARY.search(out)
    if not m:
        return {"sent": count, "recv": 0, "loss": 100.0,
                "rtt_avg": None, "rtt_max": None, "jitter": None}
    sent, recv, loss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    r = PING_RTT.search(out)
    rtt = {"rtt_avg": None, "rtt_max": None, "jitter": None}
    if r:
        rtt = {"rtt_avg": float(r.group(2)), "rtt_max": float(r.group(3)),
               "jitter": float(r.group(4))}
    return {"sent": sent, "recv": recv, "loss": loss, **rtt}


async def throughput_mbps() -> float | None:
    out = await run(
        ["curl", "-o", "/dev/null", "-s", "--max-time", "40",
         "-w", "%{speed_download}", THROUGHPUT_URL],
        timeout=45,
    )
    try:
        bps = float(out.strip())
    except ValueError:
        return None
    return round(bps * 8 / 1_000_000, 1) if bps > 0 else None


async def check_service(name: str, url: str) -> dict:
    """One HTTP check with the timing split by phase, so a failure says *where* it failed."""
    out = await run(
        ["curl", "-o", "/dev/null", "-s", "-L", "--max-time", "12", "-A", "netdash/1.0",
         "-w", "%{http_code} %{time_namelookup} %{time_connect} %{time_starttransfer} %{exitcode}",
         url],
        timeout=15,
    )
    parts = out.strip().split()
    row = {"name": name, "ok": 0, "status": 0, "dns_ms": None, "connect_ms": None,
           "ttfb_ms": None, "reason": "timeout"}
    if len(parts) < 5:
        return row
    try:
        status = int(parts[0]); exitcode = int(parts[4])
        dns, conn, ttfb = (float(parts[i]) * 1000 for i in (1, 2, 3))
    except ValueError:
        return row
    row.update(status=status, dns_ms=round(dns, 1), connect_ms=round(conn, 1),
               ttfb_ms=round(ttfb, 1))
    if exitcode:
        row["reason"] = CURL_REASON.get(exitcode, f"curl error {exitcode}")
    elif status >= 500:
        row["reason"] = f"HTTP {status}"
    else:
        # Any real answer below 500 means the service is reachable. Sites like claude.ai
        # hand curl a 403 from bot protection — that's still "up" for our purposes.
        row["ok"], row["reason"] = 1, ""
    return row


async def mdns_name(ip: str) -> str | None:
    """Reverse mDNS lookup: Apple devices, Linux boxes and smart TVs answer with
    'Darrens-iPhone.local'. IoT gear and most Android phones don't."""
    out = await run(["avahi-resolve-address", ip], timeout=4)
    parts = out.split()
    if len(parts) < 2 or not parts[1].endswith(".local"):
        return None
    name = parts[1][:-len(".local")].replace("-", " ").strip()
    return name if name and name.lower() not in ("localhost",) else None


def name_map() -> dict[str, dict]:
    return {r["mac"]: dict(r) for r in rows("SELECT mac,name,source,updated FROM device_names")}


def best_name(mac: str, *candidates: str | None, names: dict[str, dict] | None = None) -> str:
    """manual > mDNS > whatever the router/Pi-hole said."""
    n = (names if names is not None else name_map()).get((mac or "").upper())
    if n:
        return n["name"]
    for c in candidates:
        if c and c.lower() != "unknown":
            return c
    return ""


def read_pkg_temp() -> float | None:
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            if (z / "type").read_text().strip() == "x86_pkg_temp":
                return int((z / "temp").read_text()) / 1000
        except OSError:
            pass
    return None


async def read_fan() -> int | None:
    out = await run(["sensors", "-u"], timeout=5)
    m = re.search(r"fan1_input:\s*([\d.]+)", out)
    return int(float(m.group(1))) if m else None


def read_meminfo() -> tuple[int, int]:
    avail = swap = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            avail = int(line.split()[1]) // 1024
        elif line.startswith("SwapFree:"):
            swap = int(line.split()[1]) // 1024
    return avail, swap


def read_nic_bytes() -> tuple[int, int]:
    base = Path(f"/sys/class/net/{IFACE}/statistics")
    try:
        return (int((base / "rx_bytes").read_text()),
                int((base / "tx_bytes").read_text()))
    except OSError:
        return 0, 0


class Collector:
    def __init__(self):
        self.internet_up: bool | None = None
        self.last_loss_event = 0
        self.last_nic = (0, 0, 0.0)  # rx, tx, ts
        self.last_throughput = 0.0
        self.last_router_wan: tuple[float, int, int] | None = None
        self.router_devices_now: list[dict] = []
        self.router_info: dict = {}
        self.router_info_ts = 0.0
        self.services_now: dict[str, dict] = {}
        # [{"name","url","source":"pinned"|"auto","queries"}] — rebuilt by detect_services()
        self.services_active: list[dict] = []
        self.router_uptime_s: int | None = None

    # -------------------------------------------------------- names --
    async def names_cycle(self):
        """Resolve mDNS names for everything with an IP; never overwrite a manual name."""
        ips = {d["mac"]: d["ip"] for d in self.router_devices_now if d.get("ip")}
        try:
            ph = await pihole.safe("/api/network/devices", {"max_devices": 100, "max_addresses": 2})
            for d in ph.get("devices", []):
                mac = (d.get("hwaddr") or "").upper()
                for i in d.get("ips", []):
                    ip = i.get("ip") or ""
                    if mac and ip and ":" not in ip and ip != "0.0.0.0" and mac not in ips:
                        ips[mac] = ip
        except Exception:
            pass
        # Pi-hole uses pseudo-MACs like "IP-192.168.10.158" for things it only knows by
        # address; those aren't devices we can name.
        macs = [m for m in ips if re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", m)]
        if not macs:
            return
        found = await asyncio.gather(*(mdns_name(ips[m]) for m in macs))
        ts = int(time.time())
        with db() as conn:
            for mac, name in zip(macs, found):
                if name:
                    conn.execute(
                        "INSERT INTO device_names(mac,name,source,updated) VALUES (?,?,'mdns',?) "
                        "ON CONFLICT(mac) DO UPDATE SET name=excluded.name, updated=excluded.updated "
                        "WHERE device_names.source != 'manual'", (mac, name, ts))

    # ---------------------------------------------- gap / reboot detection --
    def detect_own_gap(self):
        """If the last probe is old, netdash (or the whole HTPC) was down. Record it so the
        Aug-1-style hard power-off shows up as an incident instead of silence."""
        r = rows("SELECT MAX(ts) m FROM probes")
        last = r[0]["m"] if r else None
        now = int(time.time())
        if last and now - last > 5 * 60:
            mins = (now - last) // 60
            with db() as conn:
                conn.execute("INSERT INTO events(ts,kind,detail) VALUES (?,?,?)",
                             (last, "host_down", f"netdash stopped reporting (HTPC off, asleep or rebooted?)"))
                conn.execute("INSERT INTO events(ts,kind,detail) VALUES (?,?,?)",
                             (now, "host_up", f"netdash back after {mins} min"))

    def note_router_uptime(self, uptime_text: str | None):
        """'13 Day 10:47:37' -> seconds; a decrease means the router rebooted."""
        if not uptime_text:
            return
        m = re.match(r"(?:(\d+)\s*Day\s*)?(\d+):(\d+):(\d+)", uptime_text.strip())
        if not m:
            return
        d, h, mi, s = (int(x or 0) for x in m.groups())
        up = d * 86400 + h * 3600 + mi * 60 + s
        if self.router_uptime_s is not None and up < self.router_uptime_s - 60:
            add_event("router_reboot", f"Router rebooted (uptime reset; was up "
                                       f"{self.router_uptime_s // 3600}h)")
        self.router_uptime_s = up

    # ------------------------------------------------------ services --
    def rebuild_services(self):
        """pinned (always) + catalog entries the house has resolved recently."""
        cutoff = int(time.time()) - AUTO_DETECT_DAYS * 86400
        seen = {r["name"]: r for r in rows(
            "SELECT name,last_seen,queries FROM service_seen WHERE last_seen>?", cutoff)}
        active, have = [], set()
        for name, url in PINNED_SERVICES:
            active.append({"name": name, "url": url, "source": "pinned",
                           "queries": seen.get(name, {}).get("queries")})
            have.add(name)
        for name, url, _ in SERVICE_CATALOG:      # catalog order = display order
            if name in seen and name not in have:
                active.append({"name": name, "url": url, "source": "auto",
                               "queries": seen[name]["queries"]})
                have.add(name)
        if not active:                              # nothing known yet (or no Pi-hole)
            active = [{"name": n, "url": u, "source": "default", "queries": None}
                      for n, u in DEFAULT_SERVICES]
        self.services_active = active

    async def detect_services(self):
        """Match Pi-hole's permitted domains against the catalog; remember what's in use."""
        top = await pihole.safe("/api/stats/top_domains", {"blocked": "false", "count": 250})
        domains = top.get("domains") if isinstance(top, dict) else None
        if not domains:
            self.rebuild_services()
            return
        ts = int(time.time())
        hits = []
        for name, _, patterns in SERVICE_CATALOG:
            n = sum(d["count"] for d in domains
                    if any(p in d["domain"].lower() for p in patterns))
            if n >= AUTO_DETECT_MIN_QUERIES:
                hits.append((name, ts, n))
        if hits:
            with db() as conn:
                conn.executemany(
                    "INSERT INTO service_seen(name,last_seen,queries) VALUES (?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, "
                    "queries=excluded.queries", hits)
        self.rebuild_services()

    # ------------------------------------------------------- router --
    async def router_cycle(self):
        if not ROUTER_PASSWORD:
            return
        ts = int(time.time())
        sample = await router.wan_counters(ROUTER_WAN_IFACE)
        if sample:
            t, rx, tx = sample
            if self.last_router_wan and t > self.last_router_wan[0]:
                dt = t - self.last_router_wan[0]
                rx_mbps = (rx - self.last_router_wan[1]) * 8 / dt / 1_000_000
                tx_mbps = (tx - self.last_router_wan[2]) * 8 / dt / 1_000_000
                if rx_mbps >= 0 and tx_mbps >= 0:  # counter reset guard
                    with db() as conn:
                        conn.execute("INSERT INTO router_wan VALUES (?,?,?)",
                                     (ts, round(rx_mbps, 3), round(tx_mbps, 3)))
            self.last_router_wan = sample
        devs = await router.devices()
        if devs:
            self.router_devices_now = devs
            with db() as conn:
                conn.executemany(
                    "INSERT INTO router_devices VALUES (?,?,?,?,?,?,?,?)",
                    [(ts, d["mac"], d["ip"], d["name"], d["band"],
                      d["signal_db"], d["down_kbps"], d["up_kbps"]) for d in devs])
        if time.time() - self.router_info_ts > 300:
            wan, sysinfo, mesh = await asyncio.gather(
                router.wan_status(), router.system(), router.mesh())
            # Never surface the public IP or router MAC through the dashboard API.
            for k in ("Public IP", "MAC-Address"):
                wan.pop(k, None)
            self.note_router_uptime((sysinfo or {}).get("Uptime"))
            if wan or sysinfo or mesh:
                self.router_info = {"wan": wan, "system": sysinfo, "mesh": mesh,
                                    "fetched": ts}
                self.router_info_ts = time.time()

    async def services_cycle(self):
        ts = int(time.time())
        if not self.services_active:
            await self.detect_services()
        results = await asyncio.gather(
            *(check_service(s["name"], s["url"]) for s in self.services_active))
        with db() as conn:
            conn.executemany(
                "INSERT INTO services VALUES (?,?,?,?,?,?,?,?)",
                [(ts, r["name"], r["ok"], r["status"], r["dns_ms"], r["connect_ms"],
                  r["ttfb_ms"], r["reason"]) for r in results])
        self.services_now = {r["name"]: r for r in results}

    async def syslog_cycle(self):
        if not ROUTER_PASSWORD:
            return
        with db() as conn:
            row = conn.execute(
                "SELECT MAX(ts) m FROM (SELECT ts FROM wifi_drops "
                "UNION ALL SELECT ts FROM events WHERE kind LIKE 'wan_%')").fetchone()
        since = row["m"] or 0
        events = await router.syslog_events(since)
        if not events:
            return
        with db() as conn:
            for e in events:
                if e["kind"] == "wifi_drop":
                    conn.execute("INSERT INTO wifi_drops VALUES (?,?,?,?)",
                                 (e["ts"], e["mac"], e["radio"], e["rssi"]))
                else:
                    conn.execute("INSERT INTO events(ts, kind, detail) VALUES (?,?,?)",
                                 (e["ts"], e["kind"], e["detail"]))

    async def probe_cycle(self):
        ts = int(time.time())
        results = {}
        for name, target in TARGETS.items():
            results[name] = await ping(target)
        with db() as conn:
            for name, r in results.items():
                conn.execute(
                    "INSERT INTO probes VALUES (?,?,?,?,?,?,?,?)",
                    (ts, name, r["sent"], r["recv"], r["loss"],
                     r["rtt_avg"], r["rtt_max"], r["jitter"]),
                )
        self.detect_outage(ts, results)

    def detect_outage(self, ts: int, results: dict):
        inet = results["internet"]
        gw = results["gateway"]
        up = inet["loss"] < 100
        if self.internet_up is None:
            self.internet_up = up
        elif up != self.internet_up:
            self.internet_up = up
            if up:
                add_event("internet_up", "Internet reachable again")
            else:
                where = ("LAN — gateway unreachable too" if gw["loss"] >= 100
                         else "WAN — gateway fine, internet dead")
                add_event("internet_down", f"Internet unreachable ({where})")
        elif up and inet["loss"] >= 5 and ts - self.last_loss_event > 600:
            self.last_loss_event = ts
            add_event("packet_loss",
                      f"{inet['loss']:.0f}% loss to {INTERNET}"
                      f" (gateway {gw['loss']:.0f}%)")

    async def machine_cycle(self):
        ts = time.time()
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
        avail, swap = read_meminfo()
        rx, tx = read_nic_bytes()
        prx, ptx, pts = self.last_nic
        rx_mbps = tx_mbps = 0.0
        if pts and ts > pts:
            rx_mbps = round((rx - prx) * 8 / (ts - pts) / 1_000_000, 2)
            tx_mbps = round((tx - ptx) * 8 / (ts - pts) / 1_000_000, 2)
        self.last_nic = (rx, tx, ts)
        with db() as conn:
            conn.execute(
                "INSERT INTO machine VALUES (?,?,?,?,?,?,?,?)",
                (int(ts), load1, read_pkg_temp(), await read_fan(),
                 avail, swap, rx_mbps, tx_mbps),
            )

    async def throughput_cycle(self):
        # Skip if the line is busy so we don't stomp on a stream and misreport.
        rx_now, _ = read_nic_bytes()
        await asyncio.sleep(3)
        rx_later, _ = read_nic_bytes()
        if (rx_later - rx_now) * 8 / 3 / 1_000_000 > 3:
            return
        mbps = await throughput_mbps()
        if mbps is not None:
            with db() as conn:
                conn.execute("INSERT INTO throughput VALUES (?,?)",
                             (int(time.time()), mbps))

    async def loop(self):
        try:
            self.detect_own_gap()
        except Exception as e:
            add_event("collector_error", f"gap detect: {e}"[:200])
        add_event("netdash_start", "Collector started")
        self.last_nic = (*read_nic_bytes(), time.time())
        try:
            rollup(backfill=True)
        except Exception as e:
            add_event("collector_error", f"rollup backfill: {e}"[:200])
        next_tp = 0.0
        next_syslog = 0.0
        next_svc = 0.0
        next_detect = 0.0
        next_names = time.time() + 90      # after the first router poll has populated IPs
        next_prune = time.time() + 3600
        while True:
            started = time.time()
            try:
                if started >= next_detect:
                    next_detect = started + 3600
                    await self.detect_services()
                await asyncio.gather(self.probe_cycle(), self.machine_cycle(),
                                     self.router_cycle())
                if started >= next_tp:
                    next_tp = started + THROUGHPUT_INTERVAL
                    asyncio.create_task(self.throughput_cycle())
                if started >= next_svc:
                    next_svc = started + SERVICE_INTERVAL
                    asyncio.create_task(self.services_cycle())
                if started >= next_syslog:
                    next_syslog = started + SYSLOG_INTERVAL
                    asyncio.create_task(self.syslog_cycle())
                if started >= next_names:
                    next_names = started + 600
                    asyncio.create_task(self.names_cycle())
                if started >= next_prune:
                    next_prune = started + 3600
                    rollup()
                    prune()
            except Exception as e:  # keep the loop alive no matter what
                add_event("collector_error", str(e)[:200])
            await asyncio.sleep(max(1, PROBE_INTERVAL - (time.time() - started)))


# --------------------------------------------------------------- pi-hole --

class PiHole:
    def __init__(self):
        self.sid: str | None = None
        self.sid_ts = 0.0
        self.client = httpx.AsyncClient(base_url=PIHOLE_URL, verify=False,
                                        timeout=10)

    async def auth(self):
        r = await self.client.post("/api/auth",
                                   json={"password": PIHOLE_PASSWORD})
        r.raise_for_status()
        self.sid = r.json()["session"]["sid"]
        self.sid_ts = time.time()

    async def get(self, path: str, params: dict | None = None):
        if not self.sid or time.time() - self.sid_ts > 1500:
            await self.auth()
        r = await self.client.get(path, params=params,
                                  headers={"X-FTL-SID": self.sid})
        if r.status_code == 401:
            await self.auth()
            r = await self.client.get(path, params=params,
                                      headers={"X-FTL-SID": self.sid})
        r.raise_for_status()
        return r.json()

    async def safe(self, path: str, params: dict | None = None):
        try:
            return await self.get(path, params)
        except Exception as e:
            return {"error": str(e)[:200]}


# ------------------------------------------------------------------- app --

collector = Collector()
pihole = PiHole()
router = Router(ROUTER_URL, ROUTER_PASSWORD)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(collector.loop())
    yield
    task.cancel()
    await pihole.client.aclose()
    await router.client.aclose()


app = FastAPI(title="netdash", lifespan=lifespan)


def rows(sql: str, *args):
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def since(hours: float) -> int:
    return int(time.time() - hours * 3600)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
async def overview():
    latest = {}
    for name in TARGETS:
        r = rows("SELECT * FROM probes WHERE target=? ORDER BY ts DESC LIMIT 1",
                 name)
        latest[name] = r[0] if r else None
    tp = rows("SELECT * FROM throughput ORDER BY ts DESC LIMIT 1")
    mc = rows("SELECT * FROM machine ORDER BY ts DESC LIMIT 1")
    inet = latest.get("internet")
    # 24h aggregates for the headline
    agg = rows(
        "SELECT AVG(loss) loss, AVG(rtt_avg) rtt, MAX(rtt_max) rtt_max, "
        "AVG(jitter) jitter, SUM(loss>=100) outages "
        "FROM probes WHERE target='internet' AND ts>?", since(24))
    summary = await pihole.safe("/api/stats/summary")
    rw = rows("SELECT * FROM router_wan ORDER BY ts DESC LIMIT 1")
    rday = rows("SELECT MAX(rx_mbps) peak_down, MAX(tx_mbps) peak_up, AVG(rx_mbps) avg_down "
                "FROM router_wan WHERE ts>?", since(24))
    svcs = services_summary(24)
    internet_up = bool(inet and inet["loss"] < 100)
    gw = latest.get("gateway")
    wifi_weak = sum(1 for d in collector.router_devices_now
                    if d.get("signal_db") is not None and d["signal_db"] < 25)
    hog = max(collector.router_devices_now, key=lambda d: d.get("down_kbps") or 0, default=None)
    hog_mbps = (hog["down_kbps"] / 1000) if hog and hog.get("down_kbps") else None
    v = verdict(internet_up, inet["loss"] if inet else 0.0, gw["loss"] if gw else 0.0,
                svcs, wifi_weak, "error" not in summary,
                router.logged_in and not router.last_error,
                line_used=rw[0]["rx_mbps"] if rw else None,
                line_cap=tp[0]["mbps"] if tp else None,
                hog_mbps=hog_mbps)
    if "{hog}" in v["text"] and hog:
        v["hog"] = {"name": best_name(hog.get("mac", ""), hog.get("name")), "mac": hog.get("mac"),
                    "ip": hog.get("ip"), "mbps": round(hog_mbps or 0, 1)}
    return {
        "now": int(time.time()),
        "internet_up": internet_up,
        "verdict": v,
        "plan_mbps": PLAN_DOWN_MBPS or None,
        "services": {"up": sum(1 for s in svcs if s["ok"]), "total": len(svcs),
                     "down": [s["name"] for s in svcs if s["down"]]},
        "latest": latest,
        "throughput": tp[0] if tp else None,
        "machine": mc[0] if mc else None,
        "day": agg[0] if agg else {},
        "pihole": summary,
        "targets": TARGETS,
        "router": {
            "configured": bool(ROUTER_PASSWORD),
            "ok": router.logged_in and not router.last_error,
            "error": router.last_error,
            "wan_now": rw[0] if rw else None,
            "wan_day": rday[0] if rday else {},
            "clients": len(collector.router_devices_now),
            **collector.router_info,
        },
    }


def services_summary(hours: float = 24, buckets: int = 48) -> list[dict]:
    """Per service: latest result, uptime %, and a status-page strip of N time buckets."""
    now = int(time.time())
    s0 = now - int(hours * 3600)
    width = (now - s0) / buckets
    # Strip + uptime come from raw or hourly depending on the window; "latest" and
    # "last failure" always come from the raw table so they're current.
    if raw_ok(hours):
        hist = rows("SELECT ts,name,ok,1 AS checks FROM services WHERE ts>? ORDER BY ts", s0)
    else:
        hist = rows("SELECT ts,name,ok,checks FROM services_h WHERE ts>? ORDER BY ts", s0)
    recent = rows("SELECT ts,name,ok,status,ttfb_ms,reason FROM services WHERE ts>? ORDER BY ts",
                  since(24))
    by_h: dict[str, list] = {}
    for r in hist:
        by_h.setdefault(r["name"], []).append(r)
    by_r: dict[str, list] = {}
    for r in recent:
        by_r.setdefault(r["name"], []).append(r)
    out = []
    for svc in collector.services_active:
        name, url = svc["name"], svc["url"]
        hs, rs = by_h.get(name, []), by_r.get(name, [])
        strip = [None] * buckets
        agg = [[0, 0] for _ in range(buckets)]  # ok, checks
        for r in hs:
            i = min(buckets - 1, int((r["ts"] - s0) / width))
            agg[i][0] += r["ok"]; agg[i][1] += r["checks"]
        for i, (ok, tot) in enumerate(agg):
            if tot:
                strip[i] = 1.0 if ok == tot else (0.0 if ok == 0 else round(ok / tot, 2))
        total = sum(r["checks"] for r in hs); up = sum(r["ok"] for r in hs)
        latest = rs[-1] if rs else None
        prev = rs[-2] if len(rs) > 1 else None
        last_fail = next((r for r in reversed(rs) if not r["ok"]), None)
        out.append({
            "name": name, "url": url,
            "source": svc["source"], "queries": svc.get("queries"),
            "ok": bool(latest and latest["ok"]),
            # "down" needs two consecutive failures so one blip doesn't shout
            "down": bool(latest and not latest["ok"] and (prev is None or not prev["ok"])),
            "status": latest["status"] if latest else None,
            "ttfb_ms": latest["ttfb_ms"] if latest else None,
            "reason": latest["reason"] if latest else "",
            "uptime": round(100 * up / total, 2) if total else None,
            "checks": total,
            "last_fail": last_fail["ts"] if last_fail else None,
            "last_fail_reason": last_fail["reason"] if last_fail else "",
            "strip": strip,
        })
    return out


def build_incidents(hours: float) -> list[dict]:
    """Group raw down/up events into outages with a duration, a cause, and collateral."""
    ev = rows("SELECT ts,kind,detail FROM events WHERE ts>? AND kind IN "
              "('internet_down','internet_up','wan_offline','wan_online','host_down','host_up') "
              "ORDER BY ts", since(hours))
    now = int(time.time())
    windows, open_ = [], {}
    SRC = {"internet": "netdash", "wan": "router", "host": "host"}
    for e in ev:
        src = SRC[e["kind"].split("_")[0]]
        if e["kind"] in ("internet_down", "wan_offline", "host_down"):
            open_[src] = (e["ts"], e["detail"])
        elif src in open_:
            s, d = open_.pop(src)
            windows.append({"start": s, "end": e["ts"], "src": src, "detail": d})
    for src, (s, d) in open_.items():           # still down
        windows.append({"start": s, "end": None, "src": src, "detail": d})
    windows.sort(key=lambda w: w["start"])
    # The probe and the router usually see the same outage; merge windows within 90s.
    merged: list[dict] = []
    for w in windows:
        last = merged[-1] if merged else None
        if last and w["start"] <= (last["end"] or now) + 90:
            last["end"] = None if (last["end"] is None or w["end"] is None) else max(last["end"], w["end"])
            last["sources"].add(w["src"])
            if w["src"] == "netdash":
                last["detail"] = w["detail"]
        else:
            merged.append({"start": w["start"], "end": w["end"], "sources": {w["src"]},
                           "detail": w["detail"] if w["src"] == "netdash" else ""})
    for m in merged:
        end = m["end"] or now
        m["duration"] = end - m["start"]
        m["ongoing"] = m["end"] is None
        d = m["detail"] or ""
        if m["sources"] == {"host"}:
            m["where"] = "HTPC — netdash wasn't running (power? reboot? sleep?)"
        elif "gateway unreachable" in d:
            m["where"] = "LAN — gateway was unreachable"
        else:
            m["where"] = "ISP — gateway was fine, line was out"
        fails = rows("SELECT DISTINCT name FROM services WHERE ok=0 AND ts BETWEEN ? AND ?",
                     m["start"] - 120, end + 120)
        m["services"] = [f["name"] for f in fails]
        m["sources"] = sorted(m["sources"])
        del m["detail"]
    merged.reverse()
    return merged


def verdict(internet_up: bool, inet_loss: float, gw_loss: float,
            svcs: list[dict], wifi_weak: int, pihole_ok: bool, router_ok: bool,
            line_used: float | None = None, line_cap: float | None = None,
            hog_mbps: float | None = None) -> dict:
    """One sentence that says whose problem it is. "{hog}" is filled in by the client so
    demo mode can anonymise the device name."""
    if not internet_up:
        if gw_loss >= 100:
            return {"level": "bad", "text": "Internet down — the gateway isn't answering either; check the router and cable"}
        return {"level": "bad", "text": "Internet down — your router is fine, the line to the ISP is out"}
    parts, level = [], "ok"
    down = [s["name"] for s in svcs if s["down"]]
    lossy = inet_loss >= 5
    # 80%, not higher: the WAN figure is a 60s average that includes ramp-up, so a line
    # that is effectively full reads 80–90%. Verified against a forced 17 Mbps download.
    saturated = bool(line_used is not None and line_cap and line_used >= 0.80 * line_cap)
    if lossy:
        parts.append(f"{inet_loss:.0f}% packet loss on the line"); level = "warn"
    if saturated:
        # Parallel flows can beat a single-stream speed test, so "used" may exceed "cap".
        how = (f"{line_used:.0f} Mbps, that's all of it" if line_used >= line_cap
               else f"{line_used:.0f} of {line_cap:.0f} Mbps in use")
        # Name the device responsible if one is clearly pulling most of it.
        who = (f", {{hog}} alone is pulling {hog_mbps:.0f}"
               if hog_mbps and hog_mbps >= 0.5 * line_used else "")
        parts.append(f"line saturated — {how}{who}; anything else will buffer"); level = "warn"
    if down:
        who = ", ".join(down)
        verb = "is" if len(down) == 1 else "are"
        # Only blame the service when our own line is clean. A saturated or lossy line
        # starves the checks too, and that's on us.
        if saturated:
            parts.append(f"{who} {verb} unreachable — probably the saturated line, not them")
        elif lossy:
            parts.append(f"{who} {verb} unreachable — probably the packet loss, not them")
        else:
            parts.append(f"{who} {verb} down — your line is fine, that's on them")
        level = "warn"
    if wifi_weak:
        parts.append(f"{wifi_weak} WiFi client{'s' if wifi_weak > 1 else ''} on weak signal"); level = "warn"
    if not pihole_ok:
        parts.append("Pi-hole API unreachable"); level = "warn"
    if ROUTER_PASSWORD and not router_ok:
        parts.append("router not reachable"); level = "warn"
    if not parts:
        return {"level": "ok", "text": "Everything looks healthy"}
    return {"level": level, "text": " · ".join(parts)}


@app.get("/api/services")
async def services(hours: float = Query(24, ge=1, le=MAX_HOURS)):
    return {"now": int(time.time()), "hours": hours,
            "interval": SERVICE_INTERVAL, "services": services_summary(hours),
            "auto_detect": {"days": AUTO_DETECT_DAYS, "min_queries": AUTO_DETECT_MIN_QUERIES,
                            "catalog": len(SERVICE_CATALOG)}}


@app.get("/api/incidents")
async def incidents(hours: float = Query(168, ge=1, le=MAX_HOURS)):
    inc = build_incidents(hours)
    total = sum(i["duration"] for i in inc)
    return {"now": int(time.time()), "hours": hours, "incidents": inc,
            "summary": {"count": len(inc), "total_s": total,
                        "longest_s": max((i["duration"] for i in inc), default=0),
                        "uptime_pct": round(100 * (1 - total / (hours * 3600)), 3)}}


@app.get("/api/router/wan")
async def router_wan(hours: float = Query(24, ge=1, le=MAX_HOURS)):
    t = "router_wan" if raw_ok(hours) else "router_wan_h"
    return rows(f"SELECT ts,rx_mbps,tx_mbps FROM {t} WHERE ts>? ORDER BY ts", since(hours))


@app.get("/api/router/devices")
async def router_devices():
    return {"now": int(time.time()), "devices": collector.router_devices_now,
            "info": collector.router_info}


@app.get("/api/router/wifi")
async def router_wifi(hours: float = Query(24, ge=1, le=MAX_HOURS)):
    """Per-client WiFi stability: drops in window, current/worst signal."""
    s = since(hours)
    drops = rows("SELECT mac, COUNT(*) n, MIN(rssi) worst_drop_rssi, MAX(ts) last_drop "
                 "FROM wifi_drops WHERE ts>? GROUP BY mac", s)
    sig = rows("SELECT mac, MIN(signal_db) worst, AVG(signal_db) avg, MAX(signal_db) best "
               "FROM router_devices WHERE ts>? AND signal_db IS NOT NULL GROUP BY mac", s)
    now = {d["mac"]: d for d in collector.router_devices_now}
    ph = await pihole.safe("/api/network/devices", {"max_devices": 100, "max_addresses": 1})
    vendor = {(d.get("hwaddr") or "").upper(): d.get("macVendor") or ""
              for d in ph.get("devices", [])}
    by = {}
    for r in drops:
        by.setdefault(r["mac"], {}).update(drops=r["n"], worst_drop_rssi=r["worst_drop_rssi"],
                                           last_drop=r["last_drop"])
    for r in sig:
        by.setdefault(r["mac"], {}).update(sig_worst=r["worst"], sig_avg=round(r["avg"] or 0),
                                           sig_best=r["best"])
    names = name_map()
    out = []
    for mac, v in by.items():
        d = now.get(mac, {})
        out.append({"mac": mac, "ip": d.get("ip", ""),
                    "name": best_name(mac, d.get("name"), names=names),
                    "vendor": vendor.get(mac, ""),
                    "band": d.get("band", ""), "online": mac in now,
                    "signal_now": d.get("signal_db"),
                    "drops": v.get("drops", 0), "worst_drop_rssi": v.get("worst_drop_rssi"),
                    "last_drop": v.get("last_drop"),
                    "sig_worst": v.get("sig_worst"), "sig_avg": v.get("sig_avg"),
                    "sig_best": v.get("sig_best")})
    out.sort(key=lambda x: (-x["drops"], x["signal_now"] if x["signal_now"] is not None else 999))
    return {"hours": hours, "clients": out}



class NameBody(BaseModel):
    mac: str
    name: str = ""


@app.put("/api/devices/name")
async def set_name(body: NameBody):
    """Manual name for a MAC. Empty name clears the manual entry (mDNS may refill it)."""
    mac = body.mac.upper().strip()
    if not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", mac):
        return JSONResponse({"error": "bad mac"}, status_code=400)
    name = body.name.strip()[:48]
    with db() as conn:
        if name:
            conn.execute("INSERT INTO device_names(mac,name,source,updated) VALUES (?,?,'manual',?) "
                         "ON CONFLICT(mac) DO UPDATE SET name=excluded.name, source='manual', "
                         "updated=excluded.updated", (mac, name, int(time.time())))
        else:
            conn.execute("DELETE FROM device_names WHERE mac=?", (mac,))
    return {"mac": mac, "name": name, "source": "manual" if name else None}


@app.get("/api/devices/usage")
async def devices_usage(days: int = Query(30, ge=1, le=RETENTION_DAYS)):
    """Approximate bytes per device: rate samples integrated over time. Raw samples are
    60s apart (rate × 60); hourly rollups carry the hour's average (rate × 3600)."""
    now = int(time.time())
    s = now - days * 86400
    raw_from = max(s, now - RAW_DAYS * 86400)
    agg: dict[str, list] = {}
    for r in rows("SELECT mac, SUM(down_kbps)*60/8/1e6 gb_down, SUM(up_kbps)*60/8/1e6 gb_up, "
                  "MAX(ip) ip, MAX(name) name FROM router_devices WHERE ts>? GROUP BY mac", raw_from):
        agg[r["mac"]] = [r["gb_down"] or 0, r["gb_up"] or 0, r["ip"], r["name"]]
    if s < raw_from:
        for r in rows("SELECT mac, SUM(down_avg)*3600/8/1e6 gb_down, SUM(up_avg)*3600/8/1e6 gb_up "
                      "FROM router_devices_h WHERE ts>? AND ts<=? GROUP BY mac", s, raw_from):
            a = agg.setdefault(r["mac"], [0, 0, "", ""])
            a[0] += r["gb_down"] or 0; a[1] += r["gb_up"] or 0
    names = name_map()
    out = [{"mac": m, "ip": v[2] or "", "name": best_name(m, v[3], names=names),
            "gb_down": round(v[0], 2), "gb_up": round(v[1], 2)} for m, v in agg.items()]
    out.sort(key=lambda x: -(x["gb_down"] + x["gb_up"]))
    total = sum(x["gb_down"] + x["gb_up"] for x in out)
    return {"days": days, "total_gb": round(total, 2), "devices": out}


@app.post("/api/speedtest")
async def speedtest_now():
    """On-demand speed test. Still refuses if the line is busy — a test that competes
    with a stream measures the wrong thing and spoils the stream."""
    rx0, _ = read_nic_bytes()
    await asyncio.sleep(2)
    rx1, _ = read_nic_bytes()
    busy = (rx1 - rx0) * 8 / 2 / 1_000_000
    rw = rows("SELECT rx_mbps FROM router_wan ORDER BY ts DESC LIMIT 1")
    line_busy = rw[0]["rx_mbps"] if rw else 0
    if busy > 3 or line_busy > 3:
        return {"skipped": True, "reason": f"line busy ({max(busy, line_busy):.0f} Mbps in use)"}
    mbps = await throughput_mbps()
    if mbps is None:
        return {"skipped": True, "reason": "test failed"}
    with db() as conn:
        conn.execute("INSERT INTO throughput VALUES (?,?)", (int(time.time()), mbps))
    return {"skipped": False, "mbps": mbps}


@app.get("/api/router/signal")
async def router_signal(mac: str, hours: float = Query(24, ge=1, le=MAX_HOURS)):
    """One device's story: signal and rate over time, plus the moments it dropped."""
    mac = mac.upper()
    s = since(hours)
    if raw_ok(hours):
        pts = rows("SELECT ts, signal_db, down_kbps, up_kbps FROM router_devices "
                   "WHERE mac=? AND ts>? ORDER BY ts", mac, s)
    else:
        pts = rows("SELECT ts, sig_avg signal_db, down_avg down_kbps, up_avg up_kbps "
                   "FROM router_devices_h WHERE mac=? AND ts>? ORDER BY ts", mac, s)
    drops = rows("SELECT ts, rssi, radio FROM wifi_drops WHERE mac=? AND ts>? ORDER BY ts", mac, s)
    return {"mac": mac, "since": s, "points": pts, "drops": drops}



@app.get("/api/wan")
async def wan(hours: float = Query(24, ge=1, le=MAX_HOURS)):
    t = "probes" if raw_ok(hours) else "probes_h"
    return {"since": since(hours), "source": t,
            "probes": rows(f"SELECT ts,target,loss,rtt_avg,rtt_max,jitter "
                           f"FROM {t} WHERE ts>? ORDER BY ts", since(hours))}


@app.get("/api/throughput")
async def throughput(hours: float = Query(24, ge=1, le=MAX_HOURS)):
    return rows("SELECT ts,mbps FROM throughput WHERE ts>? ORDER BY ts",
                since(hours))


@app.get("/api/machine")
async def machine(hours: float = Query(24, ge=1, le=MAX_HOURS)):
    t = "machine" if raw_ok(hours) else "machine_h"
    return rows(f"SELECT ts,load1,pkg_temp,fan,mem_avail_mb,swap_free_mb,rx_mbps,tx_mbps "
                f"FROM {t} WHERE ts>? ORDER BY ts", since(hours))


def report_window(start: int, end: int) -> dict:
    """The numbers you'd put in front of your ISP, for one window. Only the time netdash
    was actually watching counts — the router's syslog can report outages from before it
    existed, and those would skew everything."""
    hours = (end - start) / 3600
    first = rows("SELECT MIN(ts) m FROM (SELECT MIN(ts) ts FROM probes UNION ALL "
                 "SELECT MIN(ts) FROM probes_h)")[0]["m"]
    cov_start = max(start, first or start)
    covered_s = max(0, end - cov_start)
    inc = []
    for i in build_incidents((int(time.time()) - start) / 3600):
        a, b = max(i["start"], cov_start), min(i["end"] or end, end)
        if b > a:
            inc.append({**i, "start": a, "end": b, "duration": b - a})
    outage_s = sum(i["duration"] for i in inc)
    sp = rows("SELECT AVG(mbps) avg, MIN(mbps) min, MAX(mbps) max, COUNT(*) n "
              "FROM throughput WHERE ts>? AND ts<=?", start, end)[0]
    p10 = rows("SELECT mbps FROM throughput WHERE ts>? AND ts<=? ORDER BY mbps LIMIT 1 OFFSET "
               "(SELECT COUNT(*)/10 FROM throughput WHERE ts>? AND ts<=?)", start, end, start, end)
    t = "probes" if raw_ok(hours) else "probes_h"
    loss = rows(f"SELECT AVG(loss) l, MAX(rtt_avg) r FROM {t} "
                f"WHERE target='internet' AND ts>? AND ts<=?", start, end)[0]
    worst_speed = rows("SELECT date(ts,'unixepoch','localtime') d, AVG(mbps) m FROM throughput "
                       "WHERE ts>? AND ts<=? GROUP BY d ORDER BY m LIMIT 1", start, end)
    by_day: dict[str, int] = {}
    for i in inc:
        d = time.strftime("%Y-%m-%d", time.localtime(i["start"]))
        by_day[d] = by_day.get(d, 0) + i["duration"]
    worst_outage = max(by_day.items(), key=lambda kv: kv[1], default=None)
    drops = rows("SELECT COUNT(*) n FROM wifi_drops WHERE ts>? AND ts<=?", start, end)[0]["n"]
    avg = sp["avg"]
    return {
        "covered_days": round(covered_s / 86400, 1),
        "uptime_pct": round(100 * (1 - outage_s / covered_s), 3) if covered_s else None,
        "outages": len(inc), "outage_s": outage_s,
        "longest_s": max((i["duration"] for i in inc), default=0),
        "speed": {"avg": round(avg, 1) if avg else None, "min": sp["min"], "max": sp["max"],
                  "p10": p10[0]["mbps"] if p10 else None, "samples": sp["n"]},
        "pct_of_plan": round(100 * avg / PLAN_DOWN_MBPS, 1) if (avg and PLAN_DOWN_MBPS) else None,
        "loss_avg": round(loss["l"] or 0, 3), "rtt_max": loss["r"],
        "wifi_drops": drops,
        "worst_speed_day": ({"date": worst_speed[0]["d"], "mbps": round(worst_speed[0]["m"], 1)}
                            if worst_speed else None),
        "worst_outage_day": ({"date": worst_outage[0], "outage_s": worst_outage[1]}
                             if worst_outage else None),
    }


@app.get("/api/report")
async def report(days: int = Query(30, ge=1, le=RETENTION_DAYS)):
    now = int(time.time())
    cur = report_window(now - days * 86400, now)
    prev = report_window(now - 2 * days * 86400, now - days * 86400)
    return {"days": days, "plan_mbps": PLAN_DOWN_MBPS or None, **cur,
            # Only offer a comparison when the previous window actually has data.
            "previous": prev if prev["covered_days"] >= 0.5 else None}


@app.get("/api/events")
async def events(hours: float = Query(72, ge=1, le=MAX_HOURS)):
    return rows("SELECT * FROM events WHERE ts>? ORDER BY ts DESC LIMIT 200",
                since(hours))


@app.get("/api/pihole/history")
async def pihole_history():
    return await pihole.safe("/api/history")


@app.get("/api/pihole/top")
async def pihole_top():
    clients, blocked, permitted, upstreams = await asyncio.gather(
        pihole.safe("/api/stats/top_clients", {"count": 15}),
        pihole.safe("/api/stats/top_domains", {"blocked": "true", "count": 12}),
        pihole.safe("/api/stats/top_domains", {"blocked": "false", "count": 12}),
        pihole.safe("/api/stats/upstreams"),
    )
    return {"clients": clients, "blocked": blocked,
            "permitted": permitted, "upstreams": upstreams}


@app.get("/api/pihole/messages")
async def pihole_messages():
    return await pihole.safe("/api/info/messages")


@app.get("/api/devices")
async def devices():
    dev, top = await asyncio.gather(
        pihole.safe("/api/network/devices", {"max_devices": 100,
                                             "max_addresses": 4}),
        pihole.safe("/api/stats/top_clients", {"count": 100}),
    )
    queries_24h = {c["ip"]: c["count"] for c in top.get("clients", [])}
    names_24h = {c["ip"]: c.get("name") for c in top.get("clients", [])}
    arp, local = await asyncio.gather(
        run(["ip", "neigh", "show"], timeout=5),
        run(["ip", "-4", "-o", "addr", "show"], timeout=5),
    )
    online = {}
    for line in arp.splitlines():
        parts = line.split()
        if parts:
            online[parts[0]] = parts[-1]
    # This box never has an ARP entry for itself.
    for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", local):
        online[ip] = "SELF"
    rt = {d["mac"]: d for d in collector.router_devices_now}
    rt_by_ip = {d["ip"]: d for d in collector.router_devices_now if d["ip"]}
    names = name_map()
    out = []
    for d in dev.get("devices", []):
        for ipinfo in d.get("ips", []):
            ip = ipinfo.get("ip")
            if not ip or ":" in ip or ip == "0.0.0.0":
                continue
            state = online.get(ip, "")
            mac = (d.get("hwaddr") or "").upper()
            r = rt.get(mac) or rt_by_ip.get(ip) or {}
            nm = names.get(mac)
            out.append({
                "ip": ip,
                "name": best_name(mac, r.get("name"), ipinfo.get("name"), names_24h.get(ip),
                                  names=names),
                "name_source": nm["source"] if nm else ("router" if r.get("name") else
                                                        ("pihole" if ipinfo.get("name") else "")),
                "mac": mac,
                "vendor": d.get("macVendor") or "",
                "queries_24h": queries_24h.get(ip, 0),
                "queries_total": d.get("numQueries", 0),
                "last_seen": ipinfo.get("lastSeen") or d.get("lastQuery"),
                "online": state in ("REACHABLE", "DELAY", "PROBE", "SELF") or bool(r),
                "arp": state,
                "band": r.get("band", ""),
                "signal_db": r.get("signal_db"),
                "down_kbps": r.get("down_kbps"),
                "up_kbps": r.get("up_kbps"),
            })
    out.sort(key=lambda x: (-(x["down_kbps"] or 0), -x["queries_24h"], -(x["last_seen"] or 0)))
    return {"now": int(time.time()), "devices": out}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
