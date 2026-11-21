#!/usr/bin/env python3
"""Detect, drive-install and hotspot helper for the TP-Link Archer TX20UH (RTL8852AU)."""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

DRIVER_REPO = "https://github.com/morrownr/rtw89.git"
DRIVER_MODULE = "rtw89_8852au"
CONNECTION = "tplink-tx20uh-1"
# IDs from rtw8852au.c in morrownr/rtw89 (RTL8852AU); 2357:0141 is the TX20UH.
KNOWN_IDS = {
    "2357:013f": "TP-Link RTL8852AU",
    "2357:0140": "TP-Link RTL8852AU",
    "2357:0141": "TP-Link Archer TX20UH (RTL8852AU)",
    "0bda:8832": "Realtek RTL8852AU",
    "0bda:885a": "Realtek RTL8852AU",
    "0bda:885c": "Realtek RTL8852AU",
}
TPLINK_VIDS = ("35bc:", "2357:")
MODESWITCH_IDS = {"0bda:1a2b"}  # Realtek "driver disk" mode
DRY = False


def run(cmd, check=True, capture=True, **kw):
    if not kw.pop("quiet", False):
        print("$ " + " ".join(cmd))
    if DRY and check is not None and kw.pop("mutating", True):
        return subprocess.CompletedProcess(cmd, 0, "", "")
    kw.pop("mutating", None)
    try:
        p = subprocess.run(cmd, text=True, capture_output=capture, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    if check and p.returncode != 0:
        sys.exit(f"Command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr}")
    return p


def query(cmd, **kw):
    return run(cmd, check=False, mutating=False, **kw)


def need_root():
    if os.geteuid() != 0 and not DRY:
        print("Root needed, re-running with sudo...")
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)


# ---------- USB ----------
LSUSB_RE = re.compile(r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}:[0-9a-f]{4})\s*(.*)", re.I)


def parse_lsusb(text):
    devs = []
    for line in text.splitlines():
        m = LSUSB_RE.match(line.strip())
        if m:
            devs.append({"bus": m[1], "dev": m[2], "id": m[3].lower(), "name": m[4] or "?"})
    return devs


def list_usb():
    p = query(["lsusb"])
    if p.returncode != 0:
        sys.exit("lsusb not found. Install with: sudo apt install usbutils")
    devs = parse_lsusb(p.stdout)
    for d in devs:
        d["driver"] = usb_driver(d["id"])
    return devs


def usb_driver(vidpid):
    vid, pid = vidpid.split(":")
    for path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        base = os.path.dirname(path)
        try:
            if open(path).read().strip() == vid and open(base + "/idProduct").read().strip() == pid:
                drivers = {os.path.basename(os.path.realpath(l)) for l in glob.glob(base + "/*/driver")}
                return ",".join(sorted(drivers)) or "-"
        except OSError:
            continue
    return "-"


def cmd_list(_):
    for d in list_usb():
        print(f"{d['id']}  {d['name']:<50} driver={d['driver']}")


def find_adapter(devs=None):
    for d in devs or list_usb():
        if d["id"] in KNOWN_IDS or re.search(r"8852|8832|TX20U", d["name"], re.I) \
                or (d["id"].startswith(TPLINK_VIDS) and re.search(r"wi-?fi|wireless|802", d["name"], re.I)):
            return d
    return None


def cmd_detect(_):
    devs = list_usb()
    d = find_adapter(devs)
    if d:
        print(f"Found: {d['id']} {KNOWN_IDS.get(d['id'], d['name'])} (driver={d['driver']})")
        return d
    if any(x["id"] in MODESWITCH_IDS for x in devs):
        print("Adapter in storage/driver-disk mode. Run: sudo usb_modeswitch -KW -v 0bda -p 1a2b")
    else:
        print("TP-Link TX20UH not detected. Try another USB port (USB 3) / cable.")
    return None


# ---------- driver status ----------
def wifi_ifaces():
    """Return {iface: sysfs path} for wireless interfaces."""
    return {os.path.basename(os.path.dirname(p)): os.path.realpath(os.path.dirname(p))
            for p in glob.glob("/sys/class/net/*/wireless")}


def usb_ifaces():
    return [i for i, path in wifi_ifaces().items() if "/usb" in path]


