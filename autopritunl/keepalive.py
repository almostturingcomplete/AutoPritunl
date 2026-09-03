#!/usr/bin/env python3
"""Keep the VPN up. Runs forever under launchd (macOS) or systemd (Linux).

Every PVPN_INTERVAL seconds (default 30) calls pvpn.ensure(PVPN_MODE). On failure
backs off exponentially up to 5 minutes. Logs one line per state change.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import glogin  # noqa: E402
import pvpn  # noqa: E402


def main():
    if glogin.ENV_FILE.exists():
        glogin.load_env()
    interval = int(os.environ.get("PVPN_INTERVAL", "30"))
    mode = os.environ.get("PVPN_MODE", "auto")
    last = None
    fails = 0
    pvpn.log(f"keepalive start mode={mode} interval={interval}s")
    while True:
        try:
            state = pvpn.ensure(mode)
            fails = 0
            if state != last:
                pvpn.log(f"up: {state}")
                last = state
            time.sleep(interval)
        except pvpn.PvpnError as e:
            fails += 1
            wait = min(300, 15 * 2 ** min(fails, 5))
            pvpn.log(f"down ({e}); retry in {wait}s")
            last = None
            time.sleep(wait)
        except Exception as e:  # never die; launchd/systemd would just restart us anyway
            fails += 1
            wait = min(300, 15 * 2 ** min(fails, 5))
            pvpn.log(f"error {type(e).__name__}: {e}; retry in {wait}s")
            last = None
            time.sleep(wait)


if __name__ == "__main__":
    main()
