# Running the application

## Start, stop and status

From an extracted standalone package:

| Action | Windows | Linux |
| --- | --- | --- |
| Start | `RUN.cmd` | `bash RUN.sh` |
| Status | `RUN.cmd status` | `bash RUN.sh status` |
| Stop | `RUN.cmd stop` | `bash RUN.sh stop` |

From a prepared source installation:

```bash
bash server/scripts/start-local.sh
docker compose ps -a
docker compose stop
```

Stopping retains application files and Docker volumes. Docker must start with the host for service restart policies to take effect. Database initialization and dependency checks are one-shot containers; `Exited (0)` is normal for them. Use [troubleshooting](TROUBLESHOOTING.md) when a continuous service is unhealthy or repeatedly restarting.

The commands below run from the repository root, or from the standalone package's `app` directory. Preserve the installation's `.env` and Docker project name so the same volumes are reused.

## Users and conversations

Sign in over HTTPS. Administrators can create administrator or technician accounts from the management screen; new passwords require at least 12 characters. A prepared source installation creates its first administrator with:

```bash
docker compose exec api python -m app.cli create-user --username admin --role admin
```

Start a new conversation or select an earlier conversation in the sidebar. The interface restores history and pending work after a reload, renews expired conversation sessions, and provides a logout button. Expired authentication requires signing in again.

Chat requests are queued rather than held open until generation finishes. The processing indicator remains until the saved answer is available. Citation details show the document name, evidence ID, version and PDF page when available. An insufficient-evidence response means the selected local content did not support an answer.

## Upload and manage documents

Administrators can upload `.pdf`, `.docx` and `.doc` files. Word tables, headers and footers are included in extraction. Scanned pages use local English/Hebrew OCR; its quality depends on scan clarity. Wait for the job's completed state before asking about a new document. Failed extraction/indexing is shown in the processing list.

Use a consistent base filename and explicit version suffix, for example `Network_Guide_v1.docx` followed by `Network_Guide_v2.docx`. The parser derives a stable topic from the original filename, removes recognized version markers, and publishes the newest indexed generation. A lower-ranked version does not replace a newer one; failed indexing leaves the previous version available. Renaming the base document can create a separate topic.

The document list shows active/indexing documents and allows removal with confirmation. Removal retires the topic's active/staged content and cancels pending replacements. Uploading a document again is an explicit new import.

The knowledge-parameter screen adds a named value and explanation to the knowledge base, such as an organizational configuration setting. Its indexing job has the same visible processing states as uploaded documents.

## Configuration and maintenance

Source installations use `.env`; standalone launchers generate `app/.env` from `run-config.json` and packaged model settings. Configure standalone names and ports in `run-config.json`. Keep service credentials, model roles and existing data consistent when changing configuration.

Useful defaults are listed in [.env.example](../.env.example): conversation inactivity is 15 minutes, message retention is 7 days, job retention is 30 days, uploads are limited to 25 MiB, and technician messages to 4,000 characters. Actual configured limits take precedence. Vision and website synchronization are disabled by default; their model/network requirements must be prepared before enabling them.

Inspect readiness and recent logs:

```bash
docker compose exec api python -m app.cli health --component api
docker compose logs --tail=100 api chat-worker background-worker
```

After correcting a failed job's cause, an administrator can retry a retained job from the server:

```bash
docker compose exec api python -m app.cli retry-job JOB_ID
```

For an older installation that used timestamp-derived upload identities, take a backup before running `docker compose exec api python -m app.cli repair-documents`, then review the resulting document list and citations.

Use [backup and restore](../deploy/BACKUP_RESTORE.md) for coordinated snapshots and server moves. Keep backups protected: they contain accounts, document content, conversations, configuration and private keys. Model archives are restored from the matching release.
