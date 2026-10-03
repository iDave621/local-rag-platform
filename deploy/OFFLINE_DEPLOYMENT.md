# Disconnected installation

The release targets **Linux AMD64** containers. Install Docker Engine and Compose v2.24.4+ from your approved offline OS packages before importing this application. The application bundle does not install or reconfigure the host OS, Docker, firewall, client certificate trust, or GPU drivers. This guide covers preparation and the manual Linux Bash importer. For the single-file standalone package on Linux or Windows Docker Desktop, use [installation](../docs/INSTALLATION.md). Connected preparation requires Docker/Python 3/tar/gzip/sha256sum and the same CPU architecture.

## Prepare on a connected machine

1. Run `bash server/scripts/init-config.sh`. It creates `.env` with independent random credentials and never changes an existing `.env`. If `.env` already exists, set `POSTGRES_PASSWORD` and `REDIS_PASSWORD` to random alphanumeric strings of at least 24 characters. Existing database credentials must remain consistent with existing data. New releases should be prepared in their own checkout/data directory.
2. Keep the default model names or select explicit local Ollama model names in `.env`. Vision is disabled by default. If enabled, the vision model is required too.
3. Download the infrastructure images by their pinned `deploy/images.lock.json` digests and tag them with their listed `local-rag/...:2.2.0` names. The preparation script does this automatically; to download models before running it, a connected Linux shell can run:

   ```bash
   while IFS=$'\t' read -r target source; do
     docker pull --platform linux/amd64 "$source"
     docker tag "$source" "$target"
   done < <(python3 server/scripts/offline_tool.py image-refs)
   docker compose up -d --no-build --pull never ollama
   docker compose exec ollama ollama pull qwen3:4b-instruct
   docker compose exec ollama ollama pull embeddinggemma
   ```

4. Build and test the application with the procedures in [TESTING.md](../docs/TESTING.md). Base images have immutable digests, Python has a complete transitive version lock, and npm uses `package-lock.json` with `npm ci`. OS packages are installed only at build time; the shipped Docker image IDs freeze the actual release including those packages. Rebuilds require revalidation and a new approved release manifest; an exported release is the installable artifact.
5. Run `bash server/scripts/prepare-offline-images.sh`. Specify a different output directory as the first argument if one already exists. After a validated build, a second argument `--skip-build` exports the current verified images. If application acceptance requires a repair/rebuild after a complete model package already exists, `--refresh-images` verifies that package, retains its unchanged models, and refreshes images/deployment files/checksums from the rebuilt release. Do not change source or image tags during export.

The resulting directory contains all seven Docker images (shared backend, frontend, PostgreSQL, Redis, Chroma, Ollama and the optional split-server gateway), portable image content fingerprints and image-ID provenance, model manifests and every referenced blob, deployment files, and `SHA256SUMS`. Content fingerprints cover platform, ordered filesystem layers and runtime configuration; they remain stable across Docker Desktop/containerd and classic Linux Engine even when engine image IDs differ. Every model blob is checked against its Ollama digest before export. Ollama private keys, `.env`, uploaded documents and local TLS private keys are excluded. Transfer the **whole** directory, which includes the standalone `import-offline.sh` bootstrap script. Do not transfer model blobs independently of their manifests.

## Import on the disconnected server

```bash
bash /media/release-2.2.0/import-offline.sh /media/release-2.2.0 /srv/local-rag
cd /srv/local-rag
```

Import verifies all artifact hashes, loads every image, verifies portable image content fingerprints, safely extracts deployment files, and verifies every model blob and model manifest. Existing `.env` is retained, and an existing model store must match the incoming release before it can be reused. Import never deletes data. A new installation places models at `data/ollama/models`; if customizing `HOST_DATA_PATH` or `OLLAMA_HOST_PATH`, relocate the corresponding data before starting and use absolute host paths.

The bundle's `runtime.json` records model roles and whether optional vision was enabled, without credentials. A new target `.env` receives those packaged model settings automatically; an existing `.env` must match them and is never silently changed. The exported `models.json` lists every included model and digest. Optional vision requires its included model on both machines. Models must already be present before the one-shot dependency check succeeds.

## Local HTTPS and client trust

