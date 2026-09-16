#!/bin/bash
set -euo pipefail
# Liveness and recent companion-service pings, not chain-sync/staking readiness.
[[ -s /run/session-node.pids ]]
while IFS= read -r pid; do
  kill -0 "$pid"
done < /run/session-node.pids
rpc_port=22023
[[ ${NETWORK:-mainnet} != stagenet ]] || rpc_port=11023
info=$(curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$rpc_port/get_info")
if [[ ${NETWORK:-mainnet} == mainnet && ${ROLE:-node} == node ]]; then
  jq -e --argjson now "$(date +%s)" '
    .status == "OK" and
    ([.last_storage_server_ping, .last_lokinet_ping, .last_session_router_ping]
     | all(. != null and . > ($now - 300)))
  ' <<< "$info" > /dev/null
else
  jq -e '.status == "OK"' <<< "$info" > /dev/null
fi
