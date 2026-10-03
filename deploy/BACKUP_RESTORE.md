# Coordinated backup and recovery

Backups cover PostgreSQL (accounts, conversations, job states and document registry), Chroma indexes, Redis persistence, uploaded/staged documents and local caches, plus configuration and TLS/CA keys. Model blobs are immutable release artifacts and are restored from the matching offline release rather than copied into each backup.

```bash
# Single-server installation; pauses application writers briefly and resumes running services.
bash server/scripts/backup.sh /srv/rag-backups/snapshot-001
```

The script refuses an existing destination. It stops the frontend/API/workers, then stops Chroma and Redis, dumps PostgreSQL, archives the stopped stores and document files, and writes checksums. An exit trap resumes the services that were running. Keep backup output outside the application data directory. The backup directory contains passwords, documents, active sessions and private keys: restrict it to administrators and move a protected copy off the server. Do not copy live Chroma files or independently restore only the SQL registry.

Recovery is intentionally restricted to a **fresh empty directory and separate Docker project/volumes**. This makes the first restore a reviewable verification operation and avoids overwriting a production knowledge base.

```bash
# Load the matching release images first. Restore checks portable content fingerprints.
bash server/scripts/restore.sh /media/rag-backup /srv/local-rag-recovery
cd /srv/local-rag-recovery
# Import the same release's models; this retains the restored .env and recovered data.
bash server/scripts/import-offline.sh /media/release-2.2.0 /srv/local-rag-recovery
```

The restore tool verifies hashes and image content fingerprints, validates archive paths, creates isolated Chroma/Redis/PostgreSQL volumes, extracts documents/configuration and restores SQL with errors treated as failures. It rewrites data/TLS host paths to the recovery directory and stores a unique `COMPOSE_PROJECT_NAME` in `.env`, leaving the original containers and volumes alone. Configured `COMPOSE_FILE` overlays, including outbound-network isolation, are preserved and must resolve inside that recovery installation. Reusing an existing/nonempty directory is refused. Older backups without portable fingerprints retain their stricter original image-ID checks.

Before starting recovery next to the original installation, set unused `HTTPS_PORT`/`HTTP_REDIRECT_PORT` in the recovered `.env` and ensure the restored certificate covers the recovery hostname. Start with `docker compose up -d --no-build --pull never --wait --wait-timeout 600`, then run `docker compose exec api python -m app.cli health --component all`. Verify login, expected documents, citations and a new upload/replacement. Verify queued-job recovery and conversation history too. Direct users to recovery only after these checks pass. Keep the original installation for rollback until the recovered system has been accepted.

Use the same application release and model manifests that produced the backup. Before restoring an old backup into a newer release, first validate it under its original images and perform the documented upgrade/additive migrations on a protected copy.

For a two-server deployment, pause writers on the application server and capture PostgreSQL/Chroma on the data server while writers remain stopped. The single-server backup script does not automatically coordinate two hosts. Use a storage snapshot or equivalent quiesced database/index backup under that same outage window, then include the application's Redis/documents/configuration. Restore both halves together into isolated hosts/projects and verify before switching clients.
