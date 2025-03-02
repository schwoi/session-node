#!/bin/bash

echo "Searching for session node containers"
container_names=$(docker ps --format '{{.Names}}' | grep "session-node")

if [ -z "$container_names" ]; then
  echo "No containers with 'session-node' in the name found."
  exit 0
fi

for container_name in $container_names; do
  echo "Running command on container: $container_name"
  docker exec -it "$container_name" oxend-stagenet status
done

echo "Status check completed."
