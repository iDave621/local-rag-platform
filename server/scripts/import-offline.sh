#!/usr/bin/env bash
# Run from a source checkout, or copy this script alongside the verified release.
set -euo pipefail
[ "$#" -ge 2 ] || { echo 'Usage: import-offline.sh /path/to/release /absolute/installation/path' >&2; exit 1; }
bundle="$(cd "$1" && pwd)"
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s\n' "$1"; fi
}
case "$2" in /*) target="$2" ;; *) echo 'Installation path must be absolute.' >&2; exit 1 ;; esac
case "$target" in /|/etc|/usr|/var|/home|/root) echo 'Choose a dedicated application directory.' >&2; exit 1 ;; esac
(cd "$bundle" && sha256sum --check SHA256SUMS)
docker load --input "$(native_path "$bundle/images.tar.gz")"
images=()
while IFS=$'\t' read -r name expected; do images+=("$name"); done < "$bundle/images.tsv"
mkdir -p "$target"
target="$(cd "$target" && pwd)"
runtime_args=()
[ -f "$target/.env" ] || runtime_args+=(--new-env)
docker run --rm --pull never --network none -v "$bundle:/bundle:ro" -v "$target:/target" --entrypoint python local-rag/backend:2.2.0 -c 'import tarfile; from pathlib import Path; import sys; a=tarfile.open("/bundle/application.tar.gz"); target=Path("/target"); members=a.getmembers(); unsafe=[m.name for m in members if not (target/m.name).resolve().is_relative_to(target) or m.issym() or m.islnk() or m.isdev()]; unsafe and sys.exit("Unsafe release archive"); a.extractall(target, filter="data")'
inspection="deploy/.image-inspection-$RANDOM.json"
docker image inspect "${images[@]}" > "$target/$inspection"
docker run --rm --pull never --network none -v "$target:/release:ro" -v "$bundle:/bundle:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py image-verify --inspect "/release/$inspection" --manifest /bundle/images.json
rm -f "$target/$inspection"
cd "$target"
bash server/scripts/init-config.sh
docker run --rm --pull never --network none -v "$target:/release" -v "$bundle:/bundle:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py apply-runtime-config --runtime /bundle/runtime.json --env /release/.env "${runtime_args[@]}"
# Preserve existing model stores; validate them before choosing to reuse them.
mkdir -p data/ollama
if [ -d data/ollama/models ] && [ -n "$(find data/ollama/models -type f -print -quit)" ]; then
  docker run --rm --pull never --network none -v "$target:/release:ro" -v "$bundle:/bundle:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py models-check --models /release/data/ollama/models --release-report /bundle/models.json
else
  docker run --rm --pull never --network none -v "$target:/release" -v "$bundle:/bundle:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py safe-extract /bundle/models.tar.gz /release/data/ollama
  docker run --rm --pull never --network none -v "$target:/release:ro" -v "$bundle:/bundle:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py models-check --models /release/data/ollama/models --release-report /bundle/models.json
fi
cp "$bundle/images.tsv" "$bundle/images.json" "$bundle/models.json" "$bundle/runtime.json" "$bundle/SHA256SUMS" "$target/deploy/"
echo "Release imported into $target. Existing .env retained. Set LAN hostname, generate local TLS, then run bash server/scripts/start-local.sh."
