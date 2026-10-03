#!/usr/bin/env bash
# Single-server coordinated snapshot. Writers are paused while SQL, Chroma, Redis and docs are saved.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
destination="${1:-offline/backups/$(date -u +%Y%m%dT%H%M%SZ)}"
[ ! -e "$destination" ] || { echo 'Backup destination exists; choose a new directory.' >&2; exit 1; }
mkdir -p "$destination"
destination="$(cd "$destination" && pwd)"
running=()
while IFS= read -r service; do [ -z "$service" ] || running+=("$service"); done < <(docker compose ps --services --status running)
resume() {
  if [ "${#running[@]}" -gt 0 ]; then
    docker compose up -d --no-build --pull never "${running[@]}"
  fi
}
trap resume EXIT
api="$(docker compose ps -q --all api)"
chroma="$(docker compose ps -q --all chroma)"
redis="$(docker compose ps -q --all redis)"
[ -n "$api" ] && [ -n "$chroma" ] && [ -n "$redis" ] || { echo 'Create the application containers before backup.' >&2; exit 1; }
data_source="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$api")"
case "$destination/" in "$data_source/"*) echo 'Keep backup output outside the application data directory.' >&2; exit 1 ;; esac
chroma_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$chroma")"
redis_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$redis")"
[ -n "$chroma_volume" ] && [ -n "$redis_volume" ] || { echo 'Expected named Chroma/Redis volumes were not found.' >&2; exit 1; }
docker compose stop frontend api chat-worker background-worker cleanup-worker sync-worker
docker compose stop chroma redis
docker compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$destination/postgres.dump"
docker run --rm --pull never --network none -v "$PWD:/release:ro" -v "$destination:/backup" -v "$chroma_volume:/source:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py tree-pack /source /backup/chroma.tar.gz
docker run --rm --pull never --network none -v "$PWD:/release:ro" -v "$destination:/backup" -v "$redis_volume:/source:ro" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py tree-pack /source /backup/redis.tar.gz
docker run --rm --pull never --network none --volumes-from "$api:ro" -v "$PWD:/release:ro" -v "$destination:/backup" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py data-pack --data /data --output /backup/documents.tar.gz
config=(.env .env.example docker-compose.yml redis/redis.conf deploy/compose deploy/nginx deploy/postgres deploy/images.lock.json deploy/OFFLINE_DEPLOYMENT.md deploy/BACKUP_RESTORE.md deploy/SPLIT_DEPLOYMENT.md server/scripts README.md LOCAL_TEST.md Makefile)
[ ! -d deploy/tls ] || config+=(deploy/tls)
[ ! -d deploy/pki ] || config+=(deploy/pki)
tar -czf "$destination/configuration.tar.gz" "${config[@]}"
docker compose config --images | sort -u | while IFS= read -r image; do printf '%s %s\n' "$image" "$(docker image inspect --format '{{.Id}}' "$image")"; done > "$destination/images.txt"
images=()
while read -r image identifier; do images+=("$image"); done < "$destination/images.txt"
docker image inspect "${images[@]}" > "$destination/images.inspect.json"
docker run --rm --pull never --network none -v "$PWD:/release:ro" -v "$destination:/backup" --entrypoint python local-rag/backend:2.2.0 /release/server/scripts/offline_tool.py image-manifest --inspect /backup/images.inspect.json --tags /backup/images.txt --output /backup/images.json
rm -f "$destination/images.inspect.json"
printf 'release=2.2.0\ncreated_utc=%s\nproject=local-rag-platform\n' "$(date -u +%FT%TZ)" > "$destination/backup.txt"
(cd "$destination" && sha256sum postgres.dump chroma.tar.gz redis.tar.gz documents.tar.gz configuration.tar.gz images.txt images.json backup.txt > SHA256SUMS)
echo "Coordinated backup complete: $destination"
echo 'This backup includes passwords, sessions, documents, and private TLS keys. Keep it access restricted.'
