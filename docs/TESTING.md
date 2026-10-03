# Testing and acceptance

Use an isolated installation for live acceptance, document removal, worker interruption and recovery checks. Tests describe repeatable verification; results from a previous machine do not establish readiness or performance on a new server.

The [GitHub checks](../.github/workflows/checks.yml) run application unit tests, version identity examples, release/standalone tool tests, shell syntax checks and the frontend build without Ollama models or application data. GitHub needs internet access to install locked dependencies. Real OCR/conversion and full-stack acceptance still require the prepared release environment below. The standalone suites contain 29 checks (23 preparation and 6 launcher checks), and release tooling contains 6 checks; these passed with container networking disabled during release validation.

## Python regression and extraction checks

Build or import the release backend image first. These commands mount source read-only, use no external networking and keep test database/files isolated:

```bash
docker run --rm --pull never --network none -v "$PWD:/project:ro" --entrypoint python local-rag/backend:2.2.0 -m unittest discover -s /project/tests -p 'test_*.py' -v
docker run --rm --pull never --network none -v "$PWD:/project:ro" --entrypoint python local-rag/backend:2.2.0 -m unittest discover -s /project/server/scripts/tests -v
docker run --rm --pull never --network none -v "$PWD:/project:ro" --entrypoint python local-rag/backend:2.2.0 -m unittest discover -s /project/server/standalone/tests -v
docker run --rm --pull never --network none --entrypoint python local-rag/backend:2.2.0 -m pip check
```

In PowerShell, use `-v "${PWD}:/project:ro"`. The Python suite covers parsing, version publication, durable jobs, cancellation, ownership, local OCR/Word conversion and retrieval behavior. Tool tests check release integrity and archive handling. Standalone launcher tests use a fake Docker executable; they do not start a server.

## Frontend browser checks

On a connected preparation/development machine, install the locked dependencies, build the production frontend and run the existing browser acceptance script:

```bash
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
node tests/frontend_smoke.mjs
```

This requires Node 22 (22.12 or newer) and Chrome/Edge, or a `CHROME_PATH` executable. The script exercises login, citations, job/session restoration, failures, document administration and mobile layout against API fixtures. It does not test actual Ollama generation.

## Live offline acceptance

Prepare the configured models, release images, credentials and local HTTPS first. Start a disposable stack with backend/model/data outbound networking blocked:

```bash
docker compose -f docker-compose.yml -f deploy/compose/offline-validation.yml up -d --no-build --pull never --wait --wait-timeout 600
docker compose -f docker-compose.yml -f deploy/compose/offline-validation.yml run --rm --no-deps -T -v "$PWD:/project:ro" api python /project/tests/offline_acceptance.py
```

The script creates and removes its own test account/documents and exercises the actual API, workers, PostgreSQL, Chroma and Ollama. The overlay keeps service-to-service connections internal while the frontend has an ingress bridge for browser access. Keep website synchronization disabled for this check.

Also verify in the browser:

1. Upload a DOCX with a paragraph, table and relevant header/footer; verify their facts and citations after processing completes.
2. Upload a text PDF with repeated page content and an English/Hebrew scanned PDF; check page citations and OCR quality on your real documents.
3. Replace `Network_Guide_v1.docx` with `Network_Guide_v2.docx`, then remove a test topic; verify retrieval follows the active registry.
4. Ask supported and unsupported questions in English and Hebrew, plus a follow-up. Compare cited claims with the actual documents.
5. Reload during queued work, renew authentication, switch conversations and verify account ownership boundaries.
6. Stop/restart an ingestion worker during a test job; after lease recovery, verify completion or a retained explicit failure.
7. Restart Docker/the host and confirm the same documents/history return. Measure queue delay, generation time and peak memory at the planned concurrency.
8. Restore a coordinated backup into a fresh directory/project and verify documents, users, citations/history and queued work before accepting recovery.

For worker and backup operations, use [RUNNING.md](RUNNING.md) and [BACKUP_RESTORE.md](../deploy/BACKUP_RESTORE.md). Re-run the relevant tests when code, dependency locks, models or release images change. Hardware capacity and physical host reboot behavior need verification on the intended server.
