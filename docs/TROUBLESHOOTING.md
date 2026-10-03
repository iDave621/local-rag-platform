# Troubleshooting

Run Docker commands from the source installation root, or from the standalone package's `app` directory. Preserve the same `.env` and Docker project name.

## First checks

```bash
docker compose ps -a
docker compose exec api python -m app.cli health --component api
docker compose logs --tail=100 dependency-check api chat-worker background-worker
```

`db-init` and `dependency-check` should finish with exit code 0. Continuous services should be running and pass their configured health checks. Readiness reports PostgreSQL, Redis, Chroma, required Ollama models and worker availability. A missing dependency can prevent frontend startup.

| Symptom | Check and action |
| --- | --- |
| Runner cannot contact Docker | Start Docker Engine/Desktop. Windows must use Linux containers; the release needs amd64/x86_64. Check `docker info` and `docker compose version`. |
| Package checksum fails | Keep the original archive and its checksum. Recopy/re-extract the complete package into a fresh directory; inspect the named mismatching file. Only `run-config.json` is intended to be edited before first launch. |
| Port is already in use | Select unused HTTPS/redirect ports in standalone `run-config.json` or source `.env`. Open the explicit `https://host:port` address for a nonstandard port. |
| Required model is missing | Inspect `docker compose exec ollama ollama list`. Use the models packaged for this release; on a disconnected target, transfer a prepared complete model release rather than downloading. |
| Database/Redis authentication fails | Keep `.env` credentials aligned with existing stores. Replacing a service password in configuration alone does not change an existing PostgreSQL role. Follow the documented coordinated credential procedure. |
| Worker is unavailable | Check its container status and logs. An interrupted job is reclaimed after its lease expires, with bounded retries; it remains visible in the job list. |
| Login is rejected or rate limited | Confirm username/password, then wait before retrying a rate-limited login. A standalone first-launch login is in `runtime/admin-login.txt`; retained accounts keep their existing passwords. |
| Authentication repeatedly expires | Use the intended HTTPS URL and trust its CA. The default deployment uses secure cookies. Sign in again after authentication expires. |

Credential rotation and model import procedures are in [OFFLINE_DEPLOYMENT.md](../deploy/OFFLINE_DEPLOYMENT.md). Use [backup and restore](../deploy/BACKUP_RESTORE.md) before changing an existing installation.

## Browser certificate warning

Trust the public CA certificate on the client: `deploy/tls/ca.crt` for a source installation, or `app/deploy/tls/ca.crt` for a standalone package. On Windows, import it into **Trusted Root Certification Authorities**. Use a DNS name or IPv4 address covered by the server certificate.

If the certificate has expired or the server name changed, follow [standalone certificate renewal](INSTALLATION.md#renew-a-standalone-certificate) or [the manual HTTPS procedure](../deploy/OFFLINE_DEPLOYMENT.md#local-https-and-client-trust). Keep the CA/server private keys on the server. The loopback HTTP override below is for local development rather than LAN authentication:

```bash
docker compose -f docker-compose.yml -f deploy/compose/local-http.yml -f deploy/compose/offline-validation.yml up -d --no-build --pull never --wait --wait-timeout 600
```

It serves `http://127.0.0.1:8088` by default and disables secure cookies for that local API.

## Document upload or extraction fails

Inspect the processing error in the administrator screen, then check `background-worker` logs. Supported uploads are PDF, DOCX and DOC, within the configured size limits. Encrypted PDFs must be decrypted before upload. Blank files or scans that OCR cannot read may produce no indexable content. Save a damaged/unsupported Word file as a valid DOCX and try again.

The release backend includes Tesseract with `eng` and `heb` language data and LibreOffice for legacy DOC conversion. Other OCR languages must be installed in a newly prepared backend image and configured with `OCR_LANGUAGES`; they are not downloaded at runtime.

After correcting the cause, upload a corrected document or run `retry-job JOB_ID` for a retained failed job. A job that failed during replacement does not remove the prior active document.

## Answer is missing, stale or slow

Confirm the relevant upload completed and the intended document is active. Check its base filename/version: changing the base name can leave two distinct topics, while an older explicit version is intentionally skipped. Remove obsolete topics through the administrator interface.

An insufficient-evidence response can mean no relevant content was retrieved, the prompt had no space for usable evidence, or the generated answer's citation IDs failed validation. Ask a more specific question, inspect the original source, and verify the extraction result. Citation checks verify IDs, not the factual accuracy of every sentence.

Generation speed depends on hardware, model size, prompt size and other queued work. Check `chat-worker` and Ollama logs and monitor host RAM/GPU use. The default model generation parallelism is one. Measure representative workloads before increasing concurrency. A browser reload preserves queued work; temporary status connection failures are retried by the interface.

## Data appears missing after a move or restart

Confirm the installation is using its original Docker project, named volumes and data paths. A different project name can start empty volumes. The application folder alone does not include database/index volumes. Use the coordinated restore procedure rather than combining an unrelated SQL registry with old vectors.

For diagnostic reports, include service status, the failing component/job ID and a small relevant log excerpt. Remove passwords, authentication tokens, private keys and private document content before sharing logs.