def supports_ap(iface):
    phy = query(["iw", "dev", iface, "info"]).stdout
    m = re.search(r"wiphy (\d+)", phy)
    if not m:
        return False
    info = query(["iw", "phy", f"phy{m[1]}", "info"]).stdout
    return bool(re.search(r"Supported interface modes:(?:\s*\*[^\n]*\n)*?\s*\* AP\n", info))


def secure_boot():
    out = query(["mokutil", "--sb-state"]).stdout.lower()
    return "enabled" in out


def cmd_check(_):
    d = find_adapter()
    if not d:
        print("VERDICT: adapter not detected")
        return "missing"
    mods = query(["lsmod"]).stdout
    loaded = DRIVER_MODULE in mods
    ifaces = usb_ifaces()
    print(f"module {DRIVER_MODULE} loaded: {loaded}; usb wifi interfaces: {ifaces or 'none'}")
    if secure_boot():
        print("WARNING: Secure Boot is enabled; DKMS modules must be signed (MOK) or it must be disabled.")
    if ifaces:
        ap = supports_ap(ifaces[0])
        print(f"AP mode on {ifaces[0]}: {ap}")
        print("VERDICT: OK")
        return "ok"
    print("VERDICT: driver needed")
    return "needs_driver"


# ---------- install ----------
def cmd_install(_):
    need_root()
    if cmd_check(None) == "ok":
        print("Driver already working; nothing to do.")
        return
    kernel = os.uname().release
    run(["apt-get", "update"], capture=False)
    run(["apt-get", "install", "-y", "dkms", "build-essential", "git", "iw", "usb-modeswitch",
         "mokutil", f"linux-headers-{kernel}"], capture=False)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # fail instead of asking for credentials
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "rtw89")
        run(["git", "clone", "--depth", "1", DRIVER_REPO, src], capture=False, env=env)
        run(["make", "cleanup_target_system"], capture=False, cwd=src)
        run(["dkms", "install", src], capture=False)
        run(["make", "install_fw"], capture=False, cwd=src)
        run(["cp", "-v", "rtw89.conf", "/etc/modprobe.d/"], capture=False, cwd=src)
    if secure_boot():
        print("Secure Boot is on: run 'sudo mokutil --import /var/lib/dkms/mok.pub' "
              "(or /var/lib/shim-signed/mok/MOK.der on Ubuntu-based), then reboot and enroll the key.")
    run(["modprobe", "-r", "rtw89_8852au"], check=False)
    run(["modprobe", DRIVER_MODULE], check=False)
    if cmd_check(None) != "ok":
        print("Driver installed but adapter not up yet: unplug/replug or reboot (and enroll MOK if prompted).")


