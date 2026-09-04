#!/usr/bin/env python3
"""Keep the VPN up. Runs forever under launchd (macOS) or systemd (Linux).

Every PVPN_INTERVAL seconds (default 30) calls pvpn.ensure(PVPN_MODE). On failure
backs off exponentially up to 2 minutes. Logs one line per state change.

Two things break a plain poll-and-backoff loop on a laptop: sleeping through a
network change while in a long backoff, and burning retries (which start a browser
for the SSO fallback) while there is no network at all. So the loop watches the
attached network and wakes immediately when it changes, and stays quiet offline.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import glogin  # noqa: E402
import pvpn  # noqa: E402

SLICE_S = 5  # granularity of the interruptible sleep
OFFLINE_RETRY_S = 10
MAX_BACKOFF_S = 120
PROBES = [("1.1.1.1", 443), ("8.8.8.8", 443)]  # 443, not 53: many networks block outbound DNS


def net_fingerprint():
    """Identity of the network this host is attached to: primary interface, its router
    and our address on it. Changes on wifi switch, ethernet plug, sleep/wake, new lease."""
    iface = gw = ""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["route", "-n", "get", "default"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                k, _, v = line.strip().partition(":")
                if k == "interface":
                    iface = v.strip()
                elif k == "gateway":
                    gw = v.strip()
        else:
            out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            if "dev" in out:
                iface = out[out.index("dev") + 1]
            if "via" in out:
                gw = out[out.index("via") + 1]
    except Exception:
        pass
    return f"{iface}/{gw}/{local_ip()}"


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 53))  # no packet leaves; just picks the source address
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def online(timeout=3):
    for host, port in PROBES:
        try:
            socket.create_connection((host, port), timeout).close()
            return True
        except OSError:
            pass
    return False


def wait(seconds, net):
    """Sleep, but return as soon as the attached network changes.
    Returns (fingerprint, changed)."""
    deadline = time.time() + seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            return net, False
        time.sleep(min(SLICE_S, left))
        cur = net_fingerprint()
        if cur != net:
            pvpn.log(f"network changed: {net} -> {cur}")
            return cur, True


def main():
    if glogin.ENV_FILE.exists():
        glogin.load_env()
    interval = int(os.environ.get("PVPN_INTERVAL", "30"))
    mode = os.environ.get("PVPN_MODE", "auto")
    net = net_fingerprint()
    last = None
    fails = 0
    was_offline = False
    pvpn.log(f"keepalive start mode={mode} interval={interval}s net={net}")
    while True:
        changed = False
        try:
            if not online():
                if not was_offline:
                    pvpn.log("offline; holding until a network comes back")
                was_offline, last, fails = True, None, 0
                net, changed = wait(OFFLINE_RETRY_S, net)
                continue
            if was_offline:
                pvpn.log("online again")
                was_offline = False
            state = pvpn.ensure(mode)
            fails = 0
            if state != last:
                pvpn.log(f"up: {state}")
                last = state
            net, changed = wait(interval, net)
        except pvpn.PvpnError as e:
            fails += 1
            wait_s = min(MAX_BACKOFF_S, 15 * 2 ** min(fails, 5))
            pvpn.log(f"down ({e}); retry in {wait_s}s")
            last = None
            net, changed = wait(wait_s, net)
        except Exception as e:  # never die; launchd/systemd would just restart us anyway
            fails += 1
            wait_s = min(MAX_BACKOFF_S, 15 * 2 ** min(fails, 5))
            pvpn.log(f"error {type(e).__name__}: {e}; retry in {wait_s}s")
            last = None
            net, changed = wait(wait_s, net)
        if changed:
            # A tunnel built on the old link is dead even when its process is alive:
            # drop it so the next ensure() rebuilds on the new one, and retry at once.
            last, fails = None, 0
            if mode in ("auto", "gateway"):
                try:
                    pvpn.stop_gateway()
                except Exception as e:
                    pvpn.log(f"stop_gateway after network change: {e}")


if __name__ == "__main__":
    main()
