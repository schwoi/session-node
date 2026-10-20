#!/bin/bash
set -euo pipefail
# Liveness and recent companion-service pings, not chain-sync/staking readiness.
# While mainnet companions wait for oxend to synchronize, only oxend is running.
pids=/run/session-node.pids
[[ -s $pids || ! -s /run/session-node.syncing ]] || pids=/run/session-node.syncing
[[ -s $pids ]]
while IFS= read -r pid; do
  kill -0 "$pid"
done < "$pids"
rpc_port=22023
[[ ${NETWORK:-mainnet} != stagenet ]] || rpc_port=11023
info=$(curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$rpc_port/get_info")
if [[ ${NETWORK:-mainnet} == mainnet && ${ROLE:-node} == node && $pids == /run/session-node.pids ]]; then
  jq -e --argjson now "$(date +%s)" '
    .status == "OK" and
    ([.last_storage_server_ping, .last_lokinet_ping, .last_session_router_ping]
     | all(. != null and . > ($now - 300)))
  ' <<< "$info" > /dev/null
else
  jq -e '.status == "OK"' <<< "$info" > /dev/null
fi
