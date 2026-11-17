#!/bin/bash
# Double-click on macOS to build and start n8n-healthcheck, then open the UI.
# On Linux run it from a terminal: bash "Start Health Check.command"
cd "$(dirname "$0")" || exit 1
URL=http://localhost:8080

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop, then run this again."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop, wait until it is ready, then run this again."
  exit 1
fi

echo "Building and starting n8n-healthcheck (the first run takes a minute or two)..."
# --wait returns once the container's health check passes (the UI answers).
if docker compose build && docker compose up -d --wait --wait-timeout 60; then
  if command -v open >/dev/null 2>&1; then open "$URL"; else xdg-open "$URL"; fi
  echo
  echo "n8n-healthcheck is running at $URL"
  echo "To stop it: double-click \"Stop Health Check.command\", or run: docker compose down"
  exit 0
fi

echo
echo "n8n-healthcheck did not start, or its UI did not answer within 60 seconds. Last log lines:"
docker compose logs --tail 20
echo "To clean up: double-click \"Stop Health Check.command\", or run: docker compose down"
exit 1
