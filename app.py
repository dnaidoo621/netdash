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
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
ROUTER_URL = os.environ.get("ROUTER_URL", f"http://{GATEWAY}")
ROUTER_PASSWORD = os.environ.get("ROUTER_PASSWORD", "")
ROUTER_WAN_IFACE = os.environ.get("ROUTER_WAN_IFACE", "eth1.2")
SYSLOG_INTERVAL = int(os.environ.get("SYSLOG_INTERVAL", "300"))

SERVICE_INTERVAL = int(os.environ.get("SERVICE_INTERVAL", "120"))

TARGETS = {"gateway": GATEWAY, "isp": ISP_BOX, "internet": INTERNET}

# Services to health-check. Override with a services.json next to app.py:
#   [{"name": "Netflix", "url": "https://www.netflix.com/"}, ...]
DEFAULT_SERVICES = [
    ("Netflix",   "https://www.netflix.com/"),
    ("YouTube",   "https://www.youtube.com/"),
    ("Google",    "https://www.google.com/generate_204"),
    ("GitHub",    "https://github.com/"),
    ("Apple",     "https://www.apple.com/"),
    ("iCloud",    "https://www.icloud.com/"),
    ("Microsoft", "https://www.microsoft.com/"),
    ("WhatsApp",  "https://web.whatsapp.com/"),
    ("Stremio",   "https://app.strem.io/"),
    ("Claude",    "https://claude.ai/"),
]


def load_services() -> list[tuple[str, str]]:
    p = BASE / "services.json"
    if p.exists():
        try:
            import json
            return [(s["name"], s["url"]) for s in json.loads(p.read_text())]
        except Exception:
            pass
    return DEFAULT_SERVICES


SERVICES = load_services()

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


def prune():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with db() as conn:
        for t in ("probes", "throughput", "machine", "events",
                  "router_wan", "router_devices", "wifi_drops", "services"):
            conn.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))


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
            if wan or sysinfo or mesh:
                self.router_info = {"wan": wan, "system": sysinfo, "mesh": mesh,
                                    "fetched": ts}
                self.router_info_ts = time.time()

    async def services_cycle(self):
        ts = int(time.time())
        results = await asyncio.gather(*(check_service(n, u) for n, u in SERVICES))
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
        add_event("netdash_start", "Collector started")
        self.last_nic = (*read_nic_bytes(), time.time())
        next_tp = 0.0
        next_syslog = 0.0
        next_svc = 0.0
        next_prune = time.time() + 3600
        while True:
            started = time.time()
            try:
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
                if started >= next_prune:
                    next_prune = started + 3600
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
    v = verdict(internet_up, inet["loss"] if inet else 0.0, gw["loss"] if gw else 0.0,
                svcs, wifi_weak, "error" not in summary,
                router.logged_in and not router.last_error,
                line_used=rw[0]["rx_mbps"] if rw else None,
                line_cap=tp[0]["mbps"] if tp else None)
    return {
        "now": int(time.time()),
        "internet_up": internet_up,
        "verdict": v,
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
    data = rows("SELECT ts,name,ok,status,ttfb_ms,reason FROM services WHERE ts>? ORDER BY ts", s0)
    by: dict[str, list] = {n: [] for n, _ in SERVICES}
    for r in data:
        by.setdefault(r["name"], []).append(r)
    out = []
    for name, url in SERVICES:
        rs = by.get(name, [])
        strip = [None] * buckets
        agg = [[0, 0] for _ in range(buckets)]  # ok, total
        for r in rs:
            i = min(buckets - 1, int((r["ts"] - s0) / width))
            agg[i][0] += r["ok"]; agg[i][1] += 1
        for i, (ok, tot) in enumerate(agg):
            if tot:
                strip[i] = 1.0 if ok == tot else (0.0 if ok == 0 else round(ok / tot, 2))
        total = len(rs); up = sum(r["ok"] for r in rs)
        latest = rs[-1] if rs else None
        prev = rs[-2] if len(rs) > 1 else None
        last_fail = next((r for r in reversed(rs) if not r["ok"]), None)
        out.append({
            "name": name, "url": url,
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


def verdict(internet_up: bool, inet_loss: float, gw_loss: float,
            svcs: list[dict], wifi_weak: int, pihole_ok: bool, router_ok: bool,
            line_used: float | None = None, line_cap: float | None = None) -> dict:
    """One sentence that says whose problem it is."""
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
        parts.append(f"line saturated — {how}, anything else will buffer"); level = "warn"
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
async def services(hours: float = Query(24, ge=1, le=168)):
    return {"now": int(time.time()), "hours": hours,
            "interval": SERVICE_INTERVAL, "services": services_summary(hours)}


@app.get("/api/router/wan")
async def router_wan(hours: float = Query(24, ge=1, le=168)):
    return rows("SELECT ts,rx_mbps,tx_mbps FROM router_wan WHERE ts>? ORDER BY ts",
                since(hours))


@app.get("/api/router/devices")
async def router_devices():
    return {"now": int(time.time()), "devices": collector.router_devices_now,
            "info": collector.router_info}


@app.get("/api/router/wifi")
async def router_wifi(hours: float = Query(24, ge=1, le=336)):
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
    out = []
    for mac, v in by.items():
        d = now.get(mac, {})
        out.append({"mac": mac, "ip": d.get("ip", ""), "name": d.get("name", ""),
                    "vendor": vendor.get(mac, ""),
                    "band": d.get("band", ""), "online": mac in now,
                    "signal_now": d.get("signal_db"),
                    "drops": v.get("drops", 0), "worst_drop_rssi": v.get("worst_drop_rssi"),
                    "last_drop": v.get("last_drop"),
                    "sig_worst": v.get("sig_worst"), "sig_avg": v.get("sig_avg"),
                    "sig_best": v.get("sig_best")})
    out.sort(key=lambda x: (-x["drops"], x["signal_now"] if x["signal_now"] is not None else 999))
    return {"hours": hours, "clients": out}


@app.get("/api/router/signal")
async def router_signal(mac: str, hours: float = Query(24, ge=1, le=168)):
    return rows("SELECT ts, signal_db, down_kbps, up_kbps FROM router_devices "
                "WHERE mac=? AND ts>? ORDER BY ts", mac.upper(), since(hours))


@app.get("/api/wan")
async def wan(hours: float = Query(24, ge=1, le=168)):
    return {"since": since(hours),
            "probes": rows("SELECT ts,target,loss,rtt_avg,rtt_max,jitter "
                           "FROM probes WHERE ts>? ORDER BY ts", since(hours))}


@app.get("/api/throughput")
async def throughput(hours: float = Query(24, ge=1, le=168)):
    return rows("SELECT ts,mbps FROM throughput WHERE ts>? ORDER BY ts",
                since(hours))


@app.get("/api/machine")
async def machine(hours: float = Query(24, ge=1, le=168)):
    return rows("SELECT * FROM machine WHERE ts>? ORDER BY ts", since(hours))


@app.get("/api/events")
async def events(hours: float = Query(72, ge=1, le=336)):
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
    out = []
    for d in dev.get("devices", []):
        for ipinfo in d.get("ips", []):
            ip = ipinfo.get("ip")
            if not ip or ":" in ip or ip == "0.0.0.0":
                continue
            state = online.get(ip, "")
            mac = (d.get("hwaddr") or "").upper()
            r = rt.get(mac) or rt_by_ip.get(ip) or {}
            out.append({
                "ip": ip,
                "name": ipinfo.get("name") or names_24h.get(ip) or r.get("name") or "",
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