# ---------- hotspot ----------
def nm_wifi_ifaces():
    out = query(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"]).stdout
    return [l.split(":") for l in out.splitlines() if ":wifi:" in l]


def pick_ifaces(ap=None, uplink=None):
    wifi = nm_wifi_ifaces()
    usb = set(usb_ifaces())
    ap = ap or next((w[0] for w in wifi if w[0] in usb), None)
    if not ap:
        sys.exit("No USB WiFi interface found for the access point. Run 'check'.")
    uplink = uplink or next((w[0] for w in wifi if w[0] != ap and w[2] == "connected"), None)
    return ap, uplink


def hotspot_start(a):
    if len(a.password) < 8:
        sys.exit("Password must be at least 8 characters.")
    ap, uplink = pick_ifaces(a.ap_iface, a.uplink)
    print(f"AP interface: {ap}; uplink: {uplink or 'default route (ethernet?)'}")
    run(["nmcli", "connection", "delete", CONNECTION], check=False)
    run(["nmcli", "connection", "add", "type", "wifi", "ifname", ap, "con-name", CONNECTION,
         "ssid", a.ssid, "autoconnect", "no",
         "802-11-wireless.mode", "ap", "802-11-wireless.band", "a" if a.band == "5" else "bg",
         "ipv4.method", "shared", "ipv6.method", "ignore",
         "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", a.password])
    run(["nmcli", "connection", "up", CONNECTION])
    print(f"Hotspot '{a.ssid}' is up on {ap}.")


def hotspot_stop(_):
    run(["nmcli", "connection", "down", CONNECTION], check=False)
    run(["nmcli", "connection", "delete", CONNECTION], check=False)


def hotspot_status(_):
    print(query(["nmcli", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active"]).stdout)


def cmd_hotspot(a):
    {"start": hotspot_start, "stop": hotspot_stop, "status": hotspot_status}[a.action](a)


# ---------- connected clients / traffic ----------
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.I)
BLOCK_TAG = "holafi-block"


def parse_station_dump(text):
    """Parse `iw dev X station dump` into {mac: {...}}."""
    stations, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"Station ([0-9a-f:]{17})", line, re.I)
        if m:
            cur = stations.setdefault(m[1].lower(), {})
            continue
        if cur is None or ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        num = re.match(r"-?\d+", val)
        if key == "rx bytes":
            cur["rx"] = int(val)
        elif key == "tx bytes":
            cur["tx"] = int(val)
        elif key == "signal" and num:
            cur["signal"] = int(num[0])
        elif key == "connected time" and num:
            cur["connected"] = int(num[0])
        elif key == "tx bitrate":
            cur["bitrate"] = val.split()[0] + " Mbps" if num else val
    return stations


def parse_neigh(text):
    """Parse `ip neigh` output into {mac: ip} (IPv4 only)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\d+\.\d+\.\d+\.\d+).*lladdr ([0-9a-f:]{17})", line, re.I)
        if m and "FAILED" not in line:
            out[m[2].lower()] = m[1]
    return out


def parse_leases(text):
    """Parse dnsmasq leases ('expiry mac ip hostname clientid') into {mac: hostname}."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and f[3] != "*":
            out[f[1].lower()] = f[3]
    return out


def ap_iface(preferred=None):
    if preferred:
        return preferred
    for i in wifi_ifaces():
        if re.search(r"type AP", query(["iw", "dev", i, "info"], quiet=True).stdout):
            return i
    sys.exit("No interface in AP mode. Start the hotspot first: hotspot start")


def blocked_macs():
    out = query(["iptables", "-S", "FORWARD"], quiet=True).stdout
    return {m.lower() for m in re.findall(rf"--mac-source ([0-9a-fA-F:]{{17}}).*{BLOCK_TAG}", out)}


def snapshot(iface):
    """Current clients: {mac: {ip, host, rx, tx, signal, ...}} (rx/tx = bytes from/to the client)."""
    st = parse_station_dump(query(["iw", "dev", iface, "station", "dump"], quiet=True).stdout)
    ips = parse_neigh(query(["ip", "neigh", "show", "dev", iface], quiet=True).stdout)
    hosts = {}
    for path in glob.glob(f"/var/lib/NetworkManager/dnsmasq-{iface}.leases"):
        try:
            hosts.update(parse_leases(open(path).read()))
        except OSError:
            pass
    blocked = blocked_macs()
    for mac, s in st.items():
        s.update(ip=ips.get(mac, "-"), host=hosts.get(mac, "-"), blocked=mac in blocked)
    return st


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_clients(st, prev=None, dt=1.0):
    """Render the table; when prev is given, adds per-second download/upload rates."""
    head = f"{'MAC':<17}  {'IP':<15}  {'HOST':<16}  {'SIG':>4}  {'DOWN':>10}  {'UP':>10}"
    if prev is not None:
        head += f"  {'DOWN/s':>10}  {'UP/s':>10}"
    lines = [head + "  STATE"]
    for mac, s in sorted(st.items(), key=lambda kv: kv[1].get("ip", "")):
        # tx from the AP = data the client downloads; rx = data it uploads.
        down, up = s.get("tx", 0), s.get("rx", 0)
        row = (f"{mac:<17}  {s['ip']:<15}  {s['host'][:16]:<16}  {s.get('signal', 0):>4}  "
               f"{human_bytes(down):>10}  {human_bytes(up):>10}")
        if prev is not None:
            p = prev.get(mac)
            # A counter drop means the client reconnected; show 0 instead of a negative rate.
            dd = max(down - p.get("tx", 0), 0) if p else 0
            du = max(up - p.get("rx", 0), 0) if p else 0
            row += f"  {human_bytes(dd / dt) + '/s':>10}  {human_bytes(du / dt) + '/s':>10}"
        lines.append(row + ("  BLOCKED" if s["blocked"] else "  ok"))
    if len(lines) == 1:
        lines.append("(no clients connected)")
    tot_d = sum(s.get("tx", 0) for s in st.values())
    tot_u = sum(s.get("rx", 0) for s in st.values())
    lines.append(f"{len(st)} client(s); total down {human_bytes(tot_d)}, up {human_bytes(tot_u)}")
    return "\n".join(lines)


def clients_list(a):
    print(fmt_clients(snapshot(ap_iface(a.iface))))


def clients_monitor(a):
    iface, prev, t0, n = ap_iface(a.iface), None, time.monotonic(), 0
    try:
        while True:
            st, now = snapshot(iface), time.monotonic()
            if prev is not None:
                if sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                print(f"{iface} - every {a.interval:g}s (Ctrl-C to stop)")
                print(fmt_clients(st, prev, now - t0))
                n += 1
                if a.count and n >= a.count:
                    return
            prev, t0 = st, now
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()


def norm_mac(mac):
    if not MAC_RE.match(mac):
        sys.exit(f"Invalid MAC address: {mac}")
    return mac.lower()


def block_rules(mac, action):
    """Add (-I) or remove (-D) the drop rules; idempotent."""
    for chain in ("FORWARD", "INPUT"):
        rule = [chain, "-m", "mac", "--mac-source", mac, "-m", "comment", "--comment", BLOCK_TAG,
                "-j", "DROP"]
        exists = query(["iptables", "-C"] + rule).returncode == 0
        if action == "-I" and not exists:
            run(["iptables", "-I"] + rule)
        elif action == "-D" and exists:
            run(["iptables", "-D"] + rule)


def clients_block(a):
    need_root()
    mac = norm_mac(a.mac)
    block_rules(mac, "-I")
    # Best effort: ask wpa_supplicant (NetworkManager's AP backend) to drop the station now.
    run(["wpa_cli", "-i", ap_iface(a.iface), "deauthenticate", mac], check=False)
    print(f"{mac} blocked (traffic and DHCP/DNS dropped).")


def clients_unblock(a):
    need_root()
    mac = norm_mac(a.mac)
    block_rules(mac, "-D")
    print(f"{mac} unblocked.")


def cmd_clients(a):
    {"list": clients_list, "monitor": clients_monitor,
     "block": clients_block, "unblock": clients_unblock}[a.action](a)


def cmd_all(a):
    cmd_list(a)
    if cmd_detect(a) is None:
        sys.exit(1)
    if cmd_check(a) != "ok":
        cmd_install(a)
    hotspot_start(a)


def main():
    global DRY
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="print mutating commands without running")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("list", cmd_list), ("detect", cmd_detect), ("check", cmd_check),
                     ("install", cmd_install)):
        sub.add_parser(name).set_defaults(fn=fn)

    def hs_args(sp):
        sp.add_argument("--ssid", default="TP-LINK")
        sp.add_argument("--password", default="tplink")
        sp.add_argument("--band", choices=["2.4", "5"], default="5")
        sp.add_argument("--ap-iface")
        sp.add_argument("--uplink")

    h = sub.add_parser("hotspot")
    h.add_argument("action", choices=["start", "stop", "status"])
    hs_args(h)
    h.set_defaults(fn=cmd_hotspot)
    c = sub.add_parser("clients", help="list/monitor/block hotspot clients")
    c.add_argument("action", choices=["list", "monitor", "block", "unblock"])
    c.add_argument("mac", nargs="?", help="client MAC (block/unblock)")
    c.add_argument("--iface", help="AP interface (default: auto-detect)")
    c.add_argument("--interval", type=float, default=2.0, help="monitor refresh seconds")
    c.add_argument("--count", type=int, default=0, help="monitor: stop after N refreshes")
    c.set_defaults(fn=cmd_clients)
    al = sub.add_parser("all")
    hs_args(al)
    al.set_defaults(fn=cmd_all)
    a = p.parse_args()
    if a.cmd == "clients":
        if a.action in ("block", "unblock") and not a.mac:
            p.error(f"clients {a.action} requires a MAC address")
        if a.interval <= 0:
            p.error("--interval must be positive")
    DRY = a.dry_run
    a.fn(a)


if __name__ == "__main__":
    main()
