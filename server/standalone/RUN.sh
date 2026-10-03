#!/usr/bin/env bash
set -euo pipefail
package_root="$(cd "$(dirname "$0")" && pwd)"
action="${1:-start}"
case "$action" in install|start|status|stop) ;; *) echo 'Usage: bash RUN.sh [install|start|status|stop]' >&2; exit 1 ;; esac
command -v docker >/dev/null || { echo 'Install and start Docker Engine with the Compose plugin first.' >&2; exit 1; }
platform="$(docker info --format '{{.OSType}} {{.Architecture}}')"
case "$platform" in 'linux amd64'|'linux x86_64') ;; *) echo 'This package requires a Linux x86_64 Docker engine.' >&2; exit 1 ;; esac
compose_version="$(docker compose version --short)"
[[ "$compose_version" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+) ]] || { echo 'Cannot determine Docker Compose version.' >&2; exit 1; }
major="${BASH_REMATCH[1]}"; minor="${BASH_REMATCH[2]}"; patch="${BASH_REMATCH[3]}"
if (( major < 2 || (major == 2 && minor < 24) || (major == 2 && minor == 24 && patch < 4) )); then
  echo 'Docker Compose 2.24.4 or newer is required.' >&2; exit 1
fi
if [ "$action" = status ] || [ "$action" = stop ]; then
  [ -f "$package_root/runtime/install.json" ] || { echo 'This package has not been installed. Run bash RUN.sh start.'; exit 0; }
  cd "$package_root/app"
  if [ "$action" = status ]; then docker compose ps -a; else docker compose stop; fi
  exit
fi
[ -f "$package_root/setup.py" ] || { echo 'Run this launcher from the extracted standalone server package.' >&2; exit 1; }
images=(local-rag/backend:2.2.0 local-rag/frontend:2.2.0 local-rag/postgres:2.2.0 local-rag/redis:2.2.0 local-rag/chroma:2.2.0 local-rag/ollama:2.2.0 local-rag/gateway:2.2.0)
if [ ! -f "$package_root/runtime/install.json" ] || ! docker image inspect "${images[@]}" >/dev/null 2>&1; then
  echo 'Verifying packaged code, images, and models. This can take a few minutes.'
  (cd "$package_root" && sha256sum --check SHA256SUMS)
  echo 'Loading packaged Docker images locally.'
  docker load --input "$package_root/payload/images.tar.gz"
fi
mkdir -p "$package_root/runtime"
docker image inspect "${images[@]}" > "$package_root/runtime/image-inspection.json"
names=(localhost)
if command -v hostname >/dev/null 2>&1; then
  names+=("$(hostname)")
  for address in $(hostname -I 2>/dev/null || true); do
    if [[ "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then names+=("$address"); fi
  done
fi
helper=(docker run --rm --pull never --network none --mount "type=bind,source=$package_root,target=/package" --entrypoint python local-rag/backend:2.2.0 /package/setup.py prepare --host-root "$package_root")
for name in "${names[@]}"; do [ -z "$name" ] || helper+=(--server-name "$name"); done
"${helper[@]}"
cd "$package_root/app"
echo 'Starting the offline server and waiting for its health checks.'
docker compose up -d --no-build --pull never --wait --wait-timeout 600
docker compose run --rm --no-deps -T -v "$package_root:/package" api python /package/setup.py provision
docker run --rm --pull never --network none --mount "type=bind,source=$package_root,target=/package" --entrypoint python local-rag/backend:2.2.0 /package/setup.py show
echo 'The server is ready. Use bash RUN.sh status to inspect it or bash RUN.sh stop to stop it.'
