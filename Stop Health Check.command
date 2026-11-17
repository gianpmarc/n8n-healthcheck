#!/bin/bash
# Double-click on macOS to stop n8n-healthcheck and remove its container.
cd "$(dirname "$0")" || exit 1

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running, so n8n-healthcheck is not running either."
  exit 0
fi

docker compose down && echo "n8n-healthcheck stopped."
