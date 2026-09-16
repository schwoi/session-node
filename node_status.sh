#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
while IFS= read -r service; do
  echo "Status: $service"
  docker compose exec -T "$service" oxend --config-file=/etc/oxen/oxen.conf status
done < <(docker compose ps --services --status running)
