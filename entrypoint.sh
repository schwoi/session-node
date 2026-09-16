#!/bin/bash
set -Eeuo pipefail
umask 027

# Allow one-off diagnostic commands without requiring node configuration.
if [[ ${1:-} != oxend || ${2:-} != --non-interactive ]]; then
  exec "$@"
fi

fail() { echo "Error: $*" >&2; exit 1; }
NETWORK=${NETWORK:-mainnet}
ROLE=${ROLE:-node}
[[ $NETWORK == mainnet || $NETWORK == stagenet ]] || fail 'NETWORK must be mainnet or stagenet'
[[ $ROLE == node || $ROLE == proxy ]] || fail 'ROLE must be node or proxy'
DATA_DIR=/var/lib/oxen
# An explicit data-dir suppresses oxend's default network subdirectory. Keep
# existing keys in place; Compose provides a separate volume for each network.
NODE_DIR=$DATA_DIR
if [[ $NETWORK == stagenet ]]; then
  P2P_PORT=${P2P_PORT:-11022}
  QUORUMNET_PORT=${QUORUMNET_PORT:-11025}
else
  P2P_PORT=${P2P_PORT:-22022}
  QUORUMNET_PORT=${QUORUMNET_PORT:-22025}
fi
STORAGE_LMQ_PORT=${STORAGE_LMQ_PORT:-22020}
STORAGE_HTTPS_PORT=${STORAGE_HTTPS_PORT:-22021}
LOKINET_PORT=${LOKINET_PORT:-1090}
SESSION_ROUTER_PORT=${SESSION_ROUTER_PORT:-1190}
for name in P2P_PORT QUORUMNET_PORT STORAGE_LMQ_PORT STORAGE_HTTPS_PORT LOKINET_PORT SESSION_ROUTER_PORT; do
  value=${!name}
  if [[ ! $value =~ ^[1-9][0-9]{0,4}$ ]] || (( value > 65535 )); then
    fail "$name must be a port from 1 to 65535"
  fi
done

[[ -n ${L2_PROVIDER:-} || -n ${L2_OXEND:-} ]] || fail 'Set L2_PROVIDER or L2_OXEND'
[[ -z ${L2_PROVIDER:-} || -z ${L2_OXEND:-} ]] || fail 'Set only one of L2_PROVIDER and L2_OXEND'
if [[ $ROLE == proxy ]]; then
  [[ -n ${L2_PROVIDER:-} ]] || fail 'An L2 proxy requires L2_PROVIDER'
fi

