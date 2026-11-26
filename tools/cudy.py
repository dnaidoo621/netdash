#!/usr/bin/env python3
"""
Command-line control of the Cudy router, reusing netdash's logged-in client.

Runs on the netdash host with netdash's venv, so the router password stays in
config.env (never on the command line, never printed):

    /opt/netdash/venv/bin/python /opt/netdash/tools/cudy.py qos
    /opt/netdash/venv/bin/python /opt/netdash/tools/cudy.py qos set 192.168.10.27 --down 20
    /opt/netdash/venv/bin/python /opt/netdash/tools/cudy.py get /cgi-bin/luci/admin/network/mesh/clients

Only edits existing per-device QoS rules. `get` is read-only and refuses pages
that run an action on GET (Cudy's WAN "detect" page redials PPPoE when loaded).
"""
import argparse
import asyncio
import html
import os
import re
import time
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from router import Router, _text  # noqa: E402

EQOS = "/cgi-bin/luci/admin/network/eqos"
RESTART_EQOS = "/cgi-bin/luci/admin/servicectl/restart/eqos"
# Pages that do something when merely loaded, or that change state.
UNSAFE_GET = re.compile(r"detect|wizard|setup|scan|reboot|reset|upgrade|flash|restart|revert|apply|logout|delete|remove",
                        re.I)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ATTR_RE = re.compile(r'([\w.-]+)\s*=\s*"([^"]*)"')


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def parse_eqos(page: str) -> tuple[dict, dict, list[dict]]:
    """(form fields to post back, global limits, per-device rules) from the eqos fragment.

    The device IP comes from the hidden text input, not the <select>: when a capped
    device is offline its option is missing, so the browser would post whichever IP
    happens to be listed first and silently re-target the rule.
    """
    fields: dict[str, str] = {}
    for tag in INPUT_RE.findall(page):
        attrs = {k.lower(): html.unescape(v) for k, v in ATTR_RE.findall(tag)}
        name = attrs.get("name")
        if not name or attrs.get("type") == "password":
            continue
        if name in ("token", "timeclock", "cbi.submit") or name.startswith(("cbid.eqos.", "cbi.cbe.eqos.")):
            fields[name] = attrs.get("value", "")
    glob, rules = {}, {}
    for name, value in fields.items():
        m = re.fullmatch(r"cbid\.eqos\.(\w+)\.(\w+)", name)
        if not m:
            continue
        sid, opt = m.groups()
        if f"cbid.eqos.{sid}.ip" in fields:
            rules.setdefault(sid, {"sid": sid})[opt] = value
        else:
            glob[opt] = value
    return fields, glob, list(rules.values())


def show(glob: dict, rules: list[dict]) -> None:
    print(f"QoS {'on' if glob.get('enabled') == '1' else 'off'}; line limit "
          f"{glob.get('download')} down / {glob.get('upload')} up Mbps")
    for r in rules:
        note = f"  ({r['comment']})" if r.get("comment") else ""
        print(f"  {r.get('ip', '?'):15}  down {r.get('download'):>4}  up {r.get('upload'):>4} Mbps  [{r['sid']}]{note}")


async def fetch_eqos(router: Router):
    page = await router.get(EQOS)
    if not page or "cbid.eqos." not in page:
        sys.exit(f"could not read QoS page: {router.last_error or 'unexpected content'}")
    return parse_eqos(page)


async def cmd_qos(router: Router, args) -> None:
    fields, glob, rules = await fetch_eqos(router)
    if args.action != "set":
        show(glob, rules)
        return
    if args.down is None and args.up is None:
        sys.exit("nothing to change: give --down and/or --up")
    matches = [r for r in rules if r.get("ip") == args.ip]
    if len(matches) != 1:
        sys.exit(f"expected one QoS rule for {args.ip}, found {len(matches)}; this tool only edits existing rules")
    rule = matches[0]
    for opt, value in (("download", args.down), ("upload", args.up)):
        if value is not None:
            if not 1 <= value <= 10000:
                sys.exit(f"--{opt[:2]} must be 1-10000 Mbps")
            fields[f"cbid.eqos.{rule['sid']}.{opt}"] = str(value)
    fields["timeclock"] = str(int(time.time()))
    form = [(k, (None, v)) for k, v in fields.items()] + [("cbi.apply", (None, ""))]
    r = await router.client.post(EQOS, files=form)
    if r.status_code != 200:
        sys.exit(f"save failed: HTTP {r.status_code}")
    if "alert-dismissable" in r.text:
        sys.exit("router rejected the change: " + _text(r.text)[:300])
    await router.client.post(RESTART_EQOS, data={"token": fields.get("token", "")})
    # Read back what the router actually stored.
    _, glob, rules = await fetch_eqos(router)
    show(glob, rules)
    after = next((x for x in rules if x["sid"] == rule["sid"]), {})
    want = {o: str(v) for o, v in (("download", args.down), ("upload", args.up)) if v is not None}
    if after.get("ip") != args.ip or any(after.get(o) != v for o, v in want.items()):
        sys.exit("read-back does not match the requested change")


async def cmd_get(router: Router, args) -> None:
    path = args.path if args.path.startswith("/") else "/cgi-bin/luci/" + args.path
    if UNSAFE_GET.search(path):
        sys.exit(f"refusing {path}: pages like this act when loaded")
    body = await router.get(path)
    if body is None:
        sys.exit(f"failed: {router.last_error}")
    print(body if args.raw else _text(body))


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=str(ROOT / "config.env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("qos", help="show or change per-device QoS limits")
    q.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    q.add_argument("ip", nargs="?")
    q.add_argument("--down", type=int, help="download limit, Mbps")
    q.add_argument("--up", type=int, help="upload limit, Mbps")
    g = sub.add_parser("get", help="read a router page (read-only)")
    g.add_argument("path")
    g.add_argument("--raw", action="store_true", help="print HTML instead of text")
    args = p.parse_args()
    if args.cmd == "qos" and args.action == "set" and not args.ip:
        p.error("qos set needs an IP")

    env = load_env(Path(args.env))
    if not env.get("ROUTER_PASSWORD"):
        sys.exit("ROUTER_PASSWORD is not set in " + args.env)
    router = Router(env.get("ROUTER_URL") or f"http://{env.get('GATEWAY', '192.168.10.1')}", env["ROUTER_PASSWORD"])
    router.client.headers["User-Agent"] = "netdash-cli"
    try:
        await (cmd_qos if args.cmd == "qos" else cmd_get)(router, args)
    finally:
        await router.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
