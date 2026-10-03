#!/usr/bin/env bash
# Fresh recovery installation only. Refuses to overwrite nonempty data or database state.
set -euo pipefail
[ "$#" -ge 2 ] || { echo 'Usage: restore.sh /path/to/backup /absolute/fresh/recovery/path' >&2; exit 1; }
backup="$(cd "$1" && pwd)"
tool_source="$(cd "$(dirname "$0")" && pwd)/offline_tool.py"
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}
case "$2" in /*) target="$2" ;; *) echo 'Recovery path must be absolute.' >&2; exit 1 ;; esac
case "$target" in /|/etc|/usr|/var|/home|/root) echo 'Choose a dedicated recovery directory.' >&2; exit 1 ;; esac
[ ! -e "$target" ] || [ -z "$(find "$target" -mindepth 1 -print -quit)" ] || { echo 'Recovery directory must be empty. Existing data is never overwritten.' >&2; exit 1; }
(cd "$backup" && sha256sum --check SHA256SUMS)
if [ ! -f "$backup/images.json" ]; then
  # Legacy backups had IDs only; never waive this check without content metadata.
  while read -r image expected; do
    [ "$(docker image inspect --format '{{.Id}}' "$image")" = "$expected" ] || { echo "Load the legacy backup image before restoring: $image" >&2; exit 1; }
  done < "$backup/images.txt"
fi
mkdir -p "$target"
target="$(cd "$target" && pwd)"
docker run --rm --pull never --network none -v "$backup:/backup:ro" -v "$target:/target" --entrypoint python local-rag/backend:2.2.0 -c 'import tarfile; from pathlib import Path; import sys; a=tarfile.open("/backup/configuration.tar.gz"); target=Path("/target"); members=a.getmembers(); unsafe=[m.name for m in members if not (target/m.name).resolve().is_relative_to(target) or m.issym() or m.islnk() or m.isdev()]; unsafe and sys.exit("Unsafe backup archive"); a.extractall(target, filter="data")'
if [ -f "$backup/images.json" ]; then
  images=()
  while read -r image identifier; do images+=("$image"); done < "$backup/images.txt"
  inspection="deploy/.image-inspection-$RANDOM.json"
  docker image inspect "${images[@]}" > "$target/$inspection"
  docker run --rm --pull never --network none -v "$tool_source:/tool.py:ro" -v "$target:/release:ro" -v "$backup:/backup:ro" --entrypoint python local-rag/backend:2.2.0 /tool.py image-verify --inspect "/release/$inspection" --manifest /backup/images.json
  rm -f "$target/$inspection"
  cp "$backup/images.json" "$target/deploy/images.json"
fi
# A unique project isolates recovery volumes and permits test restoration beside an existing server.
project="local-rag-recovery-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
cd "$target"
mkdir -p data
# Recovery always writes to its own data directory; config credentials/DB names remain unchanged.
printf '\nHOST_DATA_PATH=%s\nOLLAMA_HOST_PATH=%s\nTLS_CERT_PATH=%s\nCOMPOSE_PROJECT_NAME=%s\n' "$(native_path "$target/data")" "$(native_path "$target/data/ollama")" "$(native_path "$target/deploy/tls")" "$project" >> .env
chmod 600 .env
compose=(docker compose --project-name "$project" --env-file .env)
compose_files="$(docker run --rm --pull never --network none -v "$tool_source:/tool.py:ro" -v "$target:/release:ro" --entrypoint python local-rag/backend:2.2.0 /tool.py compose-files --env /release/.env --root /release)"
while IFS= read -r file; do compose+=(-f "$file"); done <<< "$compose_files"
"${compose[@]}" create --no-build --pull never postgres chroma redis
postgres="$("${compose[@]}" ps -q --all postgres)"
chroma="$("${compose[@]}" ps -q --all chroma)"
redis="$("${compose[@]}" ps -q --all redis)"
chroma_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$chroma")"
redis_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$redis")"
[ -n "$chroma_volume" ] && [ -n "$redis_volume" ] || { echo 'Recovery volumes not created.' >&2; exit 1; }
# Extract with the standard-library safe extractor carried by the application image.
for item in chroma redis; do
  if [ "$item" = chroma ]; then volume="$chroma_volume"; else volume="$redis_volume"; fi
  docker run --rm --pull never --network none -v "$backup:/backup:ro" -v "$volume:/target" --entrypoint python local-rag/backend:2.2.0 -c 'import tarfile,sys; from pathlib import Path; target=Path("/target"); a=tarfile.open(sys.argv[1]); bad=[m.name for m in a.getmembers() if not (target/m.name).resolve().is_relative_to(target) or m.issym() or m.islnk() or m.isdev()]; bad and sys.exit("Unsafe archive"); a.extractall(target,filter="data")' "/backup/$item.tar.gz"
done
docker run --rm --pull never --network none -v "$backup:/backup:ro" -v "$target/data:/target" --entrypoint python local-rag/backend:2.2.0 -c 'import tarfile,sys; from pathlib import Path; target=Path("/target"); a=tarfile.open("/backup/documents.tar.gz"); bad=[m.name for m in a.getmembers() if not (target/m.name).resolve().is_relative_to(target) or m.issym() or m.islnk() or m.isdev()]; bad and sys.exit("Unsafe archive"); a.extractall(target,filter="data")'
"${compose[@]}" up -d --no-build --pull never --wait postgres
docker exec -i "$postgres" sh -c 'exec pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$backup/postgres.dump"
printf '%s\n' "$project" > deploy/recovery-project.txt
echo "Restored SQL, Chroma, Redis, documents, and configuration into $target."
echo "Recovery project: $project. Load the same release models into data/ollama; set free HTTPS ports in .env."
echo "Then run docker compose --project-name $project up -d --no-build --pull never --wait and verify citations before directing users to this server."
