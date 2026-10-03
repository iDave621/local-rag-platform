# Local RAG Platform

A document assistant for local servers, including environments without internet access. Upload PDF or Word documents, ask questions, and receive answers with references to the relevant documents. Ollama runs the language model and embeddings locally.

## Features

- PDF, DOCX and legacy DOC uploads with visible processing and failure states.
- Word paragraph, table, header and footer extraction; local English and Hebrew OCR for scanned pages.
- Document version replacement and removal from the searchable knowledge base.
- Persistent conversations, saved citations, session recovery and asynchronous answers.
- Administrator and technician roles, user management and knowledge parameters.
- Hebrew RTL interface, local HTTPS, authenticated access and login rate limits.
- Offline image/model packaging, dependency checks and coordinated backup tools.

## Architecture

| Component | Purpose |
| --- | --- |
| React + nginx | Browser interface, local assets, HTTPS and API proxy |
| FastAPI | Authentication, conversations, document administration and jobs |
| PostgreSQL | Users, conversation history, durable jobs and document registry |
| Chroma | Document chunks and their vectors |
| Ollama | Local answer generation and embeddings |
| Redis + workers | Job wake-up signals, ingestion, chat and maintenance |
| Tesseract + LibreOffice | Local OCR and legacy Word conversion |

PostgreSQL determines which document generation is searchable. Workers index replacements before publishing them; failed replacements preserve the previous active version. Chat requests return a job ID, and the interface retrieves the completed answer.

## Run a prepared standalone package

The package requires Docker Engine with Compose **2.24.4 or newer**, running **Linux amd64** containers. Windows uses Docker Desktop in Linux containers mode. Images and models are included; host Python, Node and Ollama are unnecessary.

For Windows Server, run the Linux package in a Linux virtual machine with Docker Engine. [Docker Desktop supports desktop Windows editions and is not supported on Windows Server](https://docs.docker.com/desktop/setup/install/windows-install/#system-requirements).

```text
tar -xf local-rag-standalone-2.2.0.tar
cd local-rag-standalone-2.2.0
```

Run `RUN.cmd` on Windows or `bash RUN.sh` on Linux. Before the first launch, edit `run-config.json` to set the server's LAN names and ports. First launch creates new credentials and an empty knowledge base. Sign in and upload your documents.

See [installation](docs/INSTALLATION.md) for preparation, client HTTPS trust and source-based setup.

## Quick local run from source

First prepare the local release images and the configured Ollama models as described in [installation](docs/INSTALLATION.md#prepare-from-source-on-a-connected-machine). A Git checkout contains source and example configuration; large image/model files and private application data are prepared separately.

```bash
bash server/scripts/init-config.sh
bash server/scripts/generate-lan-tls.sh localhost
bash server/scripts/start-local.sh
docker compose exec api python -m app.cli create-user --username admin --role admin
```

Trust `deploy/tls/ca.crt` on the browser client, then open `https://localhost`. Include the actual LAN hostname/IP when generating a certificate for other clients.

## Offline operation and limits

The default models are `qwen3:4b-instruct` and `embeddinggemma`. Their manifests and blobs must be available locally before startup. Preparing fresh images or downloading models requires a connected preparation machine; the disconnected target starts with `--no-build --pull never`.

OCR quality depends on document quality. Encrypted or unreadable documents can fail processing. Answers can be incomplete or inaccurate: citation IDs are checked against retrieved evidence, but this does not prove every claim is correct. Review important answers against their sources. Capacity depends on the server's CPU, RAM, GPU configuration and document volume; measure before increasing concurrency.

Normal inference, embeddings and OCR stay on the server. Telemetry and remote tracing are disabled in the default deployment. Website synchronization and vision are optional and disabled by default. Website sync requires network access to its configured site; vision requires its additional local model.

Uploaded documents, indexes, conversations and configuration remain in local files and Docker volumes. Protect those files and backups as private application data. Internal database/model services have no host ports in the default single-server layout; only the web interface is published.

## Documentation

- [Installation and release preparation](docs/INSTALLATION.md)
- [Running the application and managing documents](docs/RUNNING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Testing and acceptance](docs/TESTING.md)
- [Publishing the source to GitHub](docs/PUBLISHING.md)
- [Backup and restore](deploy/BACKUP_RESTORE.md)
- [Detailed offline deployment](deploy/OFFLINE_DEPLOYMENT.md)
- [Optional two-server deployment](deploy/SPLIT_DEPLOYMENT.md)
