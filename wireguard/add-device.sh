#!/bin/sh
# Add a WireGuard peer on the gateway host and write a split-tunnel device config + QR.
# Usage: wireguard/add-device.sh <name> <last-octet>     e.g. wireguard/add-device.sh phone 10
# Device gets WG_NET.<octet>. Only the company subnets (VPN_ROUTES) go through the tunnel.
set -e
NAME="${1:?usage: $0 <name> <octet>}"; OCTET="${2:?usage: $0 <name> <octet>}"
DIR="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$DIR")"
cfg() { sed -n "s/^$1=//p" "$ROOT/.env" | head -1; }
HOST="$(cfg GATEWAY_HOST)"; [ -n "$HOST" ] || { echo "set GATEWAY_HOST in .env"; exit 1; }
NET="$(cfg WG_NET)"; NET="${NET:-10.9.0}"; NET6="$(cfg WG_NET6)"; NET6="${NET6:-fd09::}"
ENDPOINT="$(cfg WG_ENDPOINT)"; SERVER_PUB="$(cfg WG_SERVER_PUBKEY)"
DNS="$(cfg VPN_DNS)"; ROUTES="$(cfg VPN_ROUTES | tr ' ' ',' | sed 's/,/, /g')"
[ -n "$ENDPOINT" ] && [ -n "$SERVER_PUB" ] || { echo "set WG_ENDPOINT and WG_SERVER_PUBKEY in .env"; exit 1; }
IP="$NET.$OCTET"; IP6="$NET6$OCTET"
PRIV="$(wg genkey)"; PUB="$(printf '%s' "$PRIV" | wg pubkey)"

ssh "$HOST" "sudo wg set wg0 peer '$PUB' allowed-ips '$IP/32,$IP6/128' && \
  sudo sh -c \"printf '\n[Peer]\n# $NAME\nPublicKey = $PUB\nAllowedIPs = $IP/32, $IP6/128\n' >> /etc/wireguard/wg0.conf\""

OUT="$DIR/devices/$NAME.conf"; umask 077
cat > "$OUT" <<CONF
[Interface]
Address = $IP/32, $IP6/128
PrivateKey = $PRIV
DNS = $DNS, 8.8.8.8

[Peer]
PublicKey = $SERVER_PUB
Endpoint = $ENDPOINT
AllowedIPs = $NET.1/32, $ROUTES
PersistentKeepalive = 25
CONF
command -v qrencode >/dev/null && qrencode -o "$DIR/devices/$NAME.png" -r "$OUT" && echo "QR: $DIR/devices/$NAME.png"
echo "config: $OUT  (peer $PUB added on $HOST)"
