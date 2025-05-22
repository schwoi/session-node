#!/bin/bash

DATA_DIR=/var/lib/oxen/oxen
LOG_FILE=/var/log/oxen/oxen.log

if [ -z "$SERVICE_NODE_IP_ADDRESS" ]; then
  # Try to get public IP from public IP detection services
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

exec "$@"