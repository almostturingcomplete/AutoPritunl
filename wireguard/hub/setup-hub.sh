#!/bin/sh
# Run ON the gateway host (Linux, as a sudo user). Creates the WireGuard hub wg0 that
# forwards peer traffic into the Pritunl tunnel (tun0). Idempotent.
# Reads WG_NET, WG_NET6, WG_PORT, VPN_TUN from ../../.env
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$DIR/../.." && pwd)"
cfg() { sed -n "s/^$1=//p" "$ROOT/.env" 2>/dev/null | head -1; }
NET="$(cfg WG_NET)"; NET="${NET:-10.9.0}"; NET6="$(cfg WG_NET6)"; NET6="${NET6:-fd09::}"
PORT="$(cfg WG_PORT)"; PORT="${PORT:-51820}"; TUN="$(cfg VPN_TUN)"; TUN="${TUN:-tun0}"
command -v wg >/dev/null || sudo dnf -y -q install wireguard-tools 2>/dev/null || sudo apt-get -y install wireguard-tools
sudo mkdir -p /etc/wireguard && sudo chmod 700 /etc/wireguard
sed "s#__WG_NET__#$NET#g; s#__TUN__#$TUN#g" "$DIR/wgnat.nft.tmpl" | sudo tee /etc/wireguard/wgnat.nft >/dev/null; sudo chmod 600 /etc/wireguard/wgnat.nft
if [ ! -f /etc/wireguard/wg0.conf ]; then
  PRIV="$(wg genkey)"
  sudo sh -c "umask 077; cat > /etc/wireguard/wg0.conf" <<CONF
[Interface]
# WireGuard hub: phones/laptops split-tunnel company subnets through tun0 (Pritunl)
Address = $NET.1/24, ${NET6}1/64
ListenPort = $PORT
PrivateKey = $PRIV
PostUp = nft -f /etc/wireguard/wgnat.nft
PostDown = nft delete table ip wgnat
CONF
fi
sudo sysctl -q -w net.ipv4.ip_forward=1
echo net.ipv4.ip_forward=1 | sudo tee /etc/sysctl.d/99-wg-forward.conf >/dev/null
sudo systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || true
sudo systemctl restart wg-quick@wg0
echo "wg0 $(systemctl is-active wg-quick@wg0); server pubkey: $(sudo wg show wg0 public-key)"
echo "put WG_SERVER_PUBKEY and WG_ENDPOINT=<public-host>:$PORT into .env, then wireguard/add-device.sh"
