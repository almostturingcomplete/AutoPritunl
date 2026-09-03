#!/bin/sh
# Install AutoPritunl on this machine: venv, Chromium, pritunl-client (Linux), sshuttle
# sudoers (macOS), CLI symlinks, keepalive service. Idempotent. `--no-service` skips the service.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
cd "$ROOT"
[ -f .env ] || { echo "copy .env.example to .env and fill it in first"; exit 1; }
LABEL="$(sed -n 's/^SERVICE_LABEL=//p' .env)"; LABEL="${LABEL:-com.autopritunl.keepalive}"

echo "[install] venv + playwright"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt 2>&1 | grep -v notice || true
.venv/bin/playwright install chromium 2>&1 | tail -1
mkdir -p "$HOME/.cache/glogin"

if [ "$OS" = "Linux" ]; then
  if command -v dnf >/dev/null; then
    if ! command -v pritunl-client >/dev/null; then
      echo "[install] pritunl-client via dnf"
      . /etc/os-release; MAJ="${VERSION_ID%%.*}"; DIST=almalinux; [ "$ID" = ol ] && DIST=oraclelinux
      sudo tee /etc/yum.repos.d/pritunl.repo >/dev/null <<REPO
[pritunl]
name=Pritunl Stable Repository
baseurl=https://repo.pritunl.com/stable/yum/$DIST/$MAJ/
gpgcheck=1
enabled=1
REPO
      sudo rpm --import https://raw.githubusercontent.com/pritunl/pgp/master/pritunl_repo_pub.asc
      sudo dnf -y -q install pritunl-client
    fi
    echo "[install] chromium runtime libs"
    sudo dnf -y -q install nss nspr atk at-spi2-atk at-spi2-core cups-libs libdrm libxkbcommon \
      libXcomposite libXdamage libXrandr libXfixes libXext libX11 libxcb mesa-libgbm alsa-lib \
      pango cairo libxshmfence >/dev/null
  elif command -v apt-get >/dev/null; then
    if ! command -v pritunl-client >/dev/null; then
      echo "[install] pritunl-client via apt"
      . /etc/os-release
      echo "deb https://repo.pritunl.com/stable/apt $VERSION_CODENAME main" | sudo tee /etc/apt/sources.list.d/pritunl.list >/dev/null
      sudo apt-get -y install gnupg >/dev/null
      curl -fsSL https://raw.githubusercontent.com/pritunl/pgp/master/pritunl_repo_pub.asc | sudo gpg --dearmor -o /usr/share/keyrings/pritunl.gpg
      sudo sed -i 's#^deb #deb [signed-by=/usr/share/keyrings/pritunl.gpg] #' /etc/apt/sources.list.d/pritunl.list
      sudo apt-get update -qq && sudo apt-get -y install pritunl-client
    fi
    .venv/bin/playwright install-deps chromium
  fi
  sudo systemctl enable --now pritunl-client
  BIN="$HOME/.local/bin"; mkdir -p "$BIN"
  # gateway box runs the client itself
  grep -q '^PVPN_MODE=' .env.local 2>/dev/null || echo "PVPN_MODE=local" >> .env.local
else
  command -v sshuttle >/dev/null || brew install sshuttle
  if [ ! -f /etc/sudoers.d/sshuttle_auto ]; then
    echo "[install] sshuttle sudoers (Touch ID prompt)"
    sshuttle --sudoers-no-modify | sudo tee /etc/sudoers.d/sshuttle_auto >/dev/null
    sudo chmod 440 /etc/sudoers.d/sshuttle_auto
    sudo visudo -cf /etc/sudoers.d/sshuttle_auto >/dev/null
  fi
  BIN="$HOME/bin"; mkdir -p "$BIN"
fi

# macOS: send only VPN_DOMAIN lookups to the VPN resolver; sshuttle carries them to the gateway.
VPN_DNS="$(sed -n 's/^VPN_DNS=//p' .env)"; VPN_DOMAIN="$(sed -n 's/^VPN_DOMAIN=//p' .env)"
if [ "$OS" = "Darwin" ] && [ -n "$VPN_DNS" ] && [ -n "$VPN_DOMAIN" ]; then
  if ! grep -qs "nameserver $VPN_DNS" "/etc/resolver/$VPN_DOMAIN"; then
    echo "[install] /etc/resolver/$VPN_DOMAIN -> $VPN_DNS (sudo)"
    sudo mkdir -p /etc/resolver
    printf 'nameserver %s\n' "$VPN_DNS" | sudo tee "/etc/resolver/$VPN_DOMAIN" >/dev/null
  fi
fi

for n in glogin pvpn pvpn-keepalive; do ln -sf "$ROOT/bin/$n" "$BIN/$n"; done
echo "[install] linked $BIN/{glogin,pvpn,pvpn-keepalive}"

[ "$1" = "--no-service" ] && exit 0
if [ "$OS" = "Linux" ]; then
  mkdir -p "$HOME/.config/systemd/user"
  sed "s#__ROOT__#$ROOT#g; s#__HOME__#$HOME#g" service/autopritunl.service.tmpl > "$HOME/.config/systemd/user/autopritunl.service"
  sudo loginctl enable-linger "$USER"
  systemctl --user daemon-reload
  systemctl --user enable --now autopritunl
  systemctl --user restart autopritunl
  echo "[install] systemd --user autopritunl: $(systemctl --user is-active autopritunl)"
else
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  sed "s#__ROOT__#$ROOT#g; s#__HOME__#$HOME#g; s#__LABEL__#$LABEL#g" service/autopritunl.plist.tmpl > "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "[install] launchd $LABEL loaded"
fi
