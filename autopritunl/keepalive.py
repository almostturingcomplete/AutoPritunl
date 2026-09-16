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
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import glogin  # noqa: E402
import pvpn  # noqa: E402

SLICE_S = 5  # granularity of the interruptible sleep
OFFLINE_RETRY_S = 10
MAX_BACKOFF_S = 120
# One ensure() must finish inside this: gateway reconnect (300s) + browser lock wait +
# a Chromium reinstall + Google login + SSO + connect wait come to about 15 minutes.
HANG_S = 1200
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
            ip = shutil.which("ip") or "/usr/sbin/ip"  # systemd user units get a short PATH
            out = subprocess.run([ip, "-o", "route", "get", "1.1.1.1"],
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


def escalate(fails, mode):
    """Retrying the same broken path is what turned one gateway outage into a whole day
    without a tunnel: the loop failed identically every two minutes and repaired nothing.
    Each further consecutive failure now buys one repair, cheapest and safest first, and
    each runs once per outage rather than on every retry."""
    if fails == 2:
        # The server's name lives inside VPN_DOMAIN, whose resolver is behind the tunnel.
        pvpn.ensure_host_pinned()
    elif fails == 3 and mode in ("auto", "local"):
        pvpn.restart_client_service()
    elif fails == 4:
        pvpn.reset_pf()


def watchdog(busy):
    """ensure() can block for ever: a lock, an ssh session that dies mid-command, a
    pritunl-client call into a wedged service. The loop cannot catch that (the loop is
    what is blocked), launchd sees a live process, and the log stays empty: the tunnel
    was down for 12 hours that way once. Exit instead. The service manager restarts us,
    and a fresh process holds no stale locks or pipes."""
    while True:
        time.sleep(SLICE_S)
        since = busy.get("since")
        if since and time.time() - since > HANG_S:
            pvpn.log(f"ensure() has been running for {int(time.time() - since)}s; "
                     "exiting so the service restarts it")
            sys.stderr.flush()
            os._exit(3)


def main():
    if glogin.ENV_FILE.exists():
        glogin.load_env()
    interval = int(os.environ.get("PVPN_INTERVAL", "30"))
    mode = os.environ.get("PVPN_MODE", "auto")
    net = net_fingerprint()
    last = None
    fails = 0
    was_offline = False
    busy = {}
    threading.Thread(target=watchdog, args=(busy,), daemon=True).start()
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
            busy["since"] = time.time()
            try:
                state = pvpn.ensure(mode)
            finally:
                busy["since"] = None
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
            escalate(fails, mode)
            net, changed = wait(wait_s, net)
        except Exception as e:  # never die; launchd/systemd would just restart us anyway
            fails += 1
            wait_s = min(MAX_BACKOFF_S, 15 * 2 ** min(fails, 5))
            pvpn.log(f"error {type(e).__name__}: {e}; retry in {wait_s}s")
            last = None
            escalate(fails, mode)
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