```bash
bash server/scripts/generate-lan-tls.sh rag.example.local 192.168.10.20
bash server/scripts/start-local.sh
docker compose exec api python -m app.cli create-user --username admin --role admin
```

Use the server DNS names and IPv4 addresses that clients actually use. The tool creates a local CA in `deploy/pki/` and a 397-day leaf certificate in `deploy/tls/`. It can run OpenSSL from the imported backend image, so the target does not need to install OpenSSL. The CA private key stays outside the containers; only the leaf key and public CA certificate are mounted. Existing leaf certificates are retained. To renew, move the leaf certificate/key to an access-restricted archive, then run the tool again using the same CA and correct server names.

Install **only `deploy/tls/ca.crt`** as a trusted root on each approved client machine. On Windows, import it into Trusted Root Certification Authorities using the certificate management UI or your organization policy. On Linux, use your distribution's local CA trust procedure. Keep `deploy/pki/ca.key` private and backed up; never distribute it to clients. An existing organization-issued server certificate can replace `server.crt`/`server.key` and avoids creating a new CA.

Default LAN access is HTTPS port 443. HTTP port 80 binds to localhost and redirects to HTTPS. Change `HTTPS_PORT`, `HTTP_REDIRECT_PORT`, `WEB_BIND_ADDRESS`, and `TLS_CERT_PATH` as needed. With a nonstandard HTTPS port, open the explicit `https://host:port` URL; the port-80 redirect assumes standard 443. `COOKIE_SECURE=true` is enforced by Compose even when an older `.env` contains `false`.

The explicitly named `deploy/compose/local-http.yml` override exposes plain HTTP only on host loopback and disables secure cookies for development. Use it only for local smoke checks. It requires no client certificate trust.

## Operations

`start-local.sh` uses `docker compose up --no-build --pull never --wait`, validates dependencies/models, and reports startup failure. The same backend image is shared across the API and every worker. Daemons restart after a host/daemon reboot; database initialization and readiness checks are one-shot containers. Health checks validate dependencies, required models, and worker heartbeat freshness. An `Exited (0)` initializer/dependency checker is expected.

Configure Docker to start at boot through the host OS. Do not run `docker compose down --volumes` on an existing installation. Use `docker compose logs --tail=200` and the admin processing screen to diagnose failed jobs. `docker compose exec api python -m app.cli retry-job JOB_ID` retries a retained failed job. `repair-documents` repairs historical timestamp-derived document identities after a backup.

Existing single-server installations with old placeholder service passwords can rotate them without replacing the database. Take a coordinated backup, pause the application writers in a maintenance window, run the helper using the current credentials, and recreate services with the updated configuration:

```bash
docker compose stop frontend api chat-worker background-worker cleanup-worker sync-worker
docker compose run --rm --no-deps -v "$PWD:/release" --entrypoint python api /release/server/scripts/rotate-local-credentials.py --env /release/.env
docker compose up -d --no-build --pull never --force-recreate --wait --wait-timeout 600
```

The helper changes the PostgreSQL role password and atomically updates `.env` with independent random database/Redis passwords without printing them. Redis applies its new password when recreated; its persisted data remains in the same volume. Preserve any acceptance overlay flags when recreating the stack. This helper applies only to the single-server service addresses; split-server credentials must be coordinated across both hosts.

Telemetry is disabled in the backend and Chroma, and Hugging Face/Transformers are explicitly offline. Ollama cloud access is disabled. Website synchronization runs only when explicitly configured with a reachable local website; the default URL is blank. Internal services have no host ports in the single-server layout. Optional remote data services require the separate TLS/authenticated split-server configuration.

For acceptance with backend/model/data outbound networking blocked, use `docker compose -f docker-compose.yml -f deploy/compose/offline-validation.yml up -d --no-build --pull never --wait --wait-timeout 600`. The backend, workers, Ollama, databases and Redis stay on an internal network. The frontend alone also joins a separate ingress bridge so published HTTPS works on Docker Desktop; it forwards API requests over the internal network and serves local static assets. Optional LAN website synchronization must remain disabled for this check. Use the ordinary deployment network when you intentionally need the separate remote data server or local website.
