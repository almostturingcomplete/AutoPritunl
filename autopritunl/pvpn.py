#!/usr/bin/env python3
"""Pritunl VPN, CLI only. SSO is completed headlessly with the glogin Google session.

Modes (PVPN_MODE in .env, or flags):
  auto     use GATEWAY_HOST if set, reachable and connected there, else local
  local    run the Pritunl client on this machine
  gateway  route VPN subnets through the gateway host with sshuttle (no Pritunl here)

Usage:
  pvpn                      connect in PVPN_MODE (default auto)
  pvpn --local | --gateway  force a mode for this call
  pvpn --status             local profile table, sshuttle state, gateway state
  pvpn --stop               disconnect everything (local profile and sshuttle)
  pvpn --add                add profile from PRITUNL_PROFILE if not present
  pvpn --profile NAME       pick profile by name substring (default: first listed)
  pvpn --keep-gui           do not quit the Pritunl GUI (it opens the single-use SSO
                            link in your default browser and burns it)
  pvpn --headed             show the browser during SSO
  pvpn --routes             print the VPN subnets used for gateway mode

Works on macOS and Linux (pritunl-client CLI + pritunl-service, no GUI needed).
"""
import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import glogin  # noqa: E402

SECRETS_FILE = os.environ.get("AUTOPRITUNL_SECRETS", "")  # optional extra KEY=VALUE file
CANDIDATES = [
    "/Applications/Pritunl.app/Contents/Resources/pritunl-client",
    "/usr/bin/pritunl-client",
    "/usr/local/bin/pritunl-client",
]
GUI_PATTERNS = ["Pritunl.app/Contents/MacOS/Pritunl", "Pritunl Helper", "pritunl-client-electron"]
CONNECT_TIMEOUT_S = 90
GATEWAY_RECONNECT_TIMEOUT_S = 300
SSO_TIMEOUT_S = 90
SSHUTTLE_PID = glogin.CACHE / "sshuttle.pid"


class PvpnError(Exception):
    pass


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pvpn] {msg}", file=sys.stderr)


def cfg(key, default=None):
    v = os.environ.get(key)
    if v:
        return v
    if SECRETS_FILE and Path(SECRETS_FILE).exists():
        for line in Path(SECRETS_FILE).read_text().splitlines():
            m = re.match(rf'^{key}=["\']?(.*?)["\']?$', line.strip())
            if m:
                return m.group(1)
    return default


def routes():
    r = cfg("VPN_ROUTES", "").split()
    if not r:
        raise PvpnError("VPN_ROUTES not set in .env (subnets the VPN pushes; see README)")
    return r


def gateway_host():
    return cfg("GATEWAY_HOST", "")


def vpn_dns():
    return cfg("VPN_DNS", "")


# ---------------------------------------------------------------- local pritunl

_PC = None


def client_bin():
    global _PC
    if _PC:
        return _PC
    found = shutil.which("pritunl-client")
    if not found:
        found = next((c for c in CANDIDATES if os.path.exists(c)), None)
    if not found:
        raise PvpnError("pritunl-client not found; install Pritunl client (service + CLI)")
    _PC = found
    return _PC


