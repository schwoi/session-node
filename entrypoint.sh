#!/bin/bash

DATA_DIR=/var/lib/oxen
LOG_FILE=/var/log/oxen.log

NETWORK="${NETWORK:-mainnet}"
ROLE="${ROLE:-node}"

if [ -z "$SERVICE_NODE_IP_ADDRESS" ]; then
  for ip_service in \
    "https://api.ipify.org" \
    "https://ifconfig.me/ip" \
    "https://icanhazip.com"; do
    SERVICE_NODE_IP_ADDRESS=$(curl -s "$ip_service")
    if [ -n "$SERVICE_NODE_IP_ADDRESS" ]; then
      break
    fi
  done

  if [ -z "$SERVICE_NODE_IP_ADDRESS" ]; then
    echo "Error: Could not determine public IP address. Please set SERVICE_NODE_IP_ADDRESS environment variable."
    exit 1
  fi

  export SERVICE_NODE_IP_ADDRESS
fi

export DATA_DIR
export LOG_FILE

envsubst < /etc/oxen/oxen_template.conf > /etc/oxen/oxen.conf

# Network mode
if [ "$NETWORK" = "stagenet" ]; then
  echo "stagenet=1" >> /etc/oxen/oxen.conf
  echo "[config] Network: stagenet"
else
  echo "[config] Network: mainnet"
fi

# Role: proxy or node
if [ "$ROLE" = "proxy" ]; then
  echo "[config] Role: L2 proxy"

  # Generate proxy.txt from L2_PROXY_CLIENTS (newline or comma-separated pubkeys)
  if [ -n "$L2_PROXY_CLIENTS" ]; then
    echo "$L2_PROXY_CLIENTS" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' > /etc/oxen/proxy.txt
    echo "[config] Whitelisted $(wc -l < /etc/oxen/proxy.txt) client pubkey(s)"
  else
    touch /etc/oxen/proxy.txt
    echo "[config] Warning: No L2_PROXY_CLIENTS set — proxy.txt is empty"
  fi

  echo "l2-proxy=/etc/oxen/proxy.txt" >> /etc/oxen/oxen.conf

  if [ -n "$L2_PROXY_LOG" ]; then
    echo "log-level=l2_proxy=debug,l2_tracker=debug" >> /etc/oxen/oxen.conf
    echo "[config] L2 proxy debug logging enabled"
  fi
else
  echo "[config] Role: service node"

  # If L2_OXEND is set, use proxy instead of direct RPC provider
  if [ -n "$L2_OXEND" ]; then
    # Remove l2-provider lines from config (client nodes use l2-oxend instead)
    sed -i '/^l2-provider=/d' /etc/oxen/oxen.conf

    # Add l2-oxend entries (comma-separated: IP:PORT/PUBKEY,IP:PORT/PUBKEY)
    echo "$L2_OXEND" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | while read -r entry; do
      if [ -n "$entry" ]; then
        echo "l2-oxend=$entry" >> /etc/oxen/oxen.conf
      fi
    done
    echo "[config] Using L2 proxy instead of direct RPC provider"
  fi
fi

# Start oxen-storage-server in background (required for uptime proofs)
if [ "$ROLE" != "proxy" ]; then
  echo "[config] Starting oxen-storage-server..."
  oxen-storage-server \
    --oxend-rpc "ipc://$DATA_DIR/oxend.sock" \
    --data-dir "$DATA_DIR/storage" \
    --log-level info \
    &
  STORAGE_PID=$!
  echo "[config] oxen-storage-server started (PID $STORAGE_PID)"
fi

exec "$@"
