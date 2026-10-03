#!/usr/bin/env bash
# Connected preparation machine only. Export complete, immutable release artifacts.
set -euo pipefail
cd "$(dirname "$0")/../.."
python_bin="${PYTHON_BIN:-python3}"
command -v "$python_bin" >/dev/null || { echo 'Preparation requires Python 3, Docker, Compose v2, tar, gzip, and sha256sum. Set PYTHON_BIN if needed.' >&2; exit 1; }
"$python_bin" --version >/dev/null
bundle="${1:-offline/release-2.2.0}"
skip_build="${2:-}"
[ "$skip_build" = '' ] || [ "$skip_build" = '--skip-build' ] || [ "$skip_build" = '--refresh-images' ] || { echo 'Second argument must be --skip-build, --refresh-images, or omitted.' >&2; exit 1; }
[ -f .env ] || { echo 'Create .env before preparation to select local models. No credentials are included in the release.' >&2; exit 1; }
if [ -e "$bundle" ]; then
  [ "$skip_build" = '--refresh-images' ] || { echo 'Output already exists; choose a new release directory or --refresh-images after a validated rebuild.' >&2; exit 1; }
  (cd "$bundle" && sha256sum --check SHA256SUMS)
else
  [ "$skip_build" != '--refresh-images' ] || { echo 'Refresh requires an existing complete release.' >&2; exit 1; }
fi
mkdir -p "$bundle"
bundle="$(cd "$bundle" && pwd)"
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s\n' "$1"; fi
}
# Every pull has exactly one immutable source image. Shared application images are saved too.
while IFS=$'\t' read -r target source; do
  docker image inspect "$source" >/dev/null 2>&1 || docker pull --platform linux/amd64 "$source"
  docker tag "$source" "$target"
done < <("$python_bin" server/scripts/offline_tool.py image-refs)
if [ "$skip_build" = '' ]; then
  docker compose build api frontend
fi
docker compose config --quiet
images=(local-rag/backend:2.2.0 local-rag/frontend:2.2.0 local-rag/postgres:2.2.0 local-rag/redis:2.2.0 local-rag/chroma:2.2.0 local-rag/ollama:2.2.0 local-rag/gateway:2.2.0)
: > "$bundle/images.tsv.next"
for image in "${images[@]}"; do
  details="$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image")"
  [[ "$details" = *' linux/amd64' ]] || { echo "Wrong target architecture: $image" >&2; exit 1; }
  printf '%s\t%s\n' "$image" "${details%% *}" >> "$bundle/images.tsv.next"
done
docker image inspect "${images[@]}" > "$bundle/images.inspect.next.json"
"$python_bin" server/scripts/offline_tool.py image-manifest --inspect "$(native_path "$bundle/images.inspect.next.json")" --tags "$(native_path "$bundle/images.tsv.next")" --output "$(native_path "$bundle/images.json.next")"
rm -f "$bundle/images.inspect.next.json"
docker save "${images[@]}" | gzip -1 > "$bundle/images.tar.gz.next"
# The directory contains model manifests and referenced blobs only, never Ollama private keys.
models="${OLLAMA_MODELS_PATH:-$("$python_bin" -c 'from pathlib import Path; import sys; sys.path.insert(0,"server/scripts"); from offline_tool import read_env; e=read_env(Path(".env")); print(Path(e.get("OLLAMA_HOST_PATH", "data/ollama"))/"models")')}"
if [ "$skip_build" = '--refresh-images' ]; then
  "$python_bin" server/scripts/offline_tool.py models-check --models "$(native_path "$models")" --release-report "$(native_path "$bundle/models.json")"
  "$python_bin" server/scripts/offline_tool.py apply-runtime-config --runtime "$(native_path "$bundle/runtime.json")" --env .env
else
  "$python_bin" server/scripts/offline_tool.py models-pack --models "$(native_path "$models")" --env .env --output "$(native_path "$bundle/models.tar.gz")"
fi
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$bundle/application.tar.gz" docker-compose.yml .env.example redis/redis.conf server/scripts deploy/compose deploy/nginx deploy/postgres deploy/images.lock.json deploy/OFFLINE_DEPLOYMENT.md deploy/BACKUP_RESTORE.md deploy/SPLIT_DEPLOYMENT.md README.md LOCAL_TEST.md Makefile
cp server/scripts/import-offline.sh "$bundle/import-offline.sh"
cp deploy/images.lock.json "$bundle/sources.json"
printf 'release=2.2.0\nplatform=linux/amd64\n' > "$bundle/release.txt"
mv -f "$bundle/images.tar.gz.next" "$bundle/images.tar.gz"
mv -f "$bundle/images.tsv.next" "$bundle/images.tsv"
mv -f "$bundle/images.json.next" "$bundle/images.json"
(cd "$bundle" && sha256sum images.tar.gz models.tar.gz models.json runtime.json images.tsv images.json application.tar.gz sources.json release.txt import-offline.sh > SHA256SUMS)
echo "Complete offline release: $bundle"
echo 'Transfer the whole directory and import with server/scripts/import-offline.sh on the disconnected server.'
