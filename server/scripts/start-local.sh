#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo 'Run init-config.sh before starting.' >&2; exit 1; }
[ -s deploy/tls/server.crt ] && [ -s deploy/tls/server.key ] || { echo 'Run generate-lan-tls.sh with the server LAN hostname/IP first.' >&2; exit 1; }
docker run --rm --pull never --network none -v "$PWD:/release:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py config-check --env /release/.env
docker compose config --quiet
docker compose up -d --no-build --pull never --wait --wait-timeout 600
docker compose exec -T api python -m app.cli health --component all
echo 'Offline services ready over HTTPS. Create your first admin with: docker compose exec api python -m app.cli create-user --username admin --role admin'
