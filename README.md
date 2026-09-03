# AutoPritunl

Unattended Pritunl VPN for setups that use Google SSO with 2-factor auth. Works from a
laptop, from a Linux VPS acting as a shared gateway, and from phones through that gateway.
No browser clicks, no phone taps after the one-time setup.

What it does:

- **Headless Google login** (`glogin`): password + TOTP in a persistent Chromium profile.
  Bypasses the passkey prompt, handles the account chooser, exports cookies for reuse.
- **Pritunl from the CLI** (`pvpn`): starts the profile with `pritunl-client`, completes the
  single-use SSO link headlessly, waits for the tunnel. Modes: `local`, `gateway`, `auto`.
- **Keepalive service** (`pvpn-keepalive`): launchd on macOS, systemd on Linux, reconnects with backoff.
- **Gateway mode**: a VPS holds the VPN; laptops route only the VPN subnets through it
  with `sshuttle`, including DNS for the VPN's search domain. Phones use a WireGuard hub on
  the same VPS.

Everything host-specific lives in `.env`.

```
laptop  --sshuttle (tcp + dns for VPN_DOMAIN)-->  gateway VPS  --pritunl-client-->  company network
phone   --WireGuard udp 51820 ----------------->  (wg0 -> nft masquerade -> tun0)
laptop  --pritunl-client (local mode, fallback when the gateway is down)---------->  company network
```

## Requirements

| Host | Needs |
|---|---|
| macOS laptop | Python 3.10+, Homebrew, Pritunl.app (its bundled `pritunl-client` CLI is used; the GUI is quit automatically) |
| Linux gateway (AlmaLinux/RHEL 9+, Ubuntu/Debian) | Python 3.10+, sudo, ssh reachable from the laptop with a passphrase-less key |
| Pritunl server | Google SSO, user allowed to export a profile from the user portal |
| Google account | 2-Step Verification with an **Authenticator app** enrolled (Google Prompt / passkey cannot be automated) |

## Install (laptop)

```bash
git clone https://github.com/almostturingcomplete/AutoPritunl ~/AutoPritunl
cd ~/AutoPritunl
cp .env.example .env && chmod 600 .env     # fill in, see "Configuration"
./install.sh                               # venv, Chromium, sshuttle + sudoers, /etc/resolver, symlinks, launchd
glogin                                     # first Google login (password + TOTP), profile becomes a trusted device
pvpn                                       # connect; auto mode uses the gateway if configured and up
pvpn --status
```
`install.sh` is idempotent. `--no-service` skips the keepalive service.

## Install (gateway VPS)

```bash
# on the laptop, after .env has GATEWAY_HOST (an ssh alias) and PRITUNL_PROFILE_FILE
./deploy-gateway.sh            # push, clone/pull on the host, rsync .env + profile, install.sh,
                               # first Google login on the host, systemd --user keepalive (lingering)
```
On Linux `install.sh` also adds the Pritunl repo, installs `pritunl-client` and the Chromium
runtime libraries, and writes `PVPN_MODE=local` to `.env.local` (the gateway runs the VPN
itself). The first connect on a production box is worth guarding with a timer that stops the
profile unless you confirm ssh still works; the pushed routes are normally specific subnets.

## Phones and other devices (WireGuard through the gateway)

```bash
ssh <gateway> ~/AutoPritunl/wireguard/hub/setup-hub.sh   # wg0, nft NAT into the Pritunl tunnel; prints server pubkey
# put WG_ENDPOINT and WG_SERVER_PUBKEY into .env, then on the laptop:
wireguard/add-device.sh phone 10                          # peer added on the gateway; devices/phone.conf + phone.png
open wireguard/devices/phone.png                          # scan in the WireGuard app
```
Devices split-tunnel only `VPN_ROUTES` and use `VPN_DNS` for lookups.
`wireguard/devices/` holds private keys and is gitignored. Open UDP `WG_PORT` in the cloud firewall.

## Configuration (`.env`)

| Key | Meaning |
|---|---|
| `GMAIL`, `GPASS` | Google account for SSO |
| `GTOTP` | base32 TOTP secret (see Secrets) |
| `GBACKUP` | optional backup codes, comma separated |
| `PRITUNL_PROFILE_FILE` | exported profile `.zip`/`.tar`/`.ovpn` from the Pritunl user portal (preferred) |
| `PRITUNL_PROFILE` | `pritunl://host/ku/...` link; these expire, so the file is preferred |
| `VPN_ROUTES` | space-separated CIDRs the VPN pushes (gateway mode and WireGuard devices) |
| `VPN_DNS`, `VPN_DOMAIN` | resolver and search domain pushed by the VPN |
| `VPN_TUN` | tunnel interface on the Linux gateway (`tun0`) |
| `GATEWAY_HOST` | ssh alias of the gateway; empty = no gateway, local mode only |
| `GATEWAY_INSTALL_DIR` | repo path on the gateway, relative to `$HOME` |
| `PVPN_MODE` | `auto` / `local` / `gateway` |
| `PVPN_INTERVAL` | keepalive check interval in seconds |
| `SERVICE_LABEL` | launchd label on macOS |
| `WG_NET`, `WG_NET6`, `WG_PORT` | WireGuard hub addressing |
| `WG_ENDPOINT`, `WG_SERVER_PUBKEY` | WireGuard hub public endpoint and key |

