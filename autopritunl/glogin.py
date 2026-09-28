#!/usr/bin/env python3
"""Automated Google login with TOTP 2FA. No human input.

Config: <project>/.env (GMAIL, GPASS, GTOTP, optional GBACKUP), fallback
~/.config/glogin.env, or plain env vars.

Usage:
  glogin.py                 login (reuses saved session if still valid), write state
  glogin.py --check         exit 0 if saved session is logged in, else 1
  glogin.py --cookies       print cookies JSON to stdout
  glogin.py --cookie-header print "Cookie: ..." header for curl/requests
  glogin.py --totp          print current TOTP code only
  glogin.py --url URL       after login, open URL and print its final URL + title
  glogin.py --manual        headed; script fills email+password, then waits up to 5 min
                            for you to finish 2FA by hand (mobile prompt etc). Run once
                            so this browser profile gets trusted; later runs are headless.
  glogin.py --headed        show the browser window (default is headless)
  glogin.py --fresh         wipe profile and log in from scratch
  glogin.py --import-state F  seed the profile with cookies from a state.json exported
                            elsewhere (e.g. copy the trusted Mac session to a server)
  glogin.py --account NAME  use a second Google account: its own Chromium profile under
                            <state>/accounts/NAME/ and its own credentials from
                            ~/.config/glogin/NAME.env (or --env FILE). Accepts either
                            GMAIL/GPASS/GTOTP or EMAIL/PASSWORD/TOTP_KEY key names.
  glogin.py --accounts      list configured accounts

Outputs (default account; --account NAME nests these under accounts/NAME/):
  ~/.local/share/autopritunl/profile/      persistent Chromium profile (survives runs)
  ~/.local/share/autopritunl/state.json    Playwright storage_state (cookies + localStorage)
  ~/.local/share/autopritunl/cookies.json  cookies only, list of dicts
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pyotp
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

def _state_dir():
    """Where the durable state lives: the Chromium profile Google trusts as a device, the
    exported Pritunl profile, the session cookies. This used to be ~/.cache/glogin, but a
    cache directory is by definition disposable - anything that cleans caches is entitled
    to delete it, and one did, taking the trusted-device login with it. Keep it under
    XDG_DATA_HOME instead, migrating an existing directory on first run."""
    env = os.environ.get("AUTOPRITUNL_STATE") or os.environ.get("AUTOPRITUNL_CACHE")
    if env:
        return Path(os.path.expanduser(env))
    new = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "autopritunl"
    old = Path.home() / ".cache" / "glogin"
    new.mkdir(parents=True, exist_ok=True)
    # Move the contents, not the directory: the service writes its log into the new path
    # before anything else runs, so the new directory usually exists but is empty.
    if old.is_dir() and not (new / "profile").exists():
        for item in old.iterdir():
            target = new / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
    return new


CACHE = _state_dir()
PROFILE = CACHE / "profile"
STATE = CACHE / "state.json"
COOKIES = CACHE / "cookies.json"
ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
if not ENV_FILE.exists():
    ENV_FILE = Path.home() / ".config" / "glogin.env"
ACCOUNTS = CACHE / "accounts"
ACCOUNT_ENV_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "glogin"
ACCOUNT = None          # None = the original single-account layout, untouched
ACCOUNT_ENV = None      # extra env file layered on top of .env for that account


def slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "default"


def list_accounts():
    """(name, env_file_or_None, profile_exists) for every configured extra account."""
    names = set()
    if ACCOUNT_ENV_DIR.is_dir():
        names |= {f.stem for f in ACCOUNT_ENV_DIR.glob("*.env")}
    if ACCOUNTS.is_dir():
        names |= {d.name for d in ACCOUNTS.iterdir() if d.is_dir()}
    out = []
    for n in sorted(names):
        env = ACCOUNT_ENV_DIR / f"{n}.env"
        out.append((n, env if env.exists() else None, (ACCOUNTS / n / "profile").exists()))
    return out


def use_account(name=None, env_file=None):
    """Point the module at one account's own profile, state and credentials.

    The default account keeps the original paths so existing trusted sessions, pvpn and
    the keepalive service are unaffected; every other account gets its own subtree under
    <state>/accounts/<name>/ because Google ties the remembered-device cookie to a
    profile, and one profile cannot hold two signed-in identities for our purposes."""
    global ACCOUNT, ACCOUNT_ENV, CACHE, PROFILE, STATE, COOKIES
    name = name or os.environ.get("GLOGIN_ACCOUNT") or ""
    env_file = env_file or os.environ.get("GLOGIN_ENV") or ""
    if env_file:
        env_file = Path(os.path.expanduser(env_file))
        if not env_file.exists():
            sys.exit(f"no such env file: {env_file}")
        name = name or env_file.stem
    if not name or name in ("default", "primary"):
        ACCOUNT, ACCOUNT_ENV = None, None
        return None
    ACCOUNT = slug(name)
    ACCOUNT_ENV = Path(env_file) if env_file else ACCOUNT_ENV_DIR / f"{ACCOUNT}.env"
    CACHE = ACCOUNTS / ACCOUNT
    PROFILE = CACHE / "profile"
    STATE = CACHE / "state.json"
    COOKIES = CACHE / "cookies.json"
    CACHE.mkdir(parents=True, exist_ok=True)
    return ACCOUNT


LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https://myaccount.google.com/"
OK_URL = "myaccount.google.com"


def at_ok(page):
    return urlparse(page.url).hostname == OK_URL

EMAIL_SEL = "#identifierId, input[type=email]"
PASS_SEL = "input[name=Passwd], input[type=password]:not([aria-hidden=true])"
# Stable device signature: Chrome 151 on an Apple Silicon Mac. UA string is the same
# for Intel and ARM Macs; client hints carry arch=arm. Keep identical across runs so
# Google's "remembered device" cookie stays valid.
CHROME_MAJOR = "151"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36")
SEC_CH_UA = (f'"Chromium";v="{CHROME_MAJOR}", "Google Chrome";v="{CHROME_MAJOR}", '
             '"Not-A.Brand";v="99"')
MANUAL_WAIT_S = 300
# Hide WebAuthn so Google skips the passkey challenge and offers password + TOTP.
NO_WEBAUTHN = ("delete window.PublicKeyCredential; "
               "Object.defineProperty(navigator, 'credentials', {get: () => undefined});")


# Second-account env files are often written with plain names; accept both spellings.
ALIASES = {
    "EMAIL": "GMAIL", "PASSWORD": "GPASS", "TOTP_KEY": "GTOTP",
    "TOTP_SECRET": "GTOTP", "BACKUP_CODES": "GBACKUP",
}


def _read_env(path, override):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        k = ALIASES.get(k, k)
        v = v.strip().strip('"').strip("'")
        if override:
            os.environ[k] = v
        else:
            os.environ.setdefault(k, v)


def load_env():
    _read_env(ENV_FILE, override=False)
    _read_env(ROOT / ".env.local", override=True)  # per-machine overrides, gitignored
    if ACCOUNT_ENV:
        # override: the shared .env has already set the primary account's credentials
        if not ACCOUNT_ENV.exists():
            sys.exit(f"account {ACCOUNT!r} has no credentials at {ACCOUNT_ENV}")
        _read_env(ACCOUNT_ENV, override=True)
    missing = [k for k in ("GMAIL", "GPASS", "GTOTP") if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing config: {', '.join(missing)} (edit {ACCOUNT_ENV or ENV_FILE})")


def totp():
    return pyotp.TOTP(os.environ["GTOTP"].replace(" ", "")).now()


def log(msg):
    print(f"[glogin] {msg}", file=sys.stderr)


NEXT_SEL = ["#identifierNext", "button:has-text('Next')", "button:has-text('Continue')"]


def reach(page, wanted, done=None, steps=3, timeout=6000):
    """Wait for one of `wanted` (or `done()` to be true), clicking through any page that
    offers only a Next/Continue button on the way. Google inserts such pages at will:
    "Verify it's you ... sign in again" (/v3/signin/confirmidentifier) turned up between
    the account chooser and the password on 2026-09-28 and took the tunnel down, and the
    same shape can appear before 2FA. Returns the selector found, or True for done()."""
    for _ in range(steps + 1):
        sel = first_visible(page, wanted, timeout=timeout)
        if sel:
            return sel
        if done and done():
            return True
        nxt = first_visible(page, NEXT_SEL, timeout=1500)
        if not nxt:
            return None
        log("interstitial, clicking Next")
        page.locator(nxt).first.click()
        page.wait_for_timeout(2500)
    return None


def first_visible(page, selectors, timeout=8000):
    """Return the first selector that becomes visible, or None."""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible():
                    return sel
            except Exception:
                pass
        page.wait_for_timeout(250)
    return None


def dismiss_interstitials(page):
    """Skip 'add recovery phone', 'passkey', 'not now' style pages."""
    for _ in range(4):
        sel = first_visible(page, [
            "text=Not now", "text=Skip", "text=Cancel", "button:has-text('Not now')",
        ], timeout=1500)
        if not sel or at_ok(page):
            return
        try:
            page.locator(sel).first.click()
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            return


def is_logged_in(page):
    try:
        page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        return at_ok(page)
    except PWTimeout:
        return False


def wait_for_human(page):
    log(f"finish 2FA in the browser window; waiting up to {MANUAL_WAIT_S}s")
    deadline = time.time() + MANUAL_WAIT_S
    while time.time() < deadline:
        if at_ok(page):
            return
        page.wait_for_timeout(1000)
    page.screenshot(path=str(CACHE / "fail.png"))
    sys.exit(f"manual login not completed in {MANUAL_WAIT_S}s; at {page.url}")


def do_login(page, manual=False):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # email, or the account chooser if Google remembers a signed-out account
    log("email")
    email = os.environ["GMAIL"]
    choosers = [f"[data-identifier='{email}']", f"li:has-text('{email}')", f"text={email}"]
    sel = first_visible(page, [EMAIL_SEL, *choosers], timeout=30000)
    if sel == EMAIL_SEL:
        page.fill(EMAIL_SEL, email)
        page.keyboard.press("Enter")
    elif sel:
        log("account chooser")
        page.locator(sel).first.click()
    else:
        page.screenshot(path=str(CACHE / "fail.png"))
        sys.exit(f"no email input or account chooser at {page.url}; see {CACHE/'fail.png'}")

    # password
    log("password")
    if not reach(page, [PASS_SEL]):
        page.screenshot(path=str(CACHE / "fail.png"))
        sys.exit(f"no password field at {page.url}; see {CACHE/'fail.png'}")
    page.wait_for_timeout(500)
    page.fill(PASS_SEL, os.environ["GPASS"])
    page.keyboard.press("Enter")
    page.wait_for_timeout(2500)

    if manual:
        if not at_ok(page):
            wait_for_human(page)
        return

    # 2FA: either we land on myaccount (device trusted), the TOTP input, or a
    # challenge chooser where "Try another way" leads to the authenticator option.
    log("2fa")
    totp_sel = "input#totpPin, input[name=totpPin], input[type=tel]"

    def settled():
        return at_ok(page) or first_visible(page, [totp_sel], timeout=500)

    for _ in range(16):
        if settled():
            break
        page.wait_for_timeout(500)
    if at_ok(page):
        return
    if not first_visible(page, [totp_sel], timeout=500):
        reach(page, [totp_sel, "text=Try another way", "text=More options"],
              done=lambda: at_ok(page), timeout=3000)
    if at_ok(page):
        return
    if not first_visible(page, [totp_sel], timeout=1000):
        alt = first_visible(page, ["text=Try another way", "text=More options"], timeout=4000)
        if alt:
            page.locator(alt).first.click()
            page.wait_for_timeout(1500)
            opt = first_visible(page, [
                "text=Google Authenticator", "text=authenticator app",
                "li:has-text('Authenticator')", "div[role=link]:has-text('Authenticator')",
            ], timeout=6000)
            if opt:
                page.locator(opt).first.click()
                page.wait_for_timeout(1500)
    if at_ok(page):
        return
    if not first_visible(page, [totp_sel], timeout=8000):
        page.screenshot(path=str(CACHE / "fail.png"))
        sys.exit(f"no TOTP input found at {page.url}; see {CACHE/'fail.png'}")

    page.fill(totp_sel, totp())
    page.keyboard.press("Enter")
    page.wait_for_timeout(3000)

    # wrong-code retry once (clock skew) then backup codes
    if first_visible(page, ["text=Wrong code"], timeout=1500):
        log("wrong code, retrying")
        page.wait_for_timeout(2000)
        page.fill(totp_sel, totp())
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
    if first_visible(page, ["text=Wrong code"], timeout=1000) and os.environ.get("GBACKUP"):
        log("using backup code")
        alt = first_visible(page, ["text=Try another way"], timeout=3000)
        if alt:
            page.locator(alt).first.click()
            opt = first_visible(page, ["text=backup code"], timeout=5000)
            if opt:
                page.locator(opt).first.click()
                code = os.environ["GBACKUP"].split(",")[0].strip()
                page.wait_for_selector("input[type=tel], input[type=text]", timeout=10000)
                page.fill("input[type=tel], input[type=text]", code)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3000)

    dismiss_interstitials(page)
    try:
        page.wait_for_url(lambda u: urlparse(u).hostname == OK_URL, timeout=20000)
    except PWTimeout:
        page.screenshot(path=str(CACHE / "fail.png"))
        sys.exit(f"login did not reach {OK_URL}; at {page.url}; see {CACHE/'fail.png'}")


LOCK_WAIT_S = 120


def _lock_profile():
    """One Chromium on this profile at a time. A plain blocking flock wedged the keepalive
    for 12 hours once: a failed launch had leaked the previous lock descriptor inside the
    same process, and the next flock() waited on it for ever, with nothing in the log.
    Poll with a deadline instead, so a stuck lock becomes an error the caller can log."""
    lock = open(CACHE / "browser.lock", "w")
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except OSError:
            if time.time() >= deadline:
                lock.close()
                raise RuntimeError(f"{CACHE / 'browser.lock'} held for over {LOCK_WAIT_S}s "
                                   "(another glogin/pvpn on this profile, or a stale lock)")
            time.sleep(1)


def _launch(p, headless):
    kw = dict(headless=headless, channel="chromium", user_agent=UA, locale="en-US",
              viewport={"width": 1280, "height": 860},
              args=["--disable-blink-features=AutomationControlled"])
    try:
        return p.chromium.launch_persistent_context(str(PROFILE), **kw)
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
        # bin/* keep the browsers under .venv, out of ~/Library/Caches, after a cache
        # cleaner deleted them once. Whatever the location, put them back.
        log("Chromium is missing; installing it (playwright install chromium)")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=True, capture_output=True, text=True, timeout=300)
        return p.chromium.launch_persistent_context(str(PROFILE), **kw)


@contextlib.contextmanager
def browser(headless=True):
    """Yield (ctx, page) on the persistent, stealthed, WebAuthn-less profile."""
    CACHE.mkdir(parents=True, exist_ok=True)
    lock = _lock_profile()
    try:
        stealth = Stealth(
            navigator_platform_override="MacIntel",
            navigator_user_agent_override=UA,
            sec_ch_ua_override=SEC_CH_UA,
            navigator_vendor_override="Google Inc.",
            webgl_vendor_override="Google Inc. (Apple)",
            webgl_renderer_override="ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)",
        )
        with stealth.use_sync(sync_playwright()) as p:
            ctx = _launch(p, headless)
            ctx.add_init_script(NO_WEBAUTHN)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                yield ctx, page
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    finally:
        # Released here, not only after a successful launch: a launch that raised used
        # to skip the release, and the descriptor it leaked kept the lock held.
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def ensure_login(page, manual=False):
    """Log in if the saved session is dead. Returns True if a login was performed."""
    if is_logged_in(page):
        return False
    log("no valid session, logging in" + (" (manual)" if manual else ""))
    do_login(page, manual=manual)
    return True


def export_state(ctx):
    state = ctx.storage_state()
    STATE.write_text(json.dumps(state, indent=1))
    COOKIES.write_text(json.dumps(state["cookies"], indent=1))
    os.chmod(STATE, 0o600)
    os.chmod(COOKIES, 0o600)
    return state


def cookie_header(cookies, domain=".google.com"):
    """One value per name; prefer the shared .google.com cookie over host-scoped dupes."""
    by_name = {}
    for c in cookies:
        d = c.get("domain", "")
        if domain not in d:
            continue
        if c["name"] not in by_name or d == domain:
            by_name[c["name"]] = c["value"]
    return "Cookie: " + "; ".join(f"{k}={v}" for k, v in by_name.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--cookies", action="store_true")
    ap.add_argument("--cookie-header", action="store_true")
    ap.add_argument("--totp", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--import-state")
    ap.add_argument("--account", help="named second account (own profile + credentials)")
    ap.add_argument("--env", help="credentials file for --account (default ~/.config/glogin/<name>.env)")
    ap.add_argument("--accounts", action="store_true", help="list configured accounts")
    a = ap.parse_args()

    if a.accounts:
        print(f"{'default':<20} {ENV_FILE}  profile={'yes' if PROFILE.exists() else 'no'}")
        for name, env, has_profile in list_accounts():
            print(f"{name:<20} {env or '(no env file)'}  profile={'yes' if has_profile else 'no'}")
        return

    use_account(a.account, a.env)
    load_env()
    if a.totp:
        print(totp())
        return

    CACHE.mkdir(parents=True, exist_ok=True)
    if a.fresh and PROFILE.exists():
        shutil.rmtree(PROFILE)

    headless = not (a.headed or a.manual)
    with browser(headless) as (ctx, page):
        if a.import_state:
            cookies = json.loads(Path(a.import_state).read_text())["cookies"]
            ctx.add_cookies(cookies)
            log(f"imported {len(cookies)} cookies from {a.import_state}")

        if a.check:
            logged_in = is_logged_in(page)
            print("logged in" if logged_in else "not logged in")
            sys.exit(0 if logged_in else 1)

        if not ensure_login(page, manual=a.manual):
            log("session still valid")

        state = export_state(ctx)
        log(f"state -> {STATE}")

        if a.url:
            page.goto(a.url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            print(page.url)
            print(page.title())

    if a.cookies:
        print(json.dumps(state["cookies"], indent=1))
    elif a.cookie_header:
        print(cookie_header(state["cookies"]))
    else:
        n = len(state["cookies"])
        print(f"ok: {n} cookies, state={STATE}, cookies={COOKIES}")


if __name__ == "__main__":
    main()