def pc(*args, check=True):
    r = subprocess.run([client_bin(), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PvpnError(f"pritunl-client {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def profiles():
    rows = []
    for line in pc("list").splitlines():
        if not line.startswith("|") or "ID" in line.split("|")[1]:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 7:
            rows.append(dict(id=cells[0], name=cells[1], state=cells[2], autostart=cells[3],
                             online=cells[4], server=cells[5], client=cells[6]))
    return rows


def connected(row):
    return bool(row) and row["client"] not in ("", "-")


def pick(name_sub=None):
    rows = profiles()
    if name_sub:
        rows = [r for r in rows if name_sub.lower() in r["name"].lower()]
    if not rows:
        raise PvpnError("no matching profile; run `pvpn --add`")
    return rows[0]


def profile_source():
    """PRITUNL_PROFILE: pritunl:// URI, or a path to .tar / .ovpn / .zip exported from the
    Pritunl user portal. Files are normalized to a tar the CLI accepts. URIs from the portal
    expire, so prefer the exported file (PRITUNL_PROFILE_FILE) when present."""
    src = cfg("PRITUNL_PROFILE_FILE") or cfg("PRITUNL_PROFILE")
    if not src:
        raise PvpnError("PRITUNL_PROFILE_FILE or PRITUNL_PROFILE not set in .env")
    if src.startswith("pritunl://"):
        return src
    path = Path(os.path.expanduser(src))
    if not path.exists():
        raise PvpnError(f"profile file not found: {path}")
    if path.suffix == ".tar":
        return str(path)
    work = glogin.CACHE / "profile-import"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if path.suffix == ".zip":
        import zipfile
        zipfile.ZipFile(path).extractall(work)
    else:
        shutil.copy(path, work / path.name)
    tar = glogin.CACHE / "profile-import.tar"
    subprocess.run(["tar", "-cf", str(tar), "-C", str(work)] + [f.name for f in work.iterdir()], check=True)
    return str(tar)


def add_profile():
    src = profile_source()
    before = {r["id"] for r in profiles()}
    out = pc("add", src)
    after = [r for r in profiles() if r["id"] not in before]
    log(f"added: {[r['name'] for r in after] or out.strip()}")
    return after


def quit_gui():
    ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    hits = [l for l in ps.splitlines() if any(p in l for p in GUI_PATTERNS) and "pritunl-service" not in l]
    if not hits:
        return
    log("quitting Pritunl GUI so it does not open the SSO link in your browser")
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'quit app "Pritunl"'], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "pritunl-client-electron"], capture_output=True)
    time.sleep(2)