Per-host overrides go in `.env.local` (same syntax, wins over `.env`, gitignored).
Env var `AUTOPRITUNL_CACHE` moves the state directory (default `~/.cache/glogin`).

### Secrets: how to obtain them

**TOTP secret.** Google Account > Security > 2-Step Verification > Authenticator > Set up.
Google shows a QR code; the secret is in it. Extract with `zbarimg` (`brew install zbar`):

```bash
zbarimg -q --raw qr.png                                   # otpauth://totp/...?secret=XXXX&issuer=Google
# if you saved it as a data:image/png;base64,... string:
sed -E 's/^data:[^,]*,//' qr.b64 | tr -d '\n ' | base64 -d > qr.png
SECRET=$(zbarimg -q --raw qr.png | sed -nE 's/.*secret=([^&]*).*/\1/p'); echo "GTOTP=$SECRET" >> .env
glogin --totp        # type this code into Google's "Enter code" box to finish enrollment
```
The secret only becomes active after that confirmation step. Delete the QR files afterwards.
Do not read the email from a shell variable named `USERNAME`: zsh makes it read-only and
silently substitutes the login name.

**Pritunl profile.** Log in to `https://<pritunl-host>/login` with SSO and download the
profile (a zip with one `.ovpn`). Point `PRITUNL_PROFILE_FILE` at it; `pvpn --add` converts it.

**VPN routes and DNS.** Connect once (any client) and read:
macOS `netstat -rn -f inet` (rows whose gateway is the VPN gateway), Linux `ip route | grep tun0`,
and the client log lines `dhcp-option DNS ...` / `dhcp-option DOMAIN ...`.

## Commands

```
glogin                 login if the saved session is dead, export cookies
glogin --check         exit 0 if logged in
glogin --cookie-header "Cookie: ..." for curl/requests      --cookies  JSON
glogin --totp          current 6-digit code
glogin --manual        headed, you finish 2FA by hand once (e.g. Google insists on a phone prompt)
glogin --fresh         wipe the browser profile and log in from scratch

pvpn                   connect in PVPN_MODE           pvpn --local / --gateway   force a mode
pvpn --status          local table, sshuttle, gateway  pvpn --stop                disconnect all
pvpn --add             import the profile              pvpn --routes              print VPN_ROUTES
pvpn-keepalive         the service loop (foreground)

deploy-gateway.sh [host]          wireguard/hub/setup-hub.sh          wireguard/add-device.sh <name> <octet>
```

Logs: `~/.cache/glogin/keepalive.log`. Failure screenshots: `~/.cache/glogin/*fail.png`.

## How it works, briefly

- Google: Playwright Chromium with `playwright-stealth`, a fixed desktop UA, and an init
  script that removes `window.PublicKeyCredential` so Google offers the password path.
  Success is detected by the final hostname, never by substring (the `continue=` param lies).
- Pritunl: `pritunl-client start` prints `https://<host>/key/request?state=...`. That link is
  single-use and regenerated on every reconnect attempt, so the browser is logged in *before*
  `start` and loads the link immediately. Flow: account chooser, optional re-auth
  (password/TOTP), OAuth consent, callback, `/success`. The Pritunl GUI app also opens that
  link in your default browser and burns it, so `pvpn` quits the GUI first.
- Gateway: `sshuttle -r <gateway> --ns-hosts VPN_DNS --to-ns VPN_DNS VPN_ROUTES...`, passwordless
  via sshuttle's own sudoers snippet. macOS `/etc/resolver/<VPN_DOMAIN>` sends only that
  domain's lookups to `VPN_DNS`, which sshuttle captures.
- One Pritunl connection per user: every device gets the same client IP, so `auto` mode stops
  the local client whenever the gateway is usable.
- Each host logs in to Google on its own. Reusing session cookies from a second IP gets the
  session signed out everywhere.

## Gotchas collected while building this

- Google pushes a "match the number" prompt to your phone on every new-device login even when
  the TOTP path is used. Dismiss it.
- `pritunl://.../ku/<token>` links from the portal return 404 after a while; the CLI reports
  "Invalid profile uri". Use the exported file.
- On a Docker host `iptables -t nat` may refuse ("chain incompatible", iptables-nft). The hub
  uses native nftables in its own table.
- Some internal hosts drop ICMP; verify with TCP, not ping.
- GNU `sed` on macOS via Homebrew: `-i` not `-i ''`.
- `pkill -f <string>` over ssh kills your own remote shell if the string is in its command line.

## License

MIT
