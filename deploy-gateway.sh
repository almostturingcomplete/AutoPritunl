#!/bin/sh
# Push this repo to the gateway host, install there, and copy secrets (.env, never in git)
# plus the exported Pritunl profile. The gateway then does its own Google login (TOTP).
# Usage: ./deploy-gateway.sh [ssh-host]   (default: GATEWAY_HOST from .env)
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cfg() { sed -n "s/^$1=//p" "$ROOT/.env" | head -1; }
HOST="${1:-$(cfg GATEWAY_HOST)}"; [ -n "$HOST" ] || { echo "no host: pass one or set GATEWAY_HOST"; exit 1; }
DEST="$(cfg GATEWAY_INSTALL_DIR)"; DEST="${DEST:-AutoPritunl}"
PROFILE="$(eval echo "$(cfg PRITUNL_PROFILE_FILE)")"
REPO="$(git -C "$ROOT" remote get-url origin)"
git -C "$ROOT" push -q origin HEAD
ssh "$HOST" "set -e; if [ -d $DEST/.git ]; then git -C $DEST pull -q; else git clone -q $REPO $DEST; fi; mkdir -p ~/.cache/glogin"
rsync -q --chmod=600 "$ROOT/.env" "$HOST:$DEST/.env"
# the gateway is its own VPN endpoint: local mode, no upstream gateway
ssh "$HOST" "grep -q '^GATEWAY_HOST=' $DEST/.env.local 2>/dev/null || echo 'GATEWAY_HOST=' >> $DEST/.env.local"
if [ -n "$PROFILE" ] && [ -f "$PROFILE" ]; then
  rsync -q --chmod=600 "$PROFILE" "$HOST:.cache/glogin/$(basename "$PROFILE")"
  ssh "$HOST" "grep -q '^PRITUNL_PROFILE_FILE=' $DEST/.env.local 2>/dev/null || echo 'PRITUNL_PROFILE_FILE=~/.cache/glogin/$(basename "$PROFILE")' >> $DEST/.env.local"
fi
ssh "$HOST" "cd $DEST && ./install.sh && (~/.local/bin/glogin --check || ~/.local/bin/glogin) && ~/.local/bin/pvpn --status"