def stop_and_settle(pid, timeout=30):
    """`start` on a profile that is still Active returns silently, without an SSO link;
    the client then waits for an authentication that never arrives and gives up with
    "Single sign-on timeout". So wait for the profile to actually go Inactive first."""
    pc("stop", pid, check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = next((r for r in profiles() if r["id"] == pid), None)
        if not r or r["state"] != "Active":
            return True
        time.sleep(1)
    return False


def start_and_get_sso(pid):
    if not stop_and_settle(pid):
        log("profile still Active after stop; starting anyway")
    # Autostart makes this a "system profile", and the client refuses to run single
    # sign-on for one non-interactively: it stops the profile and prints nothing
    # ("Stopping system profile due to non-interactive single sign-on"). The keepalive
    # is what starts us on boot, so autostart buys nothing here.
    pc("disable", pid, check=False)
    out = pc("start", pid)
    m = re.search(r"https://\S+", out)
    if not m:
        log(f"no SSO link printed (start said {out.strip()!r}); waiting to see if the "
            "profile connects without one")
        return None
    return m.group(0)


def click_if_visible(page, locator, what):
    try:
        el = page.locator(locator).first
        if el.is_visible():
            log(f"clicking {what}")
            el.click(timeout=5000, no_wait_after=True)
            return True
    except Exception:
        pass
    return False


def complete_sso(page, url):
    """key/request -> Google account chooser -> consent -> callback -> /success."""
    log("opening SSO link")
    page.goto(url, wait_until="domcontentloaded")
    deadline = time.time() + SSO_TIMEOUT_S
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        u = urlparse(page.url)
        host = u.hostname or ""
        if host == "accounts.google.com":
            # Google may re-authenticate (password, then TOTP) before granting OAuth consent.
            if glogin.first_visible(page, [glogin.PASS_SEL], timeout=300):
                log("re-auth password")
                page.fill(glogin.PASS_SEL, os.environ["GPASS"])
                page.keyboard.press("Enter")
                page.wait_for_timeout(2500)
            elif glogin.first_visible(page, ["input#totpPin, input[name=totpPin], input[type=tel]"], timeout=300):
                log("re-auth totp")
                page.fill("input#totpPin, input[name=totpPin], input[type=tel]", glogin.totp())
                page.keyboard.press("Enter")
                page.wait_for_timeout(2500)
            elif "/consent" in u.path or "/oauth" in u.path:
                click_if_visible(page, "button:has-text('Continue'), button:has-text('Allow')", "consent")
            else:
                click_if_visible(page, f"[data-identifier='{os.environ['GMAIL']}']", "account") or \
                    click_if_visible(page, f"text={os.environ['GMAIL']}", "account") or \
                    click_if_visible(page, "button:has-text('Continue'), button:has-text('Next')", "continue")
            continue
        if host and "google" not in host:
            try:
                body = page.inner_text("body")
            except Exception:
                continue
            low = body.lower()
            if u.path.rstrip("/").endswith("/success") or "success" in low or "authenticated" in low:
                log(f"SSO done at {host}{u.path}")
                return True
            if "404" in low or "error" in low or "invalid" in low:
                page.screenshot(path=str(glogin.CACHE / "pvpn-fail.png"))
                raise PvpnError(f"SSO page error at {page.url}: {body[:120]!r}; see {glogin.CACHE/'pvpn-fail.png'}")
    page.screenshot(path=str(glogin.CACHE / "pvpn-fail.png"))
    raise PvpnError(f"SSO not completed in {SSO_TIMEOUT_S}s; at {page.url}; see {glogin.CACHE/'pvpn-fail.png'}")


def recent_client_logs(pid, since, limit=8):
    """pritunl-client keeps its log across runs, so show only lines from this attempt:
    otherwise a failure gets explained with yesterday's disconnect."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    fresh = [l for l in pc("logs", pid, check=False).splitlines()
             if l[:4].isdigit() and l[:19] >= stamp]
    return (fresh or ["(no client log lines since this attempt started)"])[-limit:]


def wait_connected(pid):
    started = time.time()
    deadline = started + CONNECT_TIMEOUT_S
    while time.time() < deadline:
        r = next((r for r in profiles() if r["id"] == pid), None)
        if connected(r):
            return r
        if r and r["state"] != "Active":
            break
        time.sleep(2)
    raise PvpnError("not connected; last log lines:\n" + "\n".join(recent_client_logs(pid, started)))


def connect_local(name_sub=None, keep_gui=False, headed=False):
    if not profiles():
        add_profile()
    p = pick(name_sub)
    if connected(p):
        return p
    glogin.load_env()
    if not keep_gui:
        quit_gui()
    # Browser + Google session first: the SSO link is single-use and the client
    # regenerates it on every reconnect attempt, so open it as soon as it is printed.
    with glogin.browser(headless=not headed) as (ctx, page):
        glogin.ensure_login(page)
        url = start_and_get_sso(p["id"])
        if url:
            complete_sso(page, url)
            glogin.export_state(ctx)
    return wait_connected(p["id"])


def stop_local(name_sub=None):
    try:
        rows = profiles()
    except PvpnError:
        return
    for r in rows:
        if name_sub and name_sub.lower() not in r["name"].lower():
            continue
        if r["state"] == "Active" or connected(r):
            pc("stop", r["id"], check=False)
            log(f"stopped local {r['name']}")


# ---------------------------------------------------------------- gateway (sshuttle)

def ssh_base():
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]


def gateway_status():
    """Return (reachable, connected_row_or_None)."""
    r = subprocess.run(ssh_base() + [gateway_host(), "pritunl-client list"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, None
    for line in r.stdout.splitlines():
        if line.startswith("|") and "ID" not in line.split("|")[1]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 7 and cells[6] not in ("", "-"):
                return True, dict(name=cells[1], client=cells[6], server=cells[5], online=cells[4])
    return True, None


def sshuttle_pid():
    try:
        pid = int(SSHUTTLE_PID.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def probe_target():
    """host:port that is only reachable through the VPN, used to prove the tunnel
    actually carries traffic. Defaults to the VPN resolver."""
    t = cfg("VPN_PROBE", "")
    if t:
        host, _, port = t.partition(":")
        return host, int(port or 53)
    return (vpn_dns(), 53) if vpn_dns() else None


def tunnel_ok(timeout=5):
    """A live sshuttle process is not proof of a working tunnel. macOS pf can end up with
    sshuttle's rules loaded and evaluated but redirecting nothing - every connection then
    just times out - so check that something inside the VPN is actually reachable."""
    t = probe_target()
    if not t:
        return True
    try:
        socket.create_connection(t, timeout).close()
        return True
    except OSError:
        return False


def reset_pf():
    """sshuttle enables pf with `pfctl -E` and releases it with a token on exit. An
    unclean exit leaves the reference and the anchors behind, and a later run loads its
    rules into a ruleset that no longer routes to the redirector. Flushing pf and
    reloading /etc/pf.conf clears that; it is what the system does at boot anyway."""
    if sys.platform != "darwin":
        return
    for args in (["-d"], ["-F", "all"], ["-f", "/etc/pf.conf"]):
        r = subprocess.run(["sudo", "-n", "/sbin/pfctl", *args], capture_output=True, text=True)
        if r.returncode != 0 and "not enabled" not in (r.stderr or ""):
            log(f"pfctl {' '.join(args)}: {r.stderr.strip()[-120:] or 'failed'}"
                " (re-run ./install.sh to allow it unattended)")
            return
    log("reset pf")


def gateway_reconnect():
    """The gateway holds the VPN. If its tunnel dropped, have it reconnect itself: that
    is a headless SSO on the gateway, not a browser login on this laptop."""
    log(f"asking {gateway_host()} to reconnect its VPN")
    d = cfg("GATEWAY_INSTALL_DIR", "Projects/AutoPritunl")
    r = subprocess.run(ssh_base() + [gateway_host(), f"cd {d} && ./bin/pvpn --local"],
                       capture_output=True, text=True, timeout=GATEWAY_RECONNECT_TIMEOUT_S)
    if r.returncode != 0:
        log(f"gateway could not reconnect: {(r.stderr or r.stdout).strip()[-200:]}")
    return r.returncode == 0


def connect_gateway():
    if sshuttle_pid():
        return sshuttle_pid()
    exe = shutil.which("sshuttle")
    if not exe:
        raise PvpnError("sshuttle not installed (brew install sshuttle / apt install sshuttle)")
    if not gateway_host():
        raise PvpnError("GATEWAY_HOST not set")
    reachable, gw = gateway_status()
    if not reachable:
        raise PvpnError(f"gateway {gateway_host()} not reachable over ssh")
    if not gw:
        if gateway_reconnect():
            reachable, gw = gateway_status()
        if not gw:
            raise PvpnError(f"gateway {gateway_host()} reachable but its VPN is not connected")
    stop_local()  # avoid duplicate routes
    glogin.CACHE.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-r", gateway_host(), "--daemon", "--pidfile", str(SSHUTTLE_PID),
           "-e", " ".join(ssh_base())]
    if vpn_dns():
        # DNS queries sent to the VPN resolver (see /etc/resolver/<domain> on macOS,
        # written by install.sh) are captured and answered through the gateway's tunnel.
        cmd += ["--ns-hosts", vpn_dns(), "--to-ns", vpn_dns()]
    cmd += routes()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        why = (r.stderr.strip() or r.stdout.strip())[-300:]
        if not why or "sudo" in why.lower() or "password" in why.lower():
            # sudo writes its prompt to /dev/tty, so under launchd a missing NOPASSWD rule
            # looks like sshuttle exiting with nothing to say.
            why = (why or f"exit {r.returncode}, no output") + (
                "; sudo could not start the sshuttle firewall unattended - re-run "
                "./install.sh to refresh /etc/sudoers.d/sshuttle_auto (it breaks on a "
                "Homebrew sshuttle or python upgrade)")
        raise PvpnError(f"sshuttle failed: {why}")
    time.sleep(1)
    pid = sshuttle_pid()
    if not pid:
        raise PvpnError("sshuttle exited immediately; check sudoers (install.sh) and ssh access")
    if not tunnel_ok():
        log("sshuttle is up but nothing routes through it; resetting pf and retrying")
        stop_gateway()
        reset_pf()
        subprocess.run(cmd, capture_output=True, text=True)
        time.sleep(1)
        pid = sshuttle_pid()
        if not pid or not tunnel_ok():
            stop_gateway()
            raise PvpnError(f"tunnel not carrying traffic after a pf reset "
                            f"(probe {probe_target()}); routes may have changed")
    log(f"gateway up via {gateway_host()} ({gw['client']}), sshuttle pid {pid}")
    return pid


def stop_gateway():
    pid = sshuttle_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not sshuttle_pid():
                break
            time.sleep(0.5)
        log("stopped sshuttle")
    SSHUTTLE_PID.unlink(missing_ok=True)


# ---------------------------------------------------------------- orchestration

def ensure(mode="auto", name_sub=None, keep_gui=False, headed=False):
    """Idempotent: make the VPN usable in the given mode. Returns a status string."""
    if mode == "gateway":
        connect_gateway()
        return f"gateway via {gateway_host()}"
    if mode == "local":
        stop_gateway()
        r = connect_local(name_sub, keep_gui, headed)
        return f"local {r['name']} client={r['client']} server={r['server']}"
    # auto
    if sshuttle_pid():
        reachable, gw = gateway_status()
        if reachable and gw and tunnel_ok():
            return f"gateway via {gateway_host()} (up)"
        if reachable and gw:
            log("sshuttle running but the tunnel carries no traffic; rebuilding")
        stop_gateway()
    try:
        connect_gateway()
        return f"gateway via {gateway_host()}"
    except PvpnError as e:
        log(f"gateway unavailable: {e}; using local")
    r = connect_local(name_sub, keep_gui, headed)
    return f"local {r['name']} client={r['client']} server={r['server']}"


def status():
    try:
        print(pc("list"), end="")
    except PvpnError as e:
        print(f"local: {e}")
    pid = sshuttle_pid()
    if pid:
        t = probe_target()
        ok = "carrying traffic" if tunnel_ok() else f"NOT carrying traffic (probe {t[0]}:{t[1]})"
        print(f"sshuttle: pid {pid}, {ok}")
    else:
        print("sshuttle: down")
    if not gateway_host():
        print("gateway: not configured (GATEWAY_HOST)")
        return
    reachable, gw = gateway_status()
    if not reachable:
        print(f"gateway {gateway_host()}: unreachable")
    elif gw:
        print(f"gateway {gateway_host()}: connected {gw['client']} via {gw['server']} ({gw['online']})")
    else:
        print(f"gateway {gateway_host()}: reachable, VPN down")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--routes", action="store_true")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--gateway", action="store_true")
    ap.add_argument("--profile")
    ap.add_argument("--keep-gui", action="store_true")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    glogin.load_env() if (glogin.ENV_FILE.exists()) else None
    try:
        if a.routes:
            print(" ".join(routes()))
        elif a.status:
            status()
        elif a.add:
            add_profile()
            print(pc("list"), end="")
        elif a.stop:
            stop_gateway()
            stop_local(a.profile)
            print("stopped")
        else:
            mode = "local" if a.local else "gateway" if a.gateway else cfg("PVPN_MODE", "auto")
            print("connected: " + ensure(mode, a.profile, a.keep_gui, a.headed))
    except PvpnError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