valid_ipv4() {
  local part
  local -a parts
  [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  IFS=. read -ra parts <<< "$1"
  for part in "${parts[@]}"; do
    [[ ${#part} -le 3 ]] && (( 10#$part <= 255 )) || return 1
  done
}
if [[ $ROLE == node ]]; then
  if [[ -z ${SERVICE_NODE_IP_ADDRESS:-} ]]; then
    for endpoint in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
      SERVICE_NODE_IP_ADDRESS=$(curl -4fsS --connect-timeout 5 --max-time 10 "$endpoint") || continue
      valid_ipv4 "$SERVICE_NODE_IP_ADDRESS" && break
    done
  fi
  valid_ipv4 "${SERVICE_NODE_IP_ADDRESS:-}" || fail 'Set SERVICE_NODE_IP_ADDRESS to your public IPv4 address'
fi

mkdir -p "$NODE_DIR" /etc/oxen
# Migrate data written by older root-running images, without traversing all data
# on every normal restart. Auxiliary daemons share the package's oxend account.
if [[ $(stat -c %u "$DATA_DIR") != "$(id -u _loki)" ]]; then
  chown -R _loki:_loki "$DATA_DIR"
fi
chown _loki:_loki "$NODE_DIR"
conf=/etc/oxen/oxen.conf
cat > "$conf" <<CONFIG
data-dir=$DATA_DIR
log-file=$NODE_DIR/oxen.log
p2p-bind-port=$P2P_PORT
rpc-bind-ip=127.0.0.1
CONFIG
[[ $NETWORK != stagenet ]] || echo 'stagenet=1' >> "$conf"
if [[ $ROLE == node ]]; then
  cat >> "$conf" <<CONFIG
service-node=1
service-node-public-ip=$SERVICE_NODE_IP_ADDRESS
quorumnet-port=$QUORUMNET_PORT
CONFIG
else
  # Proxy mode is a regular full node, with an authenticated LMQ listener.
  echo "lmq-curve=tcp://0.0.0.0:$QUORUMNET_PORT" >> "$conf"
  : > /etc/oxen/proxy.txt
  while IFS= read -r key; do
    [[ -n $key ]] || continue
    [[ $key =~ ^[[:xdigit:]]{64}$ ]] || fail 'L2_PROXY_CLIENTS must contain 64-character hex public keys'
    echo "$key" >> /etc/oxen/proxy.txt
  done < <(printf '%s\n' "${L2_PROXY_CLIENTS:-}" | tr ',' '\n')
  echo 'l2-proxy=/etc/oxen/proxy.txt' >> "$conf"
  [[ ${L2_PROXY_LOG:-0} != 1 ]] || echo 'log-level=l2_proxy=debug,l2_tracker=debug' >> "$conf"
fi
for name in L2_PROVIDER L2_OXEND; do
  while IFS= read -r entry; do
    [[ -n $entry ]] || continue
    [[ $entry != *$'\r'* && $entry != *' '* && $entry != *YOUR_API_KEY* ]] || fail "$name contains an invalid or placeholder entry"
    if [[ $name == L2_PROVIDER ]]; then
      [[ $entry == https://* || $entry == http://* ]] || fail 'L2_PROVIDER entries must be HTTP(S) URLs'
      echo "l2-provider=$entry" >> "$conf"
    else
      echo "l2-oxend=$entry" >> "$conf"
    fi
  done < <(printf '%s\n' "${!name:-}" | tr ',' '\n')
done
chown root:_loki "$conf"
[[ $ROLE != proxy ]] || chown root:_loki /etc/oxen/proxy.txt

pids=()
# Called by the signal and EXIT traps below.
# shellcheck disable=SC2329
stop_services() {
  trap - EXIT
  trap '' TERM INT
  if (( ${#pids[@]} )); then
    kill -TERM "${pids[@]}" 2>/dev/null || true
    # Bound shutdown even if a daemon becomes stuck; Compose allows 120 seconds.
    for ((attempt=0; attempt<100; attempt++)); do
      kill -0 "${pids[@]}" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "${pids[@]}" 2>/dev/null || true
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  fi
}
trap 'stop_services; exit 0' TERM INT
trap stop_services EXIT
start() { gosu _loki "$@" & pids+=("$!"); }

start "$@"
if [[ $ROLE == node && $NETWORK == mainnet ]]; then
  [[ -c /dev/net/tun ]] || fail 'Mainnet routers require --device /dev/net/tun and --cap-add NET_ADMIN'
  # Wildcard router binds fall back to the advertised public IP upstream,
  # which is not assigned inside a bridge-network container. Bind its real IP.
  ROUTER_BIND_IP=${ROUTER_BIND_IP:-}
  if [[ -z $ROUTER_BIND_IP ]]; then
    for address in $(hostname -i); do
      if valid_ipv4 "$address"; then ROUTER_BIND_IP=$address; break; fi
    done
  fi
  valid_ipv4 "$ROUTER_BIND_IP" || fail 'Set ROUTER_BIND_IP to the container IPv4 address'
  mkdir -p "$DATA_DIR/storage" "$DATA_DIR/lokinet" "$DATA_DIR/session-router"
  chown _loki:_loki "$DATA_DIR/storage" "$DATA_DIR/lokinet" "$DATA_DIR/session-router"
  cat > /etc/oxen/storage.conf <<CONFIG
ip=0.0.0.0
port=$STORAGE_HTTPS_PORT
lmq-port=$STORAGE_LMQ_PORT
data-dir=$DATA_DIR/storage
oxend-rpc=ipc://$NODE_DIR/oxend.sock
CONFIG
  cat > /etc/oxen/lokinet.ini <<CONFIG
[router]
data-dir=$DATA_DIR/lokinet
public-ip=$SERVICE_NODE_IP_ADDRESS
public-port=$LOKINET_PORT
[bind]
inbound=$ROUTER_BIND_IP:$LOKINET_PORT
[network]
ifname=lokitun0
[lokid]
enabled=true
rpc=ipc://$NODE_DIR/oxend.sock
CONFIG
  cat > /etc/oxen/session-router.ini <<CONFIG
[router]
data-dir=$DATA_DIR/session-router
public-ip=$SERVICE_NODE_IP_ADDRESS
public-port=$SESSION_ROUTER_PORT
[bind]
listen=$ROUTER_BIND_IP:$SESSION_ROUTER_PORT
[oxend]
rpc=ipc://$NODE_DIR/oxend.sock
CONFIG
  chown root:_loki /etc/oxen/storage.conf /etc/oxen/lokinet.ini /etc/oxen/session-router.ini
  # The data volume can retain a socket from the previous container. Wait for
  # the new daemon to serve RPC, not just for that socket file to exist, before
  # companions request their identities. HTTP RPC starts after OxenMQ.
  ready=0
  deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    kill -0 "${pids[0]}" 2>/dev/null || fail 'oxend exited during startup'
    if [[ -S $NODE_DIR/oxend.sock ]] &&
      curl --fail --silent --max-time 2 http://127.0.0.1:22023/get_info |
        jq -e '.status == "OK"' > /dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  (( ready )) || fail 'Timed out waiting for oxend RPC readiness'
  start oxen-storage --config-file /etc/oxen/storage.conf
  # Match the native router units' capabilities while retaining an unprivileged UID.
  for router in lokinet session-router; do
    setpriv --reuid=_loki --regid=_loki --init-groups \
      --inh-caps=+net_admin,+net_bind_service \
      --ambient-caps=+net_admin,+net_bind_service \
      "$router" -r "/etc/oxen/$router.ini" &
    pids+=("$!")
    if [[ $router == lokinet ]]; then
      # Both routers auto-select a free private subnet. Let Lokinet install its
      # tunnel route before Session Router selects one, avoiding a startup race
      # where both choose 172.16.0.1/16 and one fails with a conflicting IP.
      ready=0
      deadline=$((SECONDS + 120))
      while (( SECONDS < deadline )); do
        kill -0 "${pids[-1]}" 2>/dev/null || fail 'Lokinet exited during startup'
        if awk '$1 == "lokitun0" && $2 != "00000000" { found=1 } END { exit !found }' /proc/net/route; then
          ready=1
          break
        fi
        sleep 1
      done
      (( ready )) || fail 'Timed out waiting for Lokinet tunnel readiness'
    fi
  done
fi

printf '%s\n' "${pids[@]}" > /run/session-node.pids
echo "[config] Started $NETWORK $ROLE (${#pids[@]} services)"
# Exit on any service failure so Docker's restart policy restarts the whole node.
status=0
wait -n "${pids[@]}" || status=$?
echo '[config] A required service exited; stopping the container' >&2
(( status != 0 )) || status=1
exit "$status"
