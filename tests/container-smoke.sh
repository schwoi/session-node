#!/bin/bash
# Real daemon integration, isolated from the internet and production node data.
set -euo pipefail
image=${1:-session-node:review}
prefix="session-smoke-$$"
network="$prefix-net"
volume="$prefix-data"
containers=()
cleanup() {
  for container in "${containers[@]}"; do docker rm -f "$container" >/dev/null 2>&1 || true; done
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
[[ -c /dev/net/tun ]] || fail 'Mainnet tests require /dev/net/tun on the Docker host'
docker network create --internal "$network" >/dev/null
docker volume create "$volume" >/dev/null
docker run --rm --network none "$image" dpkg-query -W \
  session-service-node oxen-storage-server lokinet-router session-router-relay
expect_invalid() {
  local expected=$1 output
  shift
  if output=$(docker run --rm --network none "$@" "$image" 2>&1); then
    fail "Accepted invalid configuration: $expected"
  fi
  [[ $output == *"$expected"* ]] || fail "Expected '$expected', got: $output"
}
expect_invalid 'NETWORK must be' -e NETWORK=invalid
expect_invalid 'ROLE must be' -e ROLE=invalid
expect_invalid 'Set L2_PROVIDER or L2_OXEND'
expect_invalid 'Set only one' -e L2_PROVIDER=http://localhost -e L2_OXEND=localhost:1/key
expect_invalid 'must be a port' -e P2P_PORT=70000
expect_invalid 'public IPv4' -e SERVICE_NODE_IP_ADDRESS=999.1.1.1 -e L2_PROVIDER=http://localhost
expect_invalid 'placeholder' -e SERVICE_NODE_IP_ADDRESS=8.8.8.8 -e L2_PROVIDER=https://example.com/YOUR_API_KEY
expect_invalid '64-character hex' -e ROLE=proxy -e L2_PROVIDER=http://localhost -e L2_PROXY_CLIENTS=bad
start() {
  local name=$1
  shift
  containers+=("$name")
  docker run -d --name "$name" --network "$network" --security-opt no-new-privileges:true \
    -e SERVICE_NODE_IP_ADDRESS=8.8.8.8 -e L2_PROVIDER=http://127.0.0.1:8545 "$@" "$image" >/dev/null
}
await_health() {
  local name=$1
  for ((attempt=0; attempt<120; attempt++)); do
    if docker exec "$name" /healthcheck.sh >/dev/null 2>&1; then return; fi
    [[ $(docker inspect -f '{{.State.Running}}' "$name") == true ]] || break
    sleep 1
  done
  docker logs --tail 50 "$name" >&2
  fail "$name did not become healthy"
}
assert_processes() {
  docker exec "$1" bash -c '
    set -e
    mapfile -t pids < /run/session-node.pids
    [[ ${#pids[@]} == "$1" ]]
    for pid in "${pids[@]}"; do
      uid=$(awk "/^Uid:/ {print \$2}" "/proc/$pid/status")
      [[ $uid != 0 ]]
    done
    [[ $(awk "/^CapEff:/ {print \$2}" "/proc/${pids[0]}/status") == 0000000000000000 ]]
  ' -- "$2"
}
assert_ports() {
  local name=$1
  shift
  docker exec "$name" bash -c '
    set -e
    for spec in "$@"; do
      proto=${spec%:*}; port=${spec#*:}
      hex=$(printf "%04X" "$port")
      awk -v suffix=":$hex" '\''$2 ~ suffix "$" { found=1 } END { exit !found }'\'' "/proc/net/$proto" "/proc/net/${proto}6"
    done
  ' -- "$@"
}
stop_cleanly() {
  docker stop -t 30 "$1" >/dev/null
  [[ $(docker inspect -f '{{.State.ExitCode}}' "$1") == 0 ]] || fail "$1 did not stop cleanly"
}
main="$prefix-main"
start "$main" --device /dev/net/tun --cap-add NET_ADMIN -v "$volume:/var/lib/oxen"
await_health "$main"
assert_processes "$main" 4
assert_ports "$main" tcp:22020 udp:22020 tcp:22021 tcp:22022 tcp:22025 udp:1090 udp:1190
keys_before=$(docker exec "$main" sha256sum /var/lib/oxen/key_ed25519 /var/lib/oxen/key_bls)
# A companion failure must stop the entire container with a failing exit code.
docker exec "$main" bash -c 'kill -TERM "$(sed -n "2p" /run/session-node.pids)"'
for ((attempt=0; attempt<45; attempt++)); do
  [[ $(docker inspect -f '{{.State.Running}}' "$main") == true ]] || break
  sleep 1
done
[[ $(docker inspect -f '{{.State.Running}}' "$main") == false ]] || fail 'Container survived storage failure'
[[ $(docker inspect -f '{{.State.ExitCode}}' "$main") != 0 ]] || fail 'Storage failure produced successful exit'
docker rm "$main" >/dev/null
# Recreation preserves keys and applies all actual offset listeners.
start "$main" --device /dev/net/tun --cap-add NET_ADMIN -v "$volume:/var/lib/oxen" \
  -e P2P_PORT=22032 -e QUORUMNET_PORT=22035 -e STORAGE_LMQ_PORT=22030 \
  -e STORAGE_HTTPS_PORT=22031 -e LOKINET_PORT=1091 -e SESSION_ROUTER_PORT=1191
await_health "$main"
[[ $(docker exec "$main" sha256sum /var/lib/oxen/key_ed25519 /var/lib/oxen/key_bls) == "$keys_before" ]] || fail 'Node identity changed on recreation'
assert_ports "$main" tcp:22030 udp:22030 tcp:22031 tcp:22032 tcp:22035 udp:1091 udp:1191
stop_cleanly "$main"
stage="$prefix-stage"
start "$stage" -e NETWORK=stagenet
await_health "$stage"
assert_processes "$stage" 1
assert_ports "$stage" tcp:11022 tcp:11025
docker exec "$stage" bash -c 'set -e; test -f /var/lib/oxen/key_ed25519; curl -fsS http://127.0.0.1:11023/get_info | jq -e '\''.nettype == "stagenet"'\'' >/dev/null'
stop_cleanly "$stage"
proxy="$prefix-proxy"
start "$proxy" -e ROLE=proxy -e QUORUMNET_PORT=22125 \
  -e L2_PROXY_CLIENTS=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
await_health "$proxy"
assert_processes "$proxy" 1
assert_ports "$proxy" tcp:22125
docker exec "$proxy" bash -c 'set -e; curl -fsS http://127.0.0.1:22023/get_info | jq -e '\''.service_node == false'\'' >/dev/null; test "$(wc -l < /etc/oxen/proxy.txt)" = 1'
stop_cleanly "$proxy"
echo 'PASS: invalid config, mainnet services, offset ports, persisted keys, service failure, stagenet, proxy, graceful shutdown'
